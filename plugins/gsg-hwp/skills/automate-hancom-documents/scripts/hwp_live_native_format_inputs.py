from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from hwp_color_normalization import parse_rgb_color
from hwp_live_table_contract import (
    BorderWidth,
    CellBorder,
    CellBorders,
    CellPadding,
    TableCell,
)
from hwp_live_values import Alignment, Rgb
from hwp_operation_contract import OperationInputValue


_ADDRESS: Final = re.compile(r"^[A-Z]+[1-9][0-9]*$")
# 한 요청이 펼칠 수 있는 셀 수의 상한이다. 이 위로는 명령 batch 분할
# (hwp_live_native_format_commands._table_format_batches)이 감당하는 문제가
# 아니라 요청 자체가 표를 잘못 짚은 것이다.
_MAX_TABLE_FORMAT_CELLS: Final = 20_000
_DECIMAL: Final = re.compile(r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")
_ALIGNMENTS: Final[dict[str, Alignment]] = {
    "inherit": "inherit",
    "left": "left",
    "center": "center",
    "right": "right",
    "justify": "justify",
}
_VERTICAL_ALIGNMENTS: Final = frozenset(("inherit", "top", "center", "bottom"))
_BORDER_STYLES: Final = frozenset(
    (
        "none",
        "solid",
        "dash",
        "dot",
        "dash_dot",
        "dash_dot_dot",
        "long_dash",
        "circle",
        "double_slim",
        "slim_thick",
        "thick_slim",
        "slim_thick_slim",
    )
)
_BORDER_WIDTHS: Final[dict[float, BorderWidth]] = {
    0.1: "0.1mm",
    0.12: "0.12mm",
    0.15: "0.15mm",
    0.2: "0.2mm",
    0.25: "0.25mm",
    0.3: "0.3mm",
    0.4: "0.4mm",
    0.5: "0.5mm",
    0.6: "0.6mm",
    0.7: "0.7mm",
    1.0: "1.0mm",
    1.5: "1.5mm",
    2.0: "2.0mm",
    3.0: "3.0mm",
    4.0: "4.0mm",
    5.0: "5.0mm",
}
_TEXT_KEYS: Final = frozenset(
    (
        "bold",
        "font_name",
        "font_size_pt",
        "text_color",
        "alignment",
        "line_spacing",
        "letter_spacing_percent",
        "left_margin_mm",
        "indentation_mm",
        "keep_with_next",
        "page_break_before",
    )
)
# 서식 값이 아니라 "어느 텍스트인가" 를 적어 두는 이름들이다
# (hwp_public_selection_tools._text_patch_parameters 가 만든다). 허용 목록에
# 넣어 조용히 무시하면 지목한 곳이 아니라 현재 선택에 서식이 걸리므로 거부는
# 그대로 두고, 거부 문구만 어느 자리가 섞였는지 말한다.
_TARGETING_KEYS: Final = frozenset(
    (
        "target_kind",
        "match_case",
        "occurrence",
        "expected_text",
        "replacement",
        "start_list",
        "start_paragraph",
        "start_character",
        "end_list",
        "end_paragraph",
        "end_character",
        "table_instance_id",
    )
)
_TABLE_KEYS: Final = (
    _TEXT_KEYS
    - {
        "letter_spacing_percent",
        "left_margin_mm",
        "indentation_mm",
        "keep_with_next",
        "page_break_before",
    }
) | frozenset(
    (
        "cell",
        "cells",
        "protect_size",
        "format_source_table_id",
        "format_source_cell",
        "row_height_mm",
        "column_width_mm",
        "padding_left_mm",
        "padding_right_mm",
        "padding_top_mm",
        "padding_bottom_mm",
        "vertical_alignment",
        "fill_color",
        "border_style",
        "border_width",
        "border_color",
        "border_sides",
    )
)


@dataclass(frozen=True, slots=True)
class InputFailure:
    status: Literal["needs_input", "schema_conflict"]
    message: str
    required_inputs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TextFormatSpec:
    bold: bool | None
    font_name: str | None
    font_size_pt: float | None
    text_color: Rgb | None
    alignment: Alignment
    line_spacing: int | None
    letter_spacing_percent: int | None = None
    left_margin_mm: float | None = None
    indentation_mm: float | None = None
    keep_with_next: bool | None = None
    page_break_before: bool | None = None


@dataclass(frozen=True, slots=True)
class TableFormatSpec:
    cell: str | None
    formatting: TableCell
    row_height_mm: float | None
    column_width_mm: float | None
    # 여러 셀을 한 요청에 담을 때 실제로 적용할 주소들이다. 'A4:P4' 같은 구간은
    # 여기서 이미 펼쳐진 상태이고, cell 하나만 온 요청은 그 하나가 들어온다.
    cells: tuple[str, ...] = ()
    protect_size: bool | None = None
    format_source_table_id: str | None = None
    format_source_cell: str | None = None


@dataclass(frozen=True, slots=True)
class MergeSpec:
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class SplitSpec:
    cell: str
    columns: int
    rows: int
    distribute_height: bool
    merge: bool
    split_mode: Literal["equal", "existing_grid"]


def _address(value: OperationInputValue | None) -> str | None:
    if isinstance(value, str):
        normalized = value.strip().upper()
        return normalized if _ADDRESS.fullmatch(normalized) is not None else None
    return None


def table_cell_coordinate(address: str) -> tuple[int, int]:
    index = 0
    column = 0
    while index < len(address) and address[index].isalpha():
        column = column * 26 + ord(address[index]) - ord("A") + 1
        index += 1
    return int(address[index:]), column


def table_cell_address(row: int, column: int) -> str:
    """Inverse of ``table_cell_coordinate``.

    A split moves the grid under addresses the caller already holds, and the
    only way to say "C2 is now D2" without guessing is to build the new
    address from the coordinates the live topology reports.
    """
    letters = ""
    remaining = column
    while remaining > 0:
        remaining, index = divmod(remaining - 1, 26)
        letters = chr(ord("A") + index) + letters
    return f"{letters}{row}"


def _cell_range(token: str) -> tuple[str, ...] | None:
    """'A4:P4' 를 그 직사각형이 덮는 주소들로 편다.

    두 끝점이 어느 순서로 오든 같은 직사각형이므로 좌표를 정렬한 뒤 편다.
    격자 위의 좌표만 만들 뿐이고, 그 주소가 실제 표에 있는지는 여기서 알 수
    없다 -- 병합 셀이 감춘 주소는 뒤에서 CellTopology 가 판정한다.
    """
    start, _, end = token.partition(":")
    if _ADDRESS.fullmatch(start) is None or _ADDRESS.fullmatch(end) is None:
        return None
    start_row, start_column = table_cell_coordinate(start)
    end_row, end_column = table_cell_coordinate(end)
    top, bottom = sorted((start_row, end_row))
    left, right = sorted((start_column, end_column))
    if (bottom - top + 1) * (right - left + 1) > _MAX_TABLE_FORMAT_CELLS:
        return None
    return tuple(
        table_cell_address(row, column)
        for row in range(top, bottom + 1)
        for column in range(left, right + 1)
    )


def _cell_addresses(value: OperationInputValue | None) -> tuple[str, ...] | None:
    """``cells`` 입력을 실제 셀 주소들로 편다.

    OperationInputValue 는 스칼라만 담으므로 목록은 쉼표로 이어 붙여 온다
    (hwp_public_table_edit_tools.hwp_format_table 이 만든다). 각 조각은 셀
    주소이거나 'A4:P4' 형태의 구간이다.
    """
    if not isinstance(value, str):
        return None
    addresses: list[str] = []
    for token in value.split(","):
        normalized = token.strip().upper()
        if ":" in normalized:
            expanded = _cell_range(normalized)
        elif _ADDRESS.fullmatch(normalized) is not None:
            expanded = (normalized,)
        else:
            expanded = None
        if expanded is None:
            return None
        addresses.extend(expanded)
        if len(addresses) > _MAX_TABLE_FORMAT_CELLS:
            return None
    return tuple(addresses)


def _millimeters(value: OperationInputValue | None) -> float | None:
    parsed = _number(value)
    if parsed is None:
        return None
    return parsed if 1 <= parsed <= 250 else None


def _padding_millimeters(value: OperationInputValue | None) -> float | None:
    parsed = _number(value)
    if parsed is None:
        return None
    return parsed if 0 <= parsed <= 20 else None


def _rgb(value: OperationInputValue | None) -> Rgb | None:
    return parse_rgb_color(value) if isinstance(value, str) else None


def _boolean(value: OperationInputValue | None) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value) if value in (0, 1) else None
    if isinstance(value, str):
        return {"false": False, "0": False, "true": True, "1": True}.get(
            value.strip().casefold()
        )
    return None


def _integer(
    value: OperationInputValue | None, minimum: int, maximum: int
) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        parsed = int(value)
    else:
        token = value.strip()
        digits = token.removeprefix("+")
        if not digits.isdecimal():
            return None
        parsed = int(digits)
    return parsed if minimum <= parsed <= maximum else None


def _number(value: OperationInputValue | None) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    token = value.strip()
    return float(token) if _DECIMAL.fullmatch(token) is not None else None


def normalize_format_name_input(value: OperationInputValue | None) -> str | None:
    return value.strip().casefold() if isinstance(value, str) else None


def normalize_border_width_input(
    value: OperationInputValue | None,
) -> BorderWidth | None:
    normalized = normalize_format_name_input(value)
    numeric: OperationInputValue | None = value
    if normalized is not None:
        numeric = normalized[:-2].strip() if normalized.endswith("mm") else normalized
    parsed = _number(numeric)
    return None if parsed is None else _BORDER_WIDTHS.get(parsed)


def _unknown(
    parameters: Mapping[str, OperationInputValue],
    allowed: frozenset[str],
    where: str,
) -> InputFailure | None:
    """Say which names are wrong, and say it about the caller's own path.

    ``where`` exists because four parsers share this -- text format, table
    format, merge and split -- and they do not receive their target the same
    way. The text path formats whatever is selected; the other three act on a
    table that was resolved before parsing. One sentence about "the current
    selection" is false for three of the four, so each caller supplies its own
    clause. ``tests/test_hwp_text_patch.py`` asserts all four.
    """
    names = tuple(sorted(set(parameters) - allowed))
    if not names:
        return None
    targeting = tuple(name for name in names if name in _TARGETING_KEYS)
    if targeting:
        return InputFailure(
            "schema_conflict",
            f"대상 지정 값이 서식 입력에 섞여 있습니다: {', '.join(targeting)}. "
            + f"{where}, 대상 지정은 target으로 받습니다",
        )
    return InputFailure(
        "schema_conflict", f"지원하지 않는 서식 입력 필드입니다: {', '.join(names)}"
    )


def parse_text_format(
    parameters: Mapping[str, OperationInputValue],
) -> TextFormatSpec | InputFailure:
    unknown = _unknown(parameters, _TEXT_KEYS, "이 경로는 현재 선택에만 서식을 걸고")
    if unknown is not None:
        return unknown
    if not parameters:
        return InputFailure(
            "needs_input",
            "적용할 글자 또는 문단 서식이 필요합니다",
            tuple(f"inputs.parameters.{name}" for name in sorted(_TEXT_KEYS)),
        )
    bold = None if "bold" not in parameters else _boolean(parameters.get("bold"))
    name = parameters.get("font_name")
    font_name = name.strip() if isinstance(name, str) and name.strip() else None
    font_size_value = _number(parameters.get("font_size_pt"))
    font_size = (
        font_size_value
        if "font_size_pt" in parameters
        and font_size_value is not None
        and 1 <= font_size_value <= 96
        else None
    )
    color = (
        None if "text_color" not in parameters else _rgb(parameters.get("text_color"))
    )
    raw_alignment = parameters.get("alignment", "inherit")
    alignment = _ALIGNMENTS.get(normalize_format_name_input(raw_alignment) or "")
    spacing = (
        None
        if "line_spacing" not in parameters
        else _integer(parameters.get("line_spacing"), 50, 500)
    )
    if alignment is None:
        return InputFailure("schema_conflict", "문단 정렬 값이 올바르지 않습니다")
    letter_spacing = (
        None
        if "letter_spacing_percent" not in parameters
        else _integer(parameters.get("letter_spacing_percent"), -50, 50)
    )
    left_margin = _number(parameters.get("left_margin_mm"))
    indentation = _number(parameters.get("indentation_mm"))
    keep_next = _boolean(parameters.get("keep_with_next"))
    page_break = _boolean(parameters.get("page_break_before"))
    invalid = (
        ("bold" in parameters and bold is None)
        or ("font_name" in parameters and font_name is None)
        or ("font_size_pt" in parameters and font_size is None)
        or ("text_color" in parameters and color is None)
        or ("line_spacing" in parameters and spacing is None)
        or ("letter_spacing_percent" in parameters and letter_spacing is None)
        or (
            "left_margin_mm" in parameters
            and (left_margin is None or not 0 <= left_margin <= 200)
        )
        or (
            "indentation_mm" in parameters
            and (indentation is None or not -200 <= indentation <= 200)
        )
        or ("keep_with_next" in parameters and keep_next is None)
        or ("page_break_before" in parameters and page_break is None)
    )
    if invalid:
        return InputFailure(
            "schema_conflict", "글자·문단 서식 입력 형식이나 범위가 올바르지 않습니다"
        )
    return TextFormatSpec(
        bold,
        font_name,
        font_size,
        color,
        alignment,
        spacing,
        letter_spacing,
        left_margin_mm=left_margin,
        indentation_mm=indentation,
        keep_with_next=keep_next,
        page_break_before=page_break,
    )


def parse_table_format(
    parameters: Mapping[str, OperationInputValue],
) -> TableFormatSpec | InputFailure:
    unknown = _unknown(
        parameters, _TABLE_KEYS, "이 경로는 확정된 표 하나에만 서식을 걸고"
    )
    if unknown is not None:
        return unknown
    if "protect_size" in parameters:
        protect_size = _boolean(parameters["protect_size"])
        if protect_size is None:
            return InputFailure(
                "schema_conflict", "크기 고정은 true 또는 false여야 합니다"
            )
        if set(parameters) != {"protect_size"}:
            return InputFailure(
                "schema_conflict", "표 개체 크기 고정은 셀 서식과 별도 호출해야 합니다"
            )
        return TableFormatSpec(None, TableCell(), None, None, protect_size=protect_size)
    cell = _address(parameters.get("cell"))
    cells: tuple[str, ...] = () if cell is None else (cell,)
    if "cells" in parameters:
        expanded = _cell_addresses(parameters.get("cells"))
        if expanded is None:
            return InputFailure(
                "schema_conflict",
                "cells는 'A4:P4' 형식의 구간이거나 쉼표로 이어 붙인 셀 주소여야 "
                f"하고, 펼친 셀 수는 {_MAX_TABLE_FORMAT_CELLS}개를 넘을 수 없습니다",
            )
        # cell 과 cells 가 같이 오면 겹치는 주소를 두 번 방문하지 않는다.
        cells = tuple(dict.fromkeys((*cells, *expanded)))
    if "format_source_table_id" in parameters or "format_source_cell" in parameters:
        source_id = parameters.get("format_source_table_id")
        source_cell = _address(parameters.get("format_source_cell"))
        if not isinstance(source_id, str) or not source_id or source_cell is None:
            return InputFailure(
                "schema_conflict", "서식 원본의 표 ID와 실제 셀 주소가 필요합니다"
            )
        if not cells:
            return InputFailure(
                "needs_input", "서식 복사는 대상 cell 또는 cells가 필요합니다"
            )
        if set(parameters) - {
            "cell",
            "cells",
            "format_source_table_id",
            "format_source_cell",
        }:
            return InputFailure(
                "schema_conflict",
                "원본 서식 복사와 개별 서식 지정은 함께 사용할 수 없습니다",
            )
        return TableFormatSpec(
            cell,
            TableCell(),
            None,
            None,
            cells=cells,
            format_source_table_id=source_id,
            format_source_cell=source_cell,
        )
    format_parameters = {
        name: value for name, value in parameters.items() if name in _TEXT_KEYS
    }
    row_height = (
        None
        if "row_height_mm" not in parameters
        else _millimeters(parameters.get("row_height_mm"))
    )
    column_width = (
        None
        if "column_width_mm" not in parameters
        else _millimeters(parameters.get("column_width_mm"))
    )
    if ("row_height_mm" in parameters and row_height is None) or (
        "column_width_mm" in parameters and column_width is None
    ):
        return InputFailure(
            "schema_conflict",
            "행 높이와 열 너비는 1mm 이상 250mm 이하여야 합니다",
        )
    padding_names = (
        "padding_left_mm",
        "padding_right_mm",
        "padding_top_mm",
        "padding_bottom_mm",
    )
    supplied_padding = tuple(name for name in padding_names if name in parameters)
    if supplied_padding and len(supplied_padding) != len(padding_names):
        return InputFailure(
            "schema_conflict",
            "셀 여백은 왼쪽·오른쪽·위·아래 값을 모두 지정해야 합니다",
            tuple(f"inputs.parameters.{name}" for name in padding_names),
        )
    left_padding = _padding_millimeters(parameters.get("padding_left_mm"))
    right_padding = _padding_millimeters(parameters.get("padding_right_mm"))
    top_padding = _padding_millimeters(parameters.get("padding_top_mm"))
    bottom_padding = _padding_millimeters(parameters.get("padding_bottom_mm"))
    padding_values = (left_padding, right_padding, top_padding, bottom_padding)
    if supplied_padding and any(value is None for value in padding_values):
        return InputFailure(
            "schema_conflict",
            "셀 여백은 0mm 이상 20mm 이하여야 합니다",
        )
    padding = None
    if supplied_padding:
        assert left_padding is not None
        assert right_padding is not None
        assert top_padding is not None
        assert bottom_padding is not None
        padding = CellPadding(
            left_mm=left_padding,
            right_mm=right_padding,
            top_mm=top_padding,
            bottom_mm=bottom_padding,
        )
    text = (
        parse_text_format(format_parameters)
        if format_parameters
        else TextFormatSpec(None, None, None, None, "inherit", None)
    )
    if isinstance(text, InputFailure):
        return text
    vertical = normalize_format_name_input(
        parameters.get("vertical_alignment", "inherit")
    )
    if vertical not in _VERTICAL_ALIGNMENTS:
        return InputFailure("schema_conflict", "셀 세로 정렬 값이 올바르지 않습니다")
    fill = (
        None if "fill_color" not in parameters else _rgb(parameters.get("fill_color"))
    )
    border_color = (
        (0, 0, 0)
        if "border_color" not in parameters
        else _rgb(parameters.get("border_color"))
    )
    border_style = normalize_format_name_input(parameters.get("border_style", "solid"))
    border_width = normalize_border_width_input(
        parameters.get("border_width", "0.12mm")
    )
    has_border = any(
        name in parameters for name in ("border_color", "border_style", "border_width")
    )
    border_sides = ("left", "right", "top", "bottom")
    if "border_sides" in parameters:
        raw_sides = parameters["border_sides"]
        if not isinstance(raw_sides, str):
            return InputFailure(
                "schema_conflict", "테두리 변은 left/right/top/bottom이어야 합니다"
            )
        border_sides = tuple(dict.fromkeys(raw_sides.split(",")))
        if (
            not has_border
            or not border_sides
            or any(
                side not in {"left", "right", "top", "bottom"} for side in border_sides
            )
        ):
            return InputFailure(
                "schema_conflict",
                "테두리 변과 적용할 테두리 서식을 함께 지정해야 합니다",
            )
    if ("fill_color" in parameters and fill is None) or border_color is None:
        return InputFailure("schema_conflict", "셀 색상 값이 올바르지 않습니다")
    if border_style not in _BORDER_STYLES:
        return InputFailure("schema_conflict", "셀 테두리 종류가 올바르지 않습니다")
    if border_width is None:
        return InputFailure("schema_conflict", "셀 테두리 두께가 올바르지 않습니다")
    has_format = (
        bool(format_parameters)
        or row_height is not None
        or column_width is not None
        or "vertical_alignment" in parameters
        or fill is not None
        or padding is not None
        or has_border
    )
    if not has_format:
        return InputFailure(
            "needs_input",
            "적용할 셀 서식이 필요합니다",
            ("inputs.parameters.fill_color", "inputs.parameters.border_width"),
        )
    borders = None
    if has_border:
        border = CellBorder(style=border_style, width=border_width, color=border_color)
        borders = CellBorders.model_validate({side: border for side in border_sides})
    return TableFormatSpec(
        cell,
        TableCell(
            bold=text.bold,
            font_name=text.font_name,
            font_size_pt=text.font_size_pt,
            text_color=text.text_color,
            alignment=text.alignment,
            vertical_alignment=vertical,
            line_spacing_percent=text.line_spacing,
            fill_color=fill,
            padding=padding,
            borders=borders,
        ),
        row_height,
        column_width,
        cells,
    )


def parse_merge(
    parameters: Mapping[str, OperationInputValue],
) -> MergeSpec | InputFailure:
    unknown = _unknown(
        parameters, frozenset(("start", "end")), "이 경로는 확정된 표의 셀만 병합하고"
    )
    if unknown is not None:
        return unknown
    start, end = _address(parameters.get("start")), _address(parameters.get("end"))
    # These messages state only what could not be determined. A message that
    # tells the caller to supply a cell is what made models invent addresses
    # like A1 or F3 and merge the wrong cells.
    if start is None or end is None:
        return InputFailure(
            "needs_input",
            "병합할 시작·끝 셀을 현재 선택에서도 전달된 입력에서도 확인하지 못했습니다",
            ("inputs.parameters.start", "inputs.parameters.end"),
        )
    # Two opposite corners of one rectangle may arrive in any order. Which
    # rectangle they span depends on the row_span/column_span of the addressed
    # cells, so the order is normalized against the live CellTopology in
    # hwp_live_native_format_recipe._normalized_merge_plan, never here.
    return MergeSpec(start, end)


def parse_split(
    parameters: Mapping[str, OperationInputValue],
) -> SplitSpec | InputFailure:
    allowed = frozenset(
        ("cell", "columns", "rows", "distribute_height", "merge", "split_mode")
    )
    unknown = _unknown(parameters, allowed, "이 경로는 확정된 표의 셀만 나누고")
    if unknown is not None:
        return unknown
    cell = _address(parameters.get("cell"))
    columns = _integer(parameters.get("columns"), 1, 65_535)
    rows = _integer(parameters.get("rows"), 1, 65_535)
    if cell is None or columns is None or rows is None:
        return InputFailure(
            "needs_input",
            "나눌 셀과 칸·줄 수를 현재 선택에서도 전달된 입력에서도 확인하지 못했습니다",
            (
                "inputs.parameters.cell",
                "inputs.parameters.columns",
                "inputs.parameters.rows",
            ),
        )
    if columns == 1 and rows == 1:
        return InputFailure(
            "schema_conflict", "셀 나누기는 칸 또는 줄 수가 2 이상이어야 합니다"
        )
    flags: list[bool] = []
    for name, default in (("distribute_height", False), ("merge", False)):
        parsed = default if name not in parameters else _boolean(parameters.get(name))
        if parsed is None:
            return InputFailure(
                "schema_conflict", f"inputs.parameters.{name} 값은 boolean이어야 합니다"
            )
        flags.append(parsed)
    # 기본값은 equal 이다. existing_grid 로 바꾸면 안 된다.
    # existing_grid 는 Mode2=1 을 보내는데, 한컴 공식 ParameterSetTable 149쪽의
    # Mode2 는 "셀 나누기 모드 2, 셀 나누기를 할 때 adjust를 생략하고 셀이
    # 어긋나는 것을 방지한다"이지 "표 격자를 늘리지 않는다"가 아니다.
    # 세로로 나누면 한/글 표의 전역 격자에 세로선이 생기는 것은 두 모드 모두
    # 같고, 사람이 UI에서 나눠도 같다. existing_grid 는 병합 셀이 감춘
    # row_span×column_span 격자를 그대로 되살릴 때만 쓰는 모드라서
    # verify_split_preflight 가 대상 셀의 span과 정확히 같기를 요구한다.
    # 기본값으로 두면 평범한 1×1 셀 나누기가 전부 schema_conflict 로 거절된다.
    split_mode = normalize_format_name_input(parameters.get("split_mode", "equal"))
    if split_mode not in ("equal", "existing_grid"):
        return InputFailure(
            "schema_conflict",
            "inputs.parameters.split_mode 값은 equal 또는 existing_grid여야 합니다",
        )
    return SplitSpec(cell, columns, rows, flags[0], flags[1], split_mode)

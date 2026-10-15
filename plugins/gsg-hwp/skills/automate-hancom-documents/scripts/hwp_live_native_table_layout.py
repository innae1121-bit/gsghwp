from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from hwp_errors import HwpLiveError
from hwp_live_native_action_models import (
    CaptionCommand,
    CellCommand,
    InsertPictureCommand,
    InsertTextCommand,
    IntegerValue,
    LeaveTableCommand,
    MergeCommand,
    MillimeterValue,
    NativeActionCommand,
    NativeCharacterFormat,
    NativeParagraphFormat,
    NativePosition,
    NativeSetter,
    ParameterActionCommand,
    RunCommand,
)
from hwp_live_native_layout_format import cell_format_commands
from hwp_live_native_text_format import (
    ParagraphFormatting,
    character_command,
    lead_after_mm,
    lead_before_mm,
    paragraph_command,
    plan_lead_line_spacing,
    style_command,
)
from hwp_live_table_contract import TableBlock, TableCell, TableMerge
from hwp_table_address import cell_address


def _style_id(
    styles: Mapping[str, int],
    name: str | None,
    explicit: int | None,
    label: str,
) -> int:
    if explicit is not None:
        return explicit
    if name is None:
        raise HwpLiveError(f"{label} 스타일이 자동 해석되지 않았습니다")
    try:
        return styles[name]
    except KeyError as error:
        raise HwpLiveError(f"현재 문서에 {label} 스타일 '{name}'이 없습니다") from error


def _table_create(
    block: TableBlock,
    observed_width_mm: float | None,
) -> ParameterActionCommand:
    rows = len(block.rows)
    columns = len(block.rows[0])
    setters = [
        NativeSetter("Rows", IntegerValue(rows)),
        NativeSetter("Cols", IntegerValue(columns)),
    ]
    creation_width_mm = (
        sum(block.column_widths_mm)
        if block.column_widths_mm is not None
        else observed_width_mm
    )
    if creation_width_mm is not None:
        setters.extend(
            (
                NativeSetter("WidthType", IntegerValue(2)),
                NativeSetter("WidthValue", MillimeterValue(creation_width_mm)),
            )
        )
    if block.row_heights_mm is not None:
        setters.extend(
            (
                NativeSetter("HeightType", IntegerValue(0)),
                NativeSetter(
                    "HeightValue",
                    MillimeterValue(sum(block.row_heights_mm)),
                ),
            )
        )
    return ParameterActionCommand(
        action="TableCreate",
        parameter_set="HTableCreation",
        setters=tuple(setters),
    )


# ShapeObjAttachCaption 은 캡션 모양을 문서가 아니라 한/글 프로세스에서
# 물려받는다. 실측(artifacts/live-defects/defect2-sticky): 새 프로세스의 표
# 캡션 기본값은 Side=3(아래)·Width=8504·Gap=850·CapFullSize=0 인데, 표 A 의
# 캡션을 왼쪽·폭 6mm 로 바꾼 뒤 새로 만든 표 B 에 캡션을 붙이자 B 도
# Side=0·Width=1700 으로 나왔다. 즉 그 프로세스에서 사람이 마지막으로 만진
# 캡션 설정이 그대로 따라붙는다. 옆쪽·좁은 설정이 남아 있으면 캡션 글자는
# 문서 모델에는 있는데 렌더에서는 잘려 사라진다.
#
# 그래서 붙인 직후에 위치 하나만 명시한다. 딱 하나인 이유는 실측이다
# (artifacts/live-defects/defect2-minimal-pin): 프로세스를 Side=0·Width=1700
# 으로 오염시킨 뒤 Side=3 만 고정한 캡션과, Side=3·CapFullSize=1 을 함께 고정한
# 캡션을 나란히 렌더했더니 둘 다 표 아래에 문장 전체가 보였다. 아래쪽 캡션에서
# Width 는 쓰이지 않으므로 CapFullSize 는 가시성에 아무것도 더하지 않는다.
#
# 값의 출처: Side=3 은 우리가 정한 관례가 아니라 새 프로세스가 주는 한/글
# 자신의 표 캡션 기본 위치다(위 A_fresh_default). 문서에 이미 있는 캡션의
# 위치를 관측해 따라가는 편이 더 낫지만, 지금 읽기 경로(LiveInspection
# ReadTableCaption)는 Side/Width 를 내주지 않는다.
#
# 남는 자국: 이 명령은 이 한/글 프로세스의 "다음 캡션 기본 위치"도 Side=3 으로
# 바꾼다(실측 C_inherited_after_restore). 선택 없이 원복하는 길은 없다 —
# TablePropertyDialog 를 선택 없이 Execute 하면 True 를 돌려주면서 아무것도
# 바꾸지 않는다(defect2-restore). 그래서 자국을 지우는 대신 최소로 줄였다:
# 우리가 건드리는 항목은 Side 하나뿐이고, 그 값은 한/글 기본값과 같다.
# Width·Gap·CapFullSize 는 사용자가 두고 간 값 그대로 남는다.
_CAPTION_SIDE_BELOW = 3


def _caption_geometry_action() -> ParameterActionCommand:
    return ParameterActionCommand(
        action="TablePropertyDialog",
        parameter_set="HShapeObject",
        setters=(NativeSetter("ShapeCaption/Side", IntegerValue(_CAPTION_SIDE_BELOW)),),
    )


def _size_action(name: str, millimeters: float) -> ParameterActionCommand:
    return ParameterActionCommand(
        action="TablePropertyDialog",
        parameter_set="HShapeObject",
        setters=(
            NativeSetter("HSet/ShapeType", IntegerValue(3)),
            NativeSetter("HSet/ShapeCellSize", IntegerValue(1)),
            NativeSetter(f"ShapeTableCell/{name}", MillimeterValue(millimeters)),
        ),
    )


def _geometry_commands(
    block: TableBlock, *, include_columns: bool = True
) -> tuple[NativeActionCommand, ...]:
    commands: list[NativeActionCommand] = []
    # Geometry runs before any merge, so every axis has an unambiguous cell.
    # Select the whole axis once instead of walking every grid slot.
    if include_columns and block.column_widths_mm is not None:
        for column, width in enumerate(block.column_widths_mm):
            commands.extend(
                (
                    CellCommand(cell_address(0, column)),
                    RunCommand("TableCellBlockCol"),
                    _size_action("Width", width),
                    RunCommand("Cancel"),
                )
            )
    if block.row_heights_mm is not None:
        for row, height in enumerate(block.row_heights_mm):
            commands.extend(
                (
                    CellCommand(cell_address(row, 0)),
                    RunCommand("TableCellBlockRow"),
                    _size_action("Height", height),
                    RunCommand("Cancel"),
                )
            )
    return tuple(commands)


def _picture_command(
    cell: TableCell,
    assets: Mapping[Path, Path],
) -> InsertPictureCommand:
    image = cell.image_path
    width = cell.image_width_mm
    height = cell.image_height_mm
    if image is None or width is None or height is None:
        raise HwpLiveError("표 셀 그림의 경로 또는 배치 영역이 없습니다")
    try:
        asset = assets[image]
    except KeyError as error:
        raise HwpLiveError(f"표 셀 그림 파일이 준비되지 않았습니다: {image}") from error
    return InsertPictureCommand(path=asset, width_mm=width, height_mm=height)


def _merge_commands(block: TableBlock) -> tuple[NativeActionCommand, ...]:
    commands: list[NativeActionCommand] = []
    rows = len(block.rows)
    columns = len(block.rows[0])

    # 한/글의 셀 주소는 "그 행에 남은 셀의 순번"이 아니라 표에 아직 살아 있는
    # 격자선의 좌표다. 격자선 하나는 그것을 넘지 않는 셀이 한 칸이라도 남아
    # 있는 동안 살아 있고, 모든 칸이 그 선을 넘어버릴 때만 사라지면서 그 뒤의
    # 주소가 한 칸씩 당겨진다.
    #
    # 라이브 실측(2026-09-03, 등록된 0.5.176 브리지, 일회용 인스턴스에서
    # InspectStructure 로 매 단계 되읽음):
    #   6x4 표에서 A2:B2 만 합침 → 2행이 "A2[1x2] C2 D2 E2 F2".
    #     흡수된 B2 만 사라지고 C2 는 그대로 C2 다(주소가 당겨지지 않는다).
    #   이어서 A3:B3, C2:D2, C3:D3 를 합친 뒤 원본 좌표 C2:C3 로 세로를 닫음
    #     → 성공, "C2[2x2]". 원본 좌표가 정답이다.
    #   그 상태에서 코호트 밖 세로 병합을 원본 좌표 F2:F3 로 보냄 → 성공.
    #     같은 셀을 행-압축 좌표 D2:D3 로 보내면 한/글이
    #     "merge endpoints are not present in forward order" 로 거부한다
    #     (D2 는 C2 의 열스팬에 흡수되어 이미 없는 주소다). 이것이 실사용
    #     .xls 배치가 2,644 번째 명령에서 멈춘 그 거부다.
    #   4x4 표에서 A1:B4(표의 모든 행을 덮는 4x2) 를 합침 → "A1[4x1] B1 C1",
    #     즉 A|B 격자선이 통째로 사라져 원본 C·D 가 B·C 로 당겨진다. 이때는
    #     원본 좌표 C1:D1 이 거부되고 B1:C1 이 정답이다.
    #   6x4 표에서 2·3행의 마지막 독립 쌍까지 세로로 닫으면 두 행 사이 격자선이
    #     사라져 원본 4행이 3행으로 당겨진다(행 번호도 같은 규칙이다).
    #
    # 그래서 장부는 "원본 격자 칸 → 그 칸을 소유한 병합의 앵커"이고, 주소는
    # 살아 있는 격자선을 세어 만든다. 두 경로(코호트·비코호트)가 같은 장부를
    # 쓰므로 한 표 안에서 주소 체계가 섞이지 않는다.
    owners: list[list[tuple[int, int]]] = [
        [(row, column) for column in range(columns)] for row in range(rows)
    ]
    column_spanned = [0] * columns
    row_spanned = [0] * rows

    def _absorb(merge: TableMerge) -> None:
        anchor = (merge.row, merge.column)
        for row in range(merge.row, merge.row + merge.row_span):
            for column in range(merge.column, merge.column + merge.column_span):
                owners[row][column] = anchor
        for boundary in range(merge.column + 1, merge.column + merge.column_span):
            column_spanned[boundary] = sum(
                1
                for row in range(rows)
                if owners[row][boundary - 1] == owners[row][boundary]
            )
        for boundary in range(merge.row + 1, merge.row + merge.row_span):
            row_spanned[boundary] = sum(
                1
                for column in range(columns)
                if owners[boundary - 1][column] == owners[boundary][column]
            )

    def _live_address(row: int, column: int) -> str:
        if owners[row][column] != (row, column):
            raise HwpLiveError(
                f"표 병합 주소 계산이 이미 병합된 셀을 가리켰습니다: 행 {row + 1}, 열 {column + 1}"
            )
        letter_index = sum(
            1 for boundary in range(1, column + 1) if column_spanned[boundary] < rows
        )
        row_index = sum(
            1 for boundary in range(1, row + 1) if row_spanned[boundary] < columns
        )
        return cell_address(row_index, letter_index)

    # 행 구간이 겹치는 복합(가로+세로) 병합만 한/글의 직접 사각형 경로가
    # 불안정하다. 그런 구간은 모든 행을 우→좌로 먼저 합친 뒤 세로 앵커를
    # 우→좌로 닫는다. 고립 사각형은 기존 직접 병합을 유지해 토폴로지 재탐색과
    # 배치 수를 늘리지 않는다.
    compound_entries = sorted(
        (
            (index, merge)
            for index, merge in enumerate(block.merges)
            if merge.row_span > 1 and merge.column_span > 1
        ),
        key=lambda item: (
            item[1].row,
            item[1].row + item[1].row_span - 1,
            item[1].column,
        ),
    )
    cohorts: list[tuple[tuple[int, TableMerge], ...]] = []
    component: list[tuple[int, TableMerge]] = []
    component_end = -1
    for entry in compound_entries:
        merge = entry[1]
        if component and merge.row > component_end:
            if len(component) > 1:
                cohorts.append(tuple(component))
            component = []
        component.append(entry)
        component_end = max(component_end, merge.row + merge.row_span - 1)
    if len(component) > 1:
        cohorts.append(tuple(component))
    staged_indexes = {index for cohort in cohorts for index, _merge in cohort}

    for row in range(rows):
        for cohort in (item for item in cohorts if item[0][1].row == row):
            last_row = max(merge.row + merge.row_span - 1 for _index, merge in cohort)
            for horizontal_row in range(row, last_row + 1):
                horizontal_merges = (
                    merge
                    for _index, merge in cohort
                    if merge.row <= horizontal_row < merge.row + merge.row_span
                )
                for merge in sorted(
                    horizontal_merges,
                    key=lambda item: -item.column,
                ):
                    anchor = _live_address(horizontal_row, merge.column)
                    endpoint = _live_address(
                        horizontal_row,
                        merge.column + merge.column_span - 1,
                    )
                    commands.extend(
                        (MergeCommand(anchor, endpoint), CellCommand(anchor))
                    )
                    # 코호트가 쪼갠 가로 병합도 실제 표를 바꾼다. 장부를 여기서
                    # 갱신하지 않으면 같은 코호트의 세로 앵커와 그 뒤 비-코호트
                    # 병합이 이미 흡수된 주소를 다시 발행한다.
                    _absorb(
                        TableMerge(
                            row=horizontal_row,
                            column=merge.column,
                            row_span=1,
                            column_span=merge.column_span,
                        )
                    )
            for merge in sorted(
                (merge for _index, merge in cohort),
                key=lambda item: (-item.column, item.row),
            ):
                anchor = _live_address(merge.row, merge.column)
                endpoint = _live_address(
                    merge.row + merge.row_span - 1,
                    merge.column,
                )
                commands.extend((MergeCommand(anchor, endpoint), CellCommand(anchor)))
                commands.extend(
                    cell_format_commands(block.rows[merge.row][merge.column])
                )
                _absorb(merge)

        row_merges = (
            merge
            for index, merge in enumerate(block.merges)
            if index not in staged_indexes and merge.row == row
        )
        for merge in sorted(
            row_merges,
            key=lambda item: (
                0 if item.row_span > 1 else 1,
                item.column if item.row_span > 1 else -item.column,
            ),
        ):
            anchor = _live_address(merge.row, merge.column)
            endpoint = _live_address(
                merge.row + merge.row_span - 1,
                merge.column + merge.column_span - 1,
            )
            commands.extend((MergeCommand(anchor, endpoint), CellCommand(anchor)))
            commands.extend(cell_format_commands(block.rows[merge.row][merge.column]))
            _absorb(merge)
    return tuple(commands)


def _cell_paragraph_commands(
    paragraph: object,
) -> tuple[NativeActionCommand, ...]:
    # ParagraphBlock is shared with the top-level layout contract. Keeping the
    # same run model inside cells makes every source run observable and
    # executable instead of collapsing a cell to one InsertText command.
    from hwp_live_layout_contract import ParagraphBlock  # noqa: PLC0415

    if not isinstance(paragraph, ParagraphBlock):
        raise HwpLiveError("표 셀 문단 형식이 올바르지 않습니다")
    commands: list[NativeActionCommand] = []
    if paragraph.style_id is not None:
        commands.append(style_command(paragraph.style_id))
    character = character_command(paragraph)
    if character is not None:
        commands.append(character)
    paragraph_shape = paragraph_command(
        ParagraphFormatting(
            alignment=paragraph.alignment,
            align_type_raw=paragraph.align_type_raw,
            line_spacing=paragraph.line_spacing_percent,
            before_mm=lead_before_mm(
                paragraph.space_before_mm,
                paragraph.plan_lead_mm,
            ),
            after_mm=lead_after_mm(
                paragraph.space_after_mm,
                paragraph.plan_trail_mm,
            ),
            left_mm=paragraph.left_margin_mm,
            right_mm=paragraph.right_margin_mm,
            indentation_mm=paragraph.indentation_mm,
            heading_type=paragraph.heading_type,
            heading_level=paragraph.heading_level,
        )
    )
    if paragraph_shape is not None:
        commands.append(paragraph_shape)
    if paragraph.runs:
        for run in paragraph.runs:
            run_character = character_command(run)
            if run_character is not None:
                commands.append(run_character)
            commands.append(InsertTextCommand(run.text))
    else:
        commands.append(InsertTextCommand(paragraph.text))
    return tuple(commands)


def table_commands(
    block: TableBlock,
    assets: Mapping[Path, Path],
    styles: Mapping[str, int],
    text_formats: Mapping[str, tuple[NativeCharacterFormat, NativeParagraphFormat]],
    caption_format_sources: Mapping[str, NativePosition],
    observed_width_mm: float | None = None,
) -> tuple[NativeActionCommand, ...]:
    base_style = _style_id(
        styles,
        block.base_style_name,
        block.base_style_id,
        "표 기준",
    )
    commands: list[NativeActionCommand] = [style_command(base_style)]
    paragraph_fields = {
        "alignment",
        "left_margin_mm",
        "right_margin_mm",
        "indentation_mm",
        "plan_lead_mm",
        "plan_trail_mm",
    }
    explicit_paragraph_fields = paragraph_fields.intersection(block.model_fields_set)
    paragraph = paragraph_command(
        ParagraphFormatting(
            # Applying a base style does not clear inherited numbering.
            # The table anchor is a body paragraph; captions own their numbering.
            heading_type=0,
            alignment=(
                block.alignment
                if "alignment" in explicit_paragraph_fields
                else "inherit"
            ),
            left_mm=(
                block.left_margin_mm
                if "left_margin_mm" in explicit_paragraph_fields
                else None
            ),
            right_mm=(
                block.right_margin_mm
                if "right_margin_mm" in explicit_paragraph_fields
                else None
            ),
            indentation_mm=(
                block.indentation_mm
                if "indentation_mm" in explicit_paragraph_fields
                else None
            ),
            before_mm=(
                block.plan_lead_mm
                if "plan_lead_mm" in explicit_paragraph_fields
                else None
            ),
            after_mm=(
                block.plan_trail_mm
                if "plan_trail_mm" in explicit_paragraph_fields
                else None
            ),
            line_spacing=(
                plan_lead_line_spacing(block.plan_lead_mm)
                if "plan_lead_mm" in explicit_paragraph_fields
                else None
            ),
        )
    )
    if paragraph is not None:
        commands.append(paragraph)
    commands.append(_table_create(block, observed_width_mm))
    # repeat_header 는 의도적으로 적용하지 않는다.
    # 라이브 검증(LV9)에서 45x4 표 3/3 회 모두 2쪽이 완전히 비고 데이터 행이 렌더되지 않았다.
    # 원인 추정: 다른 TablePropertyDialog 명령이 함께 보내는 문맥 setter
    # (HSet/ShapeType, HSet/ShapeCellSize) 없이 ShapeTableCell/Header 만 보내
    # 표 속성이 어긋난다. 문맥을 갖춘 형태로 다시 만들기 전까지는 적용하지 않는다.
    commands.extend(_geometry_commands(block))
    for row, cells in enumerate(block.rows):
        for column, cell in enumerate(cells):
            commands.extend(
                (CellCommand(cell_address(row, column)), style_command(base_style))
            )
            commands.extend(cell_format_commands(cell))
            if cell.paragraphs:
                for paragraph_index, paragraph in enumerate(cell.paragraphs):
                    commands.extend(_cell_paragraph_commands(paragraph))
                    if paragraph_index + 1 < len(cell.paragraphs):
                        commands.append(RunCommand("BreakPara"))
            elif cell.text:
                for line_index, line in enumerate(cell.text.split("\n")):
                    if line_index:
                        commands.append(RunCommand("BreakPara"))
                    if line:
                        commands.append(InsertTextCommand(line))
            if cell.image_path is not None:
                if cell.text:
                    commands.append(RunCommand("BreakPara"))
                commands.append(_picture_command(cell, assets))
                # InsertPicture resets cell margin read-back to zero on the live
                # HWP build. Reselect the owning cell and replay the caller's
                # selected appearance after insertion; otherwise a succeeded
                # request loses the observed picture-frame padding.
                commands.append(CellCommand(cell_address(row, column)))
                commands.extend(cell_format_commands(cell))
    # 셀을 다 채운 뒤 계획한 기하를 한 번 더 보낸다. 한/글의 내용 맞춤은 셀에
    # 들어간 글이 계획 높이보다 크면 행을 늘리므로, 채우기 전에 한 번만 보낸
    # row_heights_mm 는 채우는 동안 덮여 행 비율이 어긋난다.
    # 1630b69 의 라이브 A/B 는 이 재적용을 지우면서 "재적용이 무효"라고 했지만
    # 그 측정 대상은 2x3 그림 표였고(그림은 정해진 크기라 내용 맞춤이 움직일
    # 것이 없다) 커밋 본문도 그 workload 밖은 측정하지 않았다고 적었다. 그래서
    # 그 실측이 no-op 이라고 증명한 그림 전용 표에서는 계속 건너뛰고, 내용
    # 맞춤이 실제로 동작하는 글이 든 표에서만 되돌린다.
    if block.row_heights_mm is not None and any(
        cell.text or cell.paragraphs for cells in block.rows for cell in cells
    ):
        commands.extend(_geometry_commands(block, include_columns=False))
    commands.extend(_merge_commands(block))
    if block.caption is not None:
        caption_style = _style_id(
            styles,
            block.caption_style_name,
            block.caption_style_id,
            "표 캡션",
        )
        caption_format = text_formats.get(block.caption_style_name or "")
        format_source = caption_format_sources.get(block.caption_style_name or "")
        if caption_format is None:
            commands.append(
                CaptionCommand(
                    caption_style,
                    block.caption,
                    format_source=format_source,
                )
            )
        else:
            character, paragraph = caption_format
            commands.append(
                CaptionCommand(
                    caption_style,
                    block.caption,
                    character,
                    paragraph,
                    format_source,
                )
            )
        # 네이티브 AttachCaption 은 마지막에 표를 다시 선택한 채로 끝난다
        # (ActionImageCaption.cpp:538). 그 선택 위에서 캡션 모양을 못 박는다.
        commands.append(_caption_geometry_action())
        if format_source is not None:
            # Replace the whole generated caption text. Its length and language
            # are HWP/document facts; copied numbering stays in the paragraph
            # shape rather than in the selected text.
            commands.extend(
                (
                    RunCommand("ShapeObjAttachCaption"),
                    RunCommand("MoveParaBegin"),
                    RunCommand("MoveSelParaEnd"),
                    InsertTextCommand(block.caption),
                    RunCommand("CloseEx"),
                )
            )
    commands.append(LeaveTableCommand())
    return tuple(commands)

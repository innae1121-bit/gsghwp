from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, cast

from pydantic import JsonValue

from hwp_document_convention_axis import build_axis
from hwp_errors import HwpLiveError
from hwp_live_structure_contract import CellFormatObservation, DocumentStructure
from hwp_live_table_contract import TableBlock, TableCell, TableMerge
from hwp_live_values import ALIGNMENT_VALUES, Alignment, Rgb
from hwp_table_readability import recommended_row_heights


def _fit_weights(
    weights: tuple[float, ...],
    total_mm: float,
    minimums: tuple[float, ...] | None = None,
) -> tuple[float, ...]:
    # TableBlock's public geometry contract requires every explicit width to
    # remain at least 1 mm. This is a protocol invariant, not a visual default.
    floors = minimums or tuple(1.0 for _ in weights)
    if len(floors) != len(weights):
        raise HwpLiveError("표의 열 너비와 최소 가독 폭 개수가 일치하지 않습니다")
    if total_mm + 0.0001 < sum(floors):
        raise HwpLiveError(
            "현재 문서 본문 폭으로는 표의 최소 가독 폭을 확보할 수 없습니다"
        )
    fitted = [0.0] * len(weights)
    active = set(range(len(weights)))
    remaining = total_mm
    while active:
        active_total = sum(weights[index] for index in active)
        scaled = {index: remaining * weights[index] / active_total for index in active}
        narrow = {index for index, width in scaled.items() if width < floors[index]}
        if not narrow:
            for index, width in scaled.items():
                fitted[index] = width
            break
        for index in narrow:
            fitted[index] = floors[index]
        active.difference_update(narrow)
        remaining = total_mm - sum(fitted)
    correction = total_mm - sum(fitted)
    fitted[max(range(len(fitted)), key=fitted.__getitem__)] += correction
    return tuple(round(width, 4) for width in fitted)


def _slice_values(
    values: tuple[float, ...] | None,
    columns: tuple[int, ...],
) -> tuple[float, ...] | None:
    if values is None:
        return None
    return tuple(values[column] for column in columns)


def _lift_full_width_title(block: TableBlock) -> TableBlock:
    if len(block.rows) < 2:
        # 유일한 행이 전폭 제목이면 승격 후 남는 표 몸통이 없다. 그대로
        # 두면 병합 분할 경로가 절단선 없음으로 거부해 안내한다.
        return block
    columns = len(block.rows[0])
    title_merge = next(
        (
            merge
            for merge in block.merges
            if merge.row == 0
            and merge.column == 0
            and merge.row_span == 1
            and merge.column_span == columns
        ),
        None,
    )
    if title_merge is None:
        return block
    title = block.rows[0][0].text.strip()
    if not title:
        return block
    if block.caption is not None and block.caption.strip() != title:
        raise HwpLiveError(
            "캡션과 전폭 제목 행이 서로 다른 넓은 표는 자동 분할하지 않습니다. "
            "분할하려면 제목 행을 캡션으로 승격해야 하는데 캡션이 이미 다른 "
            "텍스트를 담고 있어 제목이 소실됩니다 — 제목을 캡션에 합치거나 "
            "표를 의미 단위로 직접 나누어야 합니다"
        )
    heights = block.row_heights_mm[1:] if block.row_heights_mm is not None else None
    return block.model_copy(
        update={
            "caption": block.caption or title,
            "rows": block.rows[1:],
            "row_heights_mm": heights,
            "merges": tuple(
                merge.model_copy(update={"row": merge.row - 1})
                for merge in block.merges
                if merge.row > 0
            ),
        }
    )


def _safe_cut_after(block: TableBlock) -> tuple[bool, ...]:
    columns = len(block.rows[0])
    if columns < 2:
        return ()
    can_cut = [True] * (columns - 1)
    for merge in block.merges:
        for column in range(merge.column, merge.column + merge.column_span - 1):
            if 0 <= column < columns - 1:
                can_cut[column] = False
    return tuple(can_cut)


def _column_segments(block: TableBlock) -> tuple[tuple[int, int], ...]:
    columns = len(block.rows[0])
    cuts = _safe_cut_after(block)
    segments: list[tuple[int, int]] = []
    start = 0
    for column, allowed in enumerate(cuts):
        if allowed:
            segments.append((start, column))
            start = column + 1
    segments.append((start, columns - 1))
    return tuple(segments)


def _flatten_segments(
    segments: tuple[tuple[int, int], ...],
) -> tuple[int, ...]:
    columns: list[int] = []
    for start, end in segments:
        columns.extend(range(start, end + 1))
    return tuple(columns)


def _slice_merges(
    block: TableBlock, columns: tuple[int, ...]
) -> tuple[TableMerge, ...]:
    index_of = {column: index for index, column in enumerate(columns)}
    selected = set(columns)
    sliced: list[TableMerge] = []
    for merge in block.merges:
        covered = [
            column
            for column in range(merge.column, merge.column + merge.column_span)
            if column in selected
        ]
        if not covered:
            continue
        new_columns = [index_of[column] for column in covered]
        if new_columns != list(range(min(new_columns), max(new_columns) + 1)):
            continue
        sliced.append(
            merge.model_copy(
                update={
                    "column": min(new_columns),
                    "column_span": len(new_columns),
                }
            )
        )
    return tuple(sliced)


def _part_from_columns(block: TableBlock, columns: tuple[int, ...]) -> TableBlock:
    return block.model_copy(
        update={
            "rows": tuple(
                tuple(row[column] for column in columns) for row in block.rows
            ),
            "column_widths_mm": _slice_values(block.column_widths_mm, columns),
            "column_width_weights": _slice_values(block.column_width_weights, columns),
            "minimum_column_widths_mm": _slice_values(
                block.minimum_column_widths_mm, columns
            ),
            "merges": _slice_merges(block, columns),
            "split_wide_table": False,
            "repeat_key_columns": 0,
        }
    )


def _split_merged_at_safe_cuts(
    block: TableBlock,
    available_mm: float,
    minimums: tuple[float, ...],
) -> tuple[TableBlock, ...]:
    segments = _column_segments(block)
    if len(segments) < 2:
        raise HwpLiveError(
            "병합 셀이 있는 넓은 표는 자동 압축하지 않습니다. 원본 병합 구역을 보존해 의미 단위 범위로 나누어야 합니다"
        )

    def segment_width(start: int, end: int) -> float:
        return sum(minimums[start : end + 1])

    key = (segments[0],)
    key_width = segment_width(*segments[0])
    groups: list[tuple[tuple[int, int], ...]] = []
    current = key
    current_width = key_width
    for segment in segments[1:]:
        extra = segment_width(*segment)
        if len(current) > 1 and current_width + extra > available_mm + 0.0001:
            groups.append(current)
            current = (*key, segment)
            current_width = key_width + extra
        else:
            current = (*current, segment)
            current_width += extra
        if current_width > available_mm + 0.0001 and len(current) == 2:
            raise HwpLiveError(
                "반복 식별 열과 데이터 열 하나도 최소 가독 폭으로 배치할 수 없습니다"
            )
    if len(current) > 1:
        groups.append(current)
    if len(groups) < 2:
        return (block,)
    return tuple(
        _part_from_columns(block, _flatten_segments(group)) for group in groups
    )


def split_wide_table(
    block: TableBlock,
    available_mm: float,
) -> tuple[TableBlock, ...]:
    minimums = block.minimum_column_widths_mm
    if minimums is None or sum(minimums) <= available_mm + 0.0001:
        return (block,)
    if not block.split_wide_table:
        raise HwpLiveError(
            "표 가독성을 확보할 수 없을 만큼 열이 좁아집니다. 의미 단위로 열을 나누어야 합니다"
        )
    # 전폭 제목 행은 열을 가로지르므로 실제로 쪼갤 때에만 캡션으로 올린다.
    # 쪼갤 필요가 없는 표에서 올리면 제목 행과 그 테두리가 통째로 사라진다.
    lifted = _lift_full_width_title(block)
    if lifted.merges:
        return _split_merged_at_safe_cuts(lifted, available_mm, minimums)
    columns = len(lifted.rows[0])
    key_count = lifted.repeat_key_columns
    key_columns = tuple(range(key_count))
    key_width = sum(minimums[column] for column in key_columns)
    groups: list[tuple[int, ...]] = []
    current = list(key_columns)
    current_width = key_width
    for column in range(key_count, columns):
        width = minimums[column]
        if current[key_count:] and current_width + width > available_mm + 0.0001:
            groups.append(tuple(current))
            current = list(key_columns)
            current_width = key_width
        if current_width + width > available_mm + 0.0001:
            raise HwpLiveError(
                "반복 식별 열과 데이터 열 하나도 최소 가독 폭으로 배치할 수 없습니다"
            )
        current.append(column)
        current_width += width
    if current[key_count:]:
        groups.append(tuple(current))
    if len(groups) < 2:
        return (lifted,)
    return tuple(_part_from_columns(lifted, group) for group in groups)


def resolve_table_geometry(block: TableBlock, content_width_mm: float) -> TableBlock:
    available = content_width_mm - block.left_margin_mm - block.right_margin_mm
    weighted_width = (
        available
        if block.target_width_mm is None
        else min(block.target_width_mm, available)
    )
    minimums = block.minimum_column_widths_mm
    widths = block.column_widths_mm
    if widths is None:
        weights = block.column_width_weights
        if weights is None:
            return block
        widths = _fit_weights(weights, weighted_width, minimums)
    elif sum(widths) > available:
        widths = _fit_weights(widths, available, minimums)
    updates: dict[str, tuple[float, ...] | None] = {
        "column_widths_mm": widths,
        "column_width_weights": None,
    }
    if block.auto_fit_row_heights:
        text_rows = tuple(tuple(cell.text for cell in row) for row in block.rows)
        updates["row_heights_mm"] = recommended_row_heights(text_rows, widths)
    return block.model_copy(update=updates)


@dataclass(frozen=True, slots=True)
class ObservedTableConventions:
    """Cell appearance every sampled cell of the observed document agreed on.

    This is the table half of ``hwp_document_style_profile._replicated_updates``
    and follows the same three rules: a value the caller set is never touched,
    a value nobody observed is never invented, and nothing here is policy --
    every field is a number the document itself reported.

    Where it is deliberately *stricter* than the paragraph path, and why. The
    paragraph path folds by majority (``_present_majority``) because it groups
    paragraphs by style first, so the paragraphs it averages already share a
    role. Cell appearance has no such grouping: the bridge samples at most four
    cells per table chosen by position alone -- first row first column, first
    row last column, second row first column, last row first column -- and
    ``CellFormatObservation`` says in as many words that nothing there asserts
    which row is a header. Two sampled cells that disagree are therefore a
    header and a body cell, not a convention, and a majority of four would
    happily paint a whole table in the header's font. So an axis has to be
    unanimous, which is exactly ``ConventionAxis.state == "observed"`` -- the
    same verdict the public convention profile prints for the same document,
    computed by the same ``build_axis``.

    ``None`` means "nothing to replay", never "the document has none".
    """

    font_name: str | None = None
    font_size_pt: float | None = None
    bold: bool | None = None
    alignment: Alignment | None = None
    fill_color: Rgb | None = None

    @property
    def empty(self) -> bool:
        return all(
            value is None
            for value in (
                self.font_name,
                self.font_size_pt,
                self.bold,
                self.alignment,
                self.fill_color,
            )
        )


EMPTY_TABLE_CONVENTIONS: Final = ObservedTableConventions()

# One cell that answered while its neighbours could not is not a convention,
# it is the only cell that answered. Unreadable properties abstain rather than
# vote (``_present_majority``'s rule, kept here), so without a floor a single
# reading carries an axis alone: a page whose four sampled cells reported
# ``fill_brush`` as None, None, None, 1 made every cell of the next generated
# table grey. ``_dominant_lead_prefix`` settled the same question for
# paragraphs -- "a prefix is evidence of a convention only once two paragraphs
# agree on it" -- and this is that floor, for the same reason.
_MINIMUM_AGREEING_SAMPLES: Final = 2


def _unanimous(observations: Iterable[tuple[JsonValue, int]]) -> JsonValue | None:
    """The one value at least two observations agreed on, or ``None``.

    ``build_axis`` is the profile's own fold, so "observed" here and
    "observed" in ``hwp_document_convention_structure`` cannot drift apart.
    An axis with no observation at all is ``unobserved``; one with two values
    is ``observed-but-conflicting``. Only the first state may be replayed, and
    only once ``_MINIMUM_AGREEING_SAMPLES`` cells stand behind it.
    """
    axis = build_axis("table_cell_convention", observations)
    if axis.state != "observed" or axis.sample_count < _MINIMUM_AGREEING_SAMPLES:
        return None
    return axis.selected_value


def _sampled_cells(
    structure: DocumentStructure,
) -> tuple[tuple[CellFormatObservation, int], ...]:
    return tuple(
        (observed, table.anchor.paragraph)
        for table in structure.tables
        for observed in table.cell_formats
    )


def _observed_font_name(
    samples: tuple[tuple[CellFormatObservation, int], ...],
) -> str | None:
    value = _unanimous(
        (cast(JsonValue, observed.face_name), paragraph)
        for observed, paragraph in samples
        if observed.face_name
    )
    return value if isinstance(value, str) else None


def _observed_font_size_pt(
    samples: tuple[tuple[CellFormatObservation, int], ...],
) -> float | None:
    """``HCharShape/Height`` back as points, the exact inverse of the write.

    ``character_command`` sends ``Height = round(font_size_pt * 100)``, so the
    hundredths here are that same scale. The 1..96 bound is the contract's own
    (``TableCell.font_size_pt``); a reading outside it is not replayable and is
    left to the resolved style rather than clamped into range.
    """
    value = _unanimous(
        (cast(JsonValue, observed.character_height), paragraph)
        for observed, paragraph in samples
        if observed.character_height is not None
    )
    if not isinstance(value, int):
        return None
    size = value / 100
    return size if 1 <= size <= 96 else None


def _observed_bold(
    samples: tuple[tuple[CellFormatObservation, int], ...],
) -> bool | None:
    value = _unanimous(
        (cast(JsonValue, observed.bold), paragraph)
        for observed, paragraph in samples
        if observed.bold is not None
    )
    return value if isinstance(value, bool) else None


def _observed_alignment(
    samples: tuple[tuple[CellFormatObservation, int], ...],
) -> Alignment | None:
    """``HParaShape/AlignType`` of the sampled cells, named.

    The raw value is looked up by scanning ``ALIGNMENT_VALUES`` rather than
    through an inverse table, which is what ``_replicated_updates`` does for
    the paragraph path -- see the note above that mapping for why no inverse
    table lives in this repository. 배분(4) and 나눔(5) have no name in
    ``Alignment``; a document using them simply produces nothing here.
    """
    value = _unanimous(
        (cast(JsonValue, observed.alignment), paragraph)
        for observed, paragraph in samples
        if observed.alignment is not None
    )
    if not isinstance(value, int):
        return None
    return next(
        (
            name
            for name, raw_value in ALIGNMENT_VALUES.items()
            if name != "inherit" and raw_value == value
        ),
        None,
    )


def _observed_fill_color(
    samples: tuple[tuple[CellFormatObservation, int], ...],
) -> Rgb | None:
    """The cell face colour, only when every sampled cell is actually filled.

    ``fill_brush`` is ``FillAttr/WindowsBrush``, the published flag for
    "the selected brush is the face/pattern brush" -- the same item the write
    path sets to 1 when it fills a cell. A cell reporting 0 votes with its 0,
    so one shaded header among plain cells makes this axis conflicting instead
    of painting the whole table. The colour itself is ``0x00BBGGRR`` per the
    catalog, which is the exact inverse of ``rgb_value``.
    """
    value = _unanimous(
        (
            cast(
                JsonValue, {"brush": observed.fill_brush, "color": observed.fill_color}
            ),
            paragraph,
        )
        for observed, paragraph in samples
        if observed.fill_brush is not None
    )
    if not isinstance(value, dict):
        return None
    color = value.get("color")
    if value.get("brush") != 1 or not isinstance(color, int):
        return None
    if not 0 <= color <= 0xFF_FF_FF:
        return None
    return color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF


def observed_table_conventions(
    structure: DocumentStructure | None,
) -> ObservedTableConventions:
    """Fold the observed page's sampled cells into replayable cell values.

    Three observed axes are deliberately absent, each for its own reason.

    *Border line type and width, and cell vertical alignment.* They arrive as
    raw enumeration numbers (``HCellBorderFill/BorderType*``, ``BorderWidth*``,
    ``ShapeTableCell/VertAlign``) while ``CellBorder.style``/``width`` and
    ``TableCell.vertical_alignment`` accept only names. HWP itself resolves the
    name to a number at write time (``hwp.HwpLineType`` /
    ``hwp.HwpLineWidth``), and no measurement of the reverse direction exists
    anywhere in this repository -- ``TableInspection.cpp`` says the same about
    ``VertAlign``, which has no write-path counterpart here at all. Guessing
    the ordering would be exactly the kind of unmeasured raw-value table that
    ``hwp_live_values.ALIGNMENT_VALUES`` documents as having been wrong and
    undetected for months.

    *Cell margins.* These are readable and convertible, but ``TableCell``
    spells them as ``padding``, and merely having a padding anywhere in a table
    changes what ``hwp_live_native_layout._observed_table_width_mm`` does: it
    returns ``None`` -- dropping the table's observed creation width -- for any
    table whose padding cannot be collapsed into a rectangle, which includes
    every table carrying a merge. Filling a field nobody asked for must not
    silently change a table's width, and cell margins are not reported by any
    convention axis either, so there is nothing to carry.

    Everything left is reported to the model as well, through the block field
    named in ``ConventionAxis.write_fields``.
    """
    if structure is None:
        return EMPTY_TABLE_CONVENTIONS
    samples = _sampled_cells(structure)
    if not samples:
        return EMPTY_TABLE_CONVENTIONS
    return ObservedTableConventions(
        font_name=_observed_font_name(samples),
        font_size_pt=_observed_font_size_pt(samples),
        bold=_observed_bold(samples),
        alignment=_observed_alignment(samples),
        fill_color=_observed_fill_color(samples),
    )


def _replicated_cell(
    cell: TableCell,
    conventions: ObservedTableConventions,
) -> TableCell:
    updates: dict[str, object] = {}
    if cell.font_name is None and conventions.font_name is not None:
        updates["font_name"] = conventions.font_name
    if cell.font_size_pt is None and conventions.font_size_pt is not None:
        updates["font_size_pt"] = conventions.font_size_pt
    if cell.bold is None and conventions.bold is not None:
        updates["bold"] = conventions.bold
    if cell.alignment == "inherit" and conventions.alignment is not None:
        updates["alignment"] = conventions.alignment
    if cell.fill_color is None and conventions.fill_color is not None:
        updates["fill_color"] = conventions.fill_color
    return cell.model_copy(update=updates) if updates else cell


def replicate_table_conventions(
    block: TableBlock,
    conventions: ObservedTableConventions,
) -> TableBlock:
    """Fill only the cell fields this block left unset.

    Cells a merge covers are skipped rather than filled. ``TableBlock``
    requires them to stay empty and unformatted, and the native path never
    visits them after the merge, so formatting one would both invalidate the
    block and emit commands for a cell that no longer exists.
    """
    if conventions.empty:
        return block
    anchors = {(merge.row, merge.column) for merge in block.merges}
    covered = {
        (row, column)
        for merge in block.merges
        for row in range(merge.row, merge.row + merge.row_span)
        for column in range(merge.column, merge.column + merge.column_span)
    } - anchors
    rows = tuple(
        tuple(
            cell if (row, column) in covered else _replicated_cell(cell, conventions)
            for column, cell in enumerate(cells)
        )
        for row, cells in enumerate(block.rows)
    )
    changed = any(
        replaced is not original
        for replaced_row, original_row in zip(rows, block.rows, strict=True)
        for replaced, original in zip(replaced_row, original_row, strict=True)
    )
    return block.model_copy(update={"rows": rows}) if changed else block

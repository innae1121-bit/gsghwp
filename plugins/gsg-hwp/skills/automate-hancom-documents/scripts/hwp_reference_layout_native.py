from __future__ import annotations

from math import floor

from hwp_live_native_action_models import (
    IntegerValue,
    NativeSetter,
    ParameterActionCommand,
)
from hwp_reference_layout_contract import ReferenceLayoutBlock, TextAnchor
from hwp_reference_layout_gap import protected_gap_minimums
from hwp_reference_layout_geometry import (
    MINIMUM_HWP_GRID_INTERVAL,
    SectionPageGeometry,
)
from hwp_reference_layout_payload import (
    compile_reference_layout_payload,
    integer_array,
)
from hwp_reference_layout_placement import PlacementFrame
from hwp_reference_layout_text_fitting import (
    ReferenceTextFitReport,
    fit_reference_text_styles_with_report,
)
from hwp_reference_layout_text_flow import (
    ReferenceTextHeightError,
    minimum_text_height,
    style_for_text,
)


def _cell_span(
    block: ReferenceLayoutBlock,
    anchor: TextAnchor,
) -> tuple[int, int]:
    for merge in block.merges:
        if merge.row == anchor.row and merge.column == anchor.column:
            return merge.row_span, merge.column_span
    return 1, 1


def reserve_text_row_heights(
    block: ReferenceLayoutBlock,
    row_heights: tuple[int, ...],
    column_widths: tuple[int, ...],
) -> tuple[int, ...]:
    minimums = [MINIMUM_HWP_GRID_INTERVAL] * len(row_heights)
    for gap in block.protected_gaps:
        if gap.axis == "row":
            for row in range(gap.top, gap.bottom):
                minimums[row] = row_heights[row]
    for anchor in block.text_anchors:
        text_style, region_style = style_for_text(block, anchor)
        if text_style is None:
            continue
        row_span, column_span = _cell_span(block, anchor)
        available_width = sum(
            column_widths[anchor.column : anchor.column + column_span]
        )
        required = minimum_text_height(
            anchor,
            text_style,
            region_style,
            available_width,
        )
        current = sum(row_heights[anchor.row : anchor.row + row_span])
        if required > current:
            target = anchor.row + row_span - 1
            minimums[target] = max(
                minimums[target],
                row_heights[target] + required - current,
            )

    adjusted = [
        max(height, minimum)
        for height, minimum in zip(row_heights, minimums, strict=True)
    ]
    deficit = sum(adjusted) - sum(row_heights)
    if deficit == 0:
        return row_heights
    capacities = [
        max(0, height - minimum)
        for height, minimum in zip(row_heights, minimums, strict=True)
    ]
    capacity_total = sum(capacities)
    if capacity_total < deficit:
        raise ReferenceTextHeightError(
            sum(row_heights),
            sum(row_heights) + deficit - capacity_total,
        )
    exact = [deficit * capacity / capacity_total for capacity in capacities]
    deductions = [floor(value) for value in exact]
    remainder = deficit - sum(deductions)
    order = sorted(
        range(len(exact)),
        key=lambda index: (exact[index] - deductions[index], -index),
        reverse=True,
    )
    for index in order:
        if remainder == 0:
            break
        if deductions[index] < capacities[index]:
            deductions[index] += 1
            remainder -= 1
    return tuple(
        height - deduction
        for height, deduction in zip(adjusted, deductions, strict=True)
    )


def compile_reference_layout_command_with_fit_report(
    block: ReferenceLayoutBlock,
    page: SectionPageGeometry,
    *,
    page_number: int,
    base_style_id: int,
) -> tuple[ParameterActionCommand, ReferenceTextFitReport]:
    """명령과 함께, 텍스트 맞춤이 실제로 줄인 값을 돌려준다.

    ``fit_reference_text_styles`` 는 선언된 글자 크기·장평·자간·줄간격·여백을
    셀에 맞을 때까지 조용히 줄인다(최저 선언값의 75%). 그 결과는 명령 배열
    안의 서식 값으로만 남아, 호출한 쪽에서는 렌더를 보기 전까지 알 수 없었다.
    여기서 함께 돌려주는 보고가 그 공백을 메운다 -- 맞춤 동작 자체는 그대로다.
    """
    area = page.usable_area(page_number=page_number)
    frame = PlacementFrame.from_reference(
        area,
        block.source_image,
        rows=len(block.row_breakpoints) - 1,
        columns=len(block.column_breakpoints) - 1,
        visible_edges=block.visible_edges,
        styles=block.styles,
        style_regions=block.style_regions,
    )
    columns = frame.map_columns(block.column_breakpoints)
    rows = frame.map_rows(block.row_breakpoints)
    fitted_block, fit_report = fit_reference_text_styles_with_report(
        block,
        rows.sizes,
        columns.sizes,
    )
    row_heights = reserve_text_row_heights(
        fitted_block,
        rows.sizes,
        columns.sizes,
    )
    payload = compile_reference_layout_payload(fitted_block)
    column_array, column_values = integer_array(
        "ColumnWidths",
        columns.sizes,
    )
    row_array, row_values = integer_array("RowHeights", row_heights)
    gap_array, gap_values = integer_array(
        "GapMinimums",
        protected_gap_minimums(
            fitted_block.protected_gaps,
            rows.sizes,
            columns.sizes,
        ),
    )
    arrays = [column_array, row_array, gap_array, *payload.arrays]
    values = [
        *column_values,
        *row_values,
        *gap_values,
        *payload.values,
    ]

    return (
        ParameterActionCommand(
            action="ReferenceLayoutBulk",
            parameter_set="HTableCreation",
            setters=(
                NativeSetter("Rows", IntegerValue(len(rows.sizes))),
                NativeSetter("Columns", IntegerValue(len(columns.sizes))),
                NativeSetter("BaseStyleId", IntegerValue(base_style_id)),
                NativeSetter("BodyLeft", IntegerValue(frame.left)),
                NativeSetter("BodyTop", IntegerValue(frame.top)),
                NativeSetter("BodyWidth", IntegerValue(frame.width)),
                NativeSetter("BodyHeight", IntegerValue(frame.height)),
            ),
            arrays=tuple(array for array in arrays if array.count > 0),
            array_values=tuple(values),
        ),
        fit_report,
    )


def compile_reference_layout_command(
    block: ReferenceLayoutBlock,
    page: SectionPageGeometry,
    *,
    page_number: int,
    base_style_id: int,
) -> ParameterActionCommand:
    return compile_reference_layout_command_with_fit_report(
        block,
        page,
        page_number=page_number,
        base_style_id=base_style_id,
    )[0]

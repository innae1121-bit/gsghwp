from __future__ import annotations

from pathlib import Path
from typing import Final

from hwp_live_structure_contract import StructureCell, StructureTable
from hwp_live_native_template_repeat import caption_literal_text
from hwp_live_template_repeat import (
    TableTemplateRepeatPlan,
    TemplateImageCell,
    TemplateTableBlock,
    TemplateTextCell,
)
from hwp_visibility_series_contract import (
    IMAGE_HEIGHT_MM,
    IMAGE_WIDTH_MM,
    VisibilityRecord,
    VisibilitySeriesPlanError,
    VisibilitySlot,
)
from hwp_visibility_template_observation import (
    VisibilityTemplateCellObservation,
    VisibilityTemplateMismatch,
    VisibilityTemplateObservation,
    VisibilityTemplatePosition,
)
from hwp_visibility_source import extract_visibility_records, optional_numbered_image
from hwp_visibility_table_cells import (
    cell_at,
    compact_cell_text,
    logical_cells,
    owner_cells,
)


_SLOT_REMAINDER_SHAPES: Final[tuple[tuple[int, int, int], ...]] = (
    (0, 2, 2),
    (0, 4, 1),
    (0, 5, 1),
    (1, 0, 1),
    (1, 1, 1),
    (1, 2, 2),
    (1, 4, 1),
    (1, 5, 1),
    (2, 0, 3),
    (2, 3, 3),
    (3, 0, 3),
    (3, 3, 3),
    (4, 0, 2),
    (4, 2, 4),
)


def _has_cell_shape(
    cells: tuple[StructureCell, ...], row: int, column: int, column_span: int
) -> bool:
    return any(
        cell.row == row and cell.column == column and cell.column_span == column_span
        for cell in cells
    )


def _slot_header_candidates(
    table: StructureTable, owners: tuple[StructureCell, ...]
) -> tuple[StructureCell, ...]:
    return tuple(
        cell
        for cell in owners
        if cell.column == 0
        and cell.row + 4 < table.rows
        and all(
            _has_cell_shape(owners, cell.row + row, column, column_span)
            for row, column, column_span in _SLOT_REMAINDER_SHAPES
        )
    )


def _caption_title(table: StructureTable) -> str | None:
    if table.caption is None:
        return None
    title = caption_literal_text(
        table.caption.text,
        automatic_number=table.caption.automatic_number,
    )
    return title or None


def analyze_visibility_template(table: StructureTable) -> VisibilityTemplateObservation:
    owners = owner_cells(table)
    candidates = _slot_header_candidates(table, owners)
    headers = tuple(cell for cell in candidates if cell.column_span == 2)
    owner_addresses = frozenset(cell.address for cell in owners)
    missing_owner_addresses = tuple(
        sorted(
            {
                cell.owner_address
                for cell in table.cells
                if cell.owner_address not in owner_addresses
            }
        )
    )
    cells: list[VisibilityTemplateCellObservation] = []
    for cell in owners:
        compact = compact_cell_text(cell.text)
        is_candidate = cell in candidates
        span_ok = cell.column_span == 2
        cells.append(
            VisibilityTemplateCellObservation(
                address=cell.address,
                owner_address=cell.owner_address,
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
                raw_text=cell.text,
                compact_text=compact,
                is_owner=True,
                text_matches=is_candidate,
                span_ok=span_ok,
                is_candidate=is_candidate,
                is_header=cell in headers,
            )
        )
    ordered = tuple(sorted(headers, key=lambda cell: cell.row))
    gaps = tuple(right.row - left.row for left, right in zip(ordered, ordered[1:]))
    slot_height = (
        None if not ordered else table.rows - ordered[0].row if not gaps else gaps[0]
    )
    condition_owner_cells_complete = not missing_owner_addresses
    condition_header_text_found = bool(candidates)
    condition_header_span_found = bool(headers)
    condition_slot_height_minimum = slot_height is not None and slot_height >= 5
    condition_slot_gaps_uniform = bool(ordered) and all(
        gap == slot_height for gap in gaps
    )
    mismatches: list[VisibilityTemplateMismatch] = []
    if not condition_owner_cells_complete:
        mismatches.append(
            VisibilityTemplateMismatch(
                code="missing_owner_cells",
                reason="일부 논리 셀의 병합 소유자 셀이 owner cell 목록에 없습니다",
                expected="every cell.owner_address appears as an owner cell address",
                observed=", ".join(missing_owner_addresses),
            )
        )
    if not condition_header_text_found:
        observed = ", ".join(
            f"{cell.address}:r{cell.row}c{cell.column}s{cell.column_span}"
            for cell in owners
        )
        mismatches.append(
            VisibilityTemplateMismatch(
                code="header_text_mismatch",
                reason="반복 슬롯의 병합 셀 토폴로지를 찾지 못했습니다",
                expected="five-row slot topology",
                observed=observed,
            )
        )
    elif not condition_header_span_found:
        observed = ", ".join(
            f"{cell.address}:column_span={cell.column_span}"
            for cell in owners
            if cell in candidates
        )
        mismatches.append(
            VisibilityTemplateMismatch(
                code="header_span_too_small",
                reason="반복 슬롯 시작 셀의 열 병합 범위가 맞지 않습니다",
                expected="column_span == 2",
                observed=observed,
            )
        )
    if ordered and slot_height is not None and slot_height < 5:
        mismatches.append(
            VisibilityTemplateMismatch(
                code="slot_height_too_small",
                reason="반복 슬롯 높이는 최소 5행이어야 합니다",
                expected="slot_height >= 5",
                observed=str(slot_height),
            )
        )
    if ordered and slot_height is not None and any(gap != slot_height for gap in gaps):
        mismatches.append(
            VisibilityTemplateMismatch(
                code="slot_gaps_nonuniform",
                reason="반복 머리글 사이 간격은 계산된 슬롯 높이와 모두 같아야 합니다",
                expected=f"every gap == {slot_height}",
                observed=", ".join(str(gap) for gap in gaps),
            )
        )
    recognized = (
        condition_owner_cells_complete
        and condition_header_span_found
        and condition_slot_height_minimum
        and condition_slot_gaps_uniform
    )
    positions = tuple(
        VisibilityTemplatePosition(
            address=cell.address, row=cell.row, column=cell.column
        )
        for cell in ordered
    )
    return VisibilityTemplateObservation(
        table_ref=table.table_ref,
        control_instance_id=table.control_instance_id,
        page_start=table.page_start,
        page_end=table.page_end,
        rows=table.rows,
        columns=table.columns,
        owner_cells=tuple(cells),
        missing_owner_addresses=missing_owner_addresses,
        header_positions=positions,
        slot_positions=positions,
        slot_height=slot_height,
        gaps=gaps,
        condition_owner_cells_complete=condition_owner_cells_complete,
        condition_header_text_found=condition_header_text_found,
        condition_header_span_found=condition_header_span_found,
        condition_slot_height_minimum=condition_slot_height_minimum,
        condition_slot_gaps_uniform=condition_slot_gaps_uniform,
        recognized=recognized,
        mismatch_codes=tuple(mismatch.code for mismatch in mismatches),
        mismatches=tuple(mismatches),
    )


def visibility_template_slots(
    table: StructureTable,
) -> tuple[tuple[VisibilitySlot, ...], int]:
    observation = analyze_visibility_template(table)
    if not observation.header_positions:
        raise VisibilitySeriesPlanError(
            "템플릿에서 반복 슬롯 시작 셀을 찾지 못했습니다", observation
        )
    if not observation.recognized:
        raise VisibilitySeriesPlanError(
            "템플릿의 반복 슬롯 구조가 표준 조건과 일치하지 않습니다", observation
        )
    owners = owner_cells(table)
    header_positions = frozenset(
        (position.row, position.column) for position in observation.header_positions
    )
    ordered = tuple(
        sorted(
            (cell for cell in owners if (cell.row, cell.column) in header_positions),
            key=lambda cell: cell.row,
        )
    )
    slot_height = observation.slot_height
    if slot_height is None:
        raise VisibilitySeriesPlanError(
            "템플릿에서 반복 슬롯 시작 셀을 찾지 못했습니다", observation
        )
    try:
        logical = logical_cells(table)
    except VisibilitySeriesPlanError as error:
        raise VisibilitySeriesPlanError(error.message, observation) from error
    slots: list[VisibilitySlot] = []
    for header in ordered:
        row = header.row
        try:
            location_header = cell_at(
                logical, row, header.column + header.column_span, "두 번째 머리글"
            )
            distance_header = cell_at(
                logical,
                row,
                location_header.column + location_header.column_span,
                "세 번째 머리글",
            )
            elevation_header = cell_at(
                logical,
                row,
                distance_header.column + distance_header.column_span,
                "네 번째 머리글",
            )
            visibility_label = cell_at(logical, row + 2, header.column, "왼쪽 사진")
            current_label = cell_at(
                logical,
                row + 2,
                visibility_label.column + visibility_label.column_span,
                "오른쪽 사진",
            )
            result_label = cell_at(logical, row + 4, header.column, "결과")
        except VisibilitySeriesPlanError as error:
            raise VisibilitySeriesPlanError(error.message, observation) from error
        slots.append(
            VisibilitySlot(
                header=header,
                category=cell_at(logical, row + 1, header.column, "첫 번째 값"),
                label=cell_at(logical, row + 1, header.column + 1, "두 번째 값"),
                location=cell_at(
                    logical, row + 1, location_header.column, "세 번째 값"
                ),
                distance=cell_at(
                    logical, row + 1, distance_header.column, "네 번째 값"
                ),
                elevation=cell_at(
                    logical, row + 1, elevation_header.column, "다섯 번째 값"
                ),
                visibility_image=cell_at(
                    logical, row + 3, visibility_label.column, "왼쪽 사진 값"
                ),
                current_image=cell_at(
                    logical, row + 3, current_label.column, "오른쪽 사진 값"
                ),
                result=cell_at(
                    logical,
                    row + 4,
                    result_label.column + result_label.column_span,
                    "결과 값",
                ),
            )
        )
    return tuple(slots), slot_height


def _text(
    slot: VisibilitySlot, record: VisibilityRecord
) -> tuple[TemplateTextCell, ...]:
    values = (
        (slot.category, record.category),
        (slot.label, record.label),
        (slot.location, record.location),
        (slot.distance, record.distance),
        (slot.elevation, record.elevation),
        (slot.result, record.result),
    )
    return tuple(
        TemplateTextCell(
            address=cell.address,
            expected_text=cell.text,
            replacement=value,
        )
        for cell, value in values
    )


def build_visibility_series_plan(
    source: StructureTable,
    template: StructureTable,
    visibility_folder: Path,
    current_folder: Path,
) -> TableTemplateRepeatPlan:
    plan, _observation = build_visibility_series_plan_with_observation(
        source,
        template,
        visibility_folder,
        current_folder,
    )
    return plan


def build_visibility_series_plan_with_observation(
    source: StructureTable,
    template: StructureTable,
    visibility_folder: Path,
    current_folder: Path,
) -> tuple[TableTemplateRepeatPlan, VisibilityTemplateObservation]:
    observation = analyze_visibility_template(template)
    if template.control_instance_id is None:
        raise VisibilitySeriesPlanError(
            "템플릿 표의 네이티브 식별자를 읽지 못했습니다", observation
        )
    records = extract_visibility_records(source)
    try:
        slots, slot_height = visibility_template_slots(template)
    except VisibilitySeriesPlanError as error:
        raise VisibilitySeriesPlanError(
            error.message, error.template_observation or observation
        ) from error
    blocks: list[TemplateTableBlock] = []
    for start in range(0, len(records), len(slots)):
        group = records[start : start + len(slots)]
        texts: list[TemplateTextCell] = []
        images: list[TemplateImageCell] = []
        for slot, record in zip(slots, group, strict=False):
            texts.extend(_text(slot, record))
            visibility_path = optional_numbered_image(visibility_folder, record.number)
            current_path = optional_numbered_image(current_folder, record.number)
            if visibility_path is not None:
                images.append(
                    TemplateImageCell(
                        address=slot.visibility_image.address,
                        expected_text=slot.visibility_image.text,
                        path=visibility_path,
                        width_mm=IMAGE_WIDTH_MM,
                        height_mm=IMAGE_HEIGHT_MM,
                    )
                )
            if current_path is not None:
                images.append(
                    TemplateImageCell(
                        address=slot.current_image.address,
                        expected_text=slot.current_image.text,
                        path=current_path,
                        width_mm=IMAGE_WIDTH_MM,
                        height_mm=IMAGE_HEIGHT_MM,
                    )
                )
        unused = len(slots) - len(group)
        blocks.append(
            TemplateTableBlock(
                text_cells=tuple(texts),
                images=tuple(images),
                delete_rows_from=(
                    None if unused == 0 else slots[len(group)].header.address
                ),
                delete_row_count=unused * slot_height,
            )
        )
    plan = TableTemplateRepeatPlan(
        source_page=template.page_start,
        source_control_id=template.control_instance_id,
        caption_title=_caption_title(template),
        blocks=tuple(blocks),
    )
    return plan, observation

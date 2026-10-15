from __future__ import annotations

import re
from dataclasses import dataclass

from hwp_errors import HwpLiveError
from hwp_live_native_action_models import (
    NativePageControl,
    NativePageInspection,
    NativeSnapshot,
)
from hwp_live_native_batch import (
    inspect_native_page,
    inspect_native_pages,
    read_native_snapshot,
)
from hwp_live_template_repeat import TableTemplateRepeatPlan


_MAX_CONSECUTIVE_MISSES = 3


class AmbiguousTableSeriesError(HwpLiveError):
    pass


class InvalidTableSeriesError(HwpLiveError):
    pass


@dataclass(frozen=True, slots=True)
class DiscoveredSeriesTable:
    page: NativePageInspection
    control: NativePageControl


def required_series_snapshot(window_handle: int) -> NativeSnapshot:
    snapshot = read_native_snapshot(window_handle)
    if snapshot is None:
        raise HwpLiveError("표 시리즈 동기화용 현재 문서 상태를 읽지 못했습니다")
    return snapshot


def required_series_page(
    window_handle: int,
    page: int,
    *,
    include_cells: bool,
) -> NativePageInspection:
    inspected = inspect_native_page(window_handle, page, include_cells=include_cells)
    if inspected is None:
        raise HwpLiveError(f"표 시리즈 {page}쪽 구조를 읽지 못했습니다")
    return inspected


def _table_series_signature(
    page: NativePageInspection,
    table_instance_id: str,
) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        sorted(
            (cell.address, cell.row_span, cell.column_span)
            for cell in page.cells
            if cell.table_instance_id == table_instance_id
        )
    )


def _address_row(address: str) -> int:
    match = re.search(r"[1-9][0-9]*$", address)
    if match is None:
        raise HwpLiveError(f"표 셀 주소의 행 번호를 읽지 못했습니다: {address}")
    return int(match.group())


def _signature_for_rows(
    signature: tuple[tuple[str, int, int], ...], rows: int
) -> tuple[tuple[str, int, int], ...]:
    return tuple(cell for cell in signature if _address_row(cell[0]) <= rows)


def _required_series_signature(
    page: NativePageInspection,
    table_instance_id: str,
) -> tuple[tuple[str, int, int], ...]:
    signature = _table_series_signature(page, table_instance_id)
    if not signature:
        raise HwpLiveError("동기화 원본 표의 셀 구조를 읽지 못했습니다")
    return signature


def _matches_series_signature(
    page: NativePageInspection,
    table_instance_id: str,
    signature: tuple[tuple[str, int, int], ...],
) -> bool:
    return _table_series_signature(page, table_instance_id) == signature


def discover_table_series(
    window_handle: int,
    plan: TableTemplateRepeatPlan,
) -> tuple[DiscoveredSeriesTable, ...]:
    snapshot = required_series_snapshot(window_handle)
    source_page = required_series_page(
        window_handle,
        plan.source_page,
        include_cells=True,
    )
    sources = tuple(
        control
        for control in source_page.controls
        if control.control_type == "tbl"
        and control.instance_id == plan.source_control_id
    )
    if len(sources) != 1 or sources[0].rows is None or sources[0].columns is None:
        raise HwpLiveError("동기화 원본 표의 행·열 구조를 하나로 찾지 못했습니다")
    source_signature = _required_series_signature(
        source_page,
        sources[0].instance_id,
    )
    source_rows = sources[0].rows
    allowed_rows = {source_rows - block.delete_row_count for block in plan.blocks}
    allowed_rows.add(source_rows)
    allowed_signatures = {
        rows: _signature_for_rows(source_signature, rows) for rows in allowed_rows
    }
    last_page = min(
        snapshot.page_count,
        plan.source_page + max(len(plan.blocks) * 3, _MAX_CONSECUTIVE_MISSES),
    )
    found: list[DiscoveredSeriesTable] = [
        DiscoveredSeriesTable(source_page, sources[0])
    ]
    candidate_pages = tuple(range(plan.source_page + 1, last_page + 1))
    detailed_pages = inspect_native_pages(
        window_handle,
        candidate_pages,
        include_cells=True,
    )
    for detailed in detailed_pages:
        shaped_controls = tuple(
            control
            for control in detailed.controls
            if control.control_type == "tbl"
            and control.rows is not None
            and control.rows in allowed_rows
            and control.columns == sources[0].columns
        )
        controls = tuple(
            control
            for control in shaped_controls
            if control.rows is not None
            and _matches_series_signature(
                detailed,
                control.instance_id,
                allowed_signatures[control.rows],
            )
        )
        if len(controls) > 1:
            control_ids = ", ".join(
                control.instance_id
                for control in sorted(
                    controls,
                    key=lambda item: (
                        item.anchor.paragraph,
                        item.anchor.character,
                    ),
                )
            )
            raise AmbiguousTableSeriesError(
                f"{detailed.page}쪽에서 시리즈 표 후보를 하나로 확정할 수 없습니다: "
                + control_ids
            )
        if not controls and shaped_controls:
            control_ids = ", ".join(
                control.instance_id
                for control in sorted(
                    shaped_controls,
                    key=lambda item: (
                        item.anchor.paragraph,
                        item.anchor.character,
                    ),
                )
            )
            raise InvalidTableSeriesError(
                f"{detailed.page}쪽의 같은 행·열 표가 원본 셀 구조와 다릅니다: "
                + control_ids
            )
        if not controls:
            continue
        found.extend(
            DiscoveredSeriesTable(detailed, control)
            for control in sorted(
                controls,
                key=lambda item: (
                    item.anchor.paragraph,
                    item.anchor.character,
                ),
            )
        )
        if len(found) >= len(plan.blocks):
            found = found[: len(plan.blocks)]
            break
    if not found or found[0].control.instance_id != plan.source_control_id:
        raise HwpLiveError("동기화 원본 표가 시리즈의 첫 표가 아닙니다")
    return tuple(found)

from __future__ import annotations

import secrets
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep
from typing import Literal, Protocol, cast

from PIL import Image, UnidentifiedImageError

from hwp_errors import HwpLiveError
from hwp_live_api import SelectionRange
from hwp_live_contract import PreviewResult
from hwp_live_native_action_models import NativeDetailedInspection
from hwp_live_native_batch import (
    inspect_native_structure,
    native_foreground_guard,
    read_native_snapshot,
)
from hwp_live_native_format_inputs import table_cell_coordinate
from hwp_live_native_table_topology import table_topology
from hwp_object_control_types import (
    is_picture_control_type,
    is_table_control_type,
)


class PreviewDocument(Protocol):
    @property
    def Modified(self) -> int: ...


class PreviewCandidate(Protocol):
    @property
    def document(self) -> PreviewDocument: ...

    @property
    def window_handle(self) -> int: ...


class PreviewViewProperties(Protocol):
    def Item(self, name: str) -> int: ...  # noqa: N802

    def SetItem(self, name: str, value: int) -> None: ...  # noqa: N802


class PreviewComApplication(Protocol):
    @property
    def ViewProperties(self) -> PreviewViewProperties: ...  # noqa: N802

    @ViewProperties.setter
    def ViewProperties(  # noqa: N802
        self,
        value: PreviewViewProperties,
    ) -> None: ...


class PreviewApplication(Protocol):
    @property
    def hwp(self) -> PreviewComApplication: ...

    @property
    def SelectionMode(self) -> int: ...

    @property
    def PageCount(self) -> int: ...

    @property
    def current_page(self) -> int: ...

    def get_pos(self) -> tuple[int, int, int]: ...

    def get_selected_pos(self) -> SelectionRange: ...

    def select_text(self, selection: SelectionRange) -> bool: ...

    def RecalcPageCount(self) -> bool: ...

    # pyhwpx returns (print page, page index) on success but a bare False when
    # the requested index falls outside the document, so the union is the real
    # contract rather than a defensive fiction.
    def goto_page(self, page_index: int | str = 1) -> tuple[int, int] | bool: ...

    def set_pos(self, List: int, para: int, pos: int) -> bool: ...

    def get_cell_addr(self) -> str: ...

    def Cancel(self) -> bool: ...

    def TableCellBlock(self) -> bool: ...

    def TableCellBlockExtend(self) -> bool: ...

    def TableRightCell(self) -> bool: ...

    def TableLowerCell(self) -> bool: ...

    def create_page_image(
        self,
        path: str,
        pgno: int = -1,
        resolution: int = 300,
        depth: int = 24,
        format: str = "bmp",
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class CellSelectionState:
    first_list_id: int
    endpoint_address: str
    path: tuple[Literal["right", "lower"], ...]
    selection_mode: int


def _capture_cell_selection(
    candidate: PreviewCandidate,
    selection_mode: int,
    guard: Callable[[], None],
) -> CellSelectionState:
    snapshot = read_native_snapshot(candidate.window_handle)
    guard()
    if snapshot is None:
        raise HwpLiveError("셀 블록 미리보기 선택 주소를 읽을 수 없습니다")
    if snapshot.selection.cell_address_error:
        raise HwpLiveError(snapshot.selection.cell_address_error)
    if (
        snapshot.selection.mode != selection_mode
        or not is_table_control_type(snapshot.control_type)
        or not snapshot.control_instance_id
        or not snapshot.selection.cell_addresses
    ):
        raise HwpLiveError("셀 블록 미리보기 선택 상태가 올바르지 않습니다")
    # The selection belongs to the cursor page, not necessarily the page the
    # caller asked to render.
    detail = inspect_native_structure(candidate.window_handle, snapshot.current_page)
    guard()
    if detail is None:
        raise HwpLiveError("셀 블록 미리보기 표 구조를 읽을 수 없습니다")
    topology = table_topology(detail, snapshot.control_instance_id)
    region = topology.selection_region_by_addresses(snapshot.selection.cell_addresses)
    physical = tuple(topology.by_address[address] for address in region)
    top = min(table_cell_coordinate(cell.address)[0] for cell in physical)
    left = min(table_cell_coordinate(cell.address)[1] for cell in physical)
    bottom = max(
        table_cell_coordinate(cell.address)[0] + cell.row_span - 1 for cell in physical
    )
    right = max(
        table_cell_coordinate(cell.address)[1] + cell.column_span - 1
        for cell in physical
    )

    def cell_at(row: int, column: int):
        return next(
            (
                cell
                for cell in topology.cells
                if table_cell_coordinate(cell.address)[0]
                <= row
                < table_cell_coordinate(cell.address)[0] + cell.row_span
                and table_cell_coordinate(cell.address)[1]
                <= column
                < table_cell_coordinate(cell.address)[1] + cell.column_span
            ),
            None,
        )

    first = cell_at(top, left)
    endpoint = cell_at(bottom, right)
    if first is None or endpoint is None:
        raise HwpLiveError("셀 블록 미리보기 선택 모서리를 찾지 못했습니다")
    current = first
    path: list[Literal["right", "lower"]] = []
    while table_cell_coordinate(current.address)[1] + current.column_span - 1 < right:
        next_column = table_cell_coordinate(current.address)[1] + current.column_span
        next_cell = cell_at(top, next_column)
        if next_cell is None or next_cell is current:
            raise HwpLiveError("셀 블록 미리보기 오른쪽 선택 경로가 끊겼습니다")
        path.append("right")
        current = next_cell
    while table_cell_coordinate(current.address)[0] + current.row_span - 1 < bottom:
        next_row = table_cell_coordinate(current.address)[0] + current.row_span
        next_cell = cell_at(next_row, right)
        if next_cell is None or next_cell is current:
            raise HwpLiveError("셀 블록 미리보기 아래쪽 선택 경로가 끊겼습니다")
        path.append("lower")
        current = next_cell
    if current.address != endpoint.address:
        raise HwpLiveError("셀 블록 미리보기 선택 끝 셀 경로가 다릅니다")
    return CellSelectionState(
        first.list_id,
        endpoint.address,
        tuple(path),
        selection_mode,
    )


def _restore_cell_selection(
    hwp: PreviewApplication,
    state: CellSelectionState,
    guard: Callable[[], None],
) -> None:
    if not hwp.set_pos(state.first_list_id, 0, 0):
        raise HwpLiveError("미리보기 후 셀 블록 시작 위치를 복원하지 못했습니다")
    guard()
    if not hwp.TableCellBlock():
        raise HwpLiveError("미리보기 후 셀 블록 선택을 시작하지 못했습니다")
    guard()
    if (state.path or state.selection_mode & 0x10) and not hwp.TableCellBlockExtend():
        raise HwpLiveError("미리보기 후 셀 블록 확장을 시작하지 못했습니다")
    guard()
    for step in state.path:
        moved = hwp.TableRightCell() if step == "right" else hwp.TableLowerCell()
        guard()
        if not moved:
            raise HwpLiveError("미리보기 후 셀 블록 선택 경로를 복원하지 못했습니다")
    if (
        int(hwp.SelectionMode) != state.selection_mode
        or hwp.get_cell_addr().strip().upper() != state.endpoint_address
    ):
        raise HwpLiveError("미리보기 후 셀 블록 선택 검증에 실패했습니다")
    guard()


def _wait_for_collapsed_selection(
    hwp: PreviewApplication,
    cursor: tuple[int, int, int],
    guard: Callable[[], None],
) -> None:
    deadline = monotonic() + 0.75
    stable_reads = 0
    while True:
        collapsed = (int(hwp.SelectionMode) & 0x0F) == 0 and hwp.get_pos() == cursor
        guard()
        stable_reads = stable_reads + 1 if collapsed else 0
        if stable_reads >= 5:
            return
        if monotonic() >= deadline:
            raise HwpLiveError(
                "한컴 미리보기 전에 선택 해제 상태가 안정되지 않았습니다"
            )
        sleep(0.05)


def _hide_guidelines(hwp: PreviewApplication, guard: Callable[[], None]) -> int:
    properties = hwp.hwp.ViewProperties
    guard()
    option_flags = int(properties.Item("OptionFlag"))
    guard()
    properties.SetItem("OptionFlag", option_flags & ~0x0008)
    hwp.hwp.ViewProperties = properties
    guard()
    return option_flags


def _restore_guidelines(
    hwp: PreviewApplication,
    option_flags: int,
    guard: Callable[[], None],
) -> None:
    properties = hwp.hwp.ViewProperties
    guard()
    properties.SetItem("OptionFlag", option_flags)
    hwp.hwp.ViewProperties = properties
    guard()


def _rendered_png_is_uniform(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        with Image.open(path) as image:
            if image.format != "PNG":
                raise HwpLiveError("한컴 미리보기 결과가 PNG 형식이 아닙니다")
            channel_count = len(image.getbands())
            raw_extrema = image.getextrema()
    except HwpLiveError:
        raise
    except (OSError, UnidentifiedImageError, ValueError) as error:
        raise HwpLiveError("한컴 미리보기 PNG를 확인하지 못했습니다") from error
    if channel_count == 1:
        low, high = cast(tuple[float, float], raw_extrema)
        return low == high
    extrema = cast(tuple[tuple[float, float], ...], raw_extrema)
    return all(low == high for low, high in extrema)


def _has_visible_text(value: str) -> bool:
    return any(
        character.isprintable() and not character.isspace() for character in value
    )


def _page_has_visible_structure(detail: NativeDetailedInspection) -> bool:
    page = detail.page
    return (
        _has_visible_text(detail.text)
        or any(
            cell.page_start <= page <= cell.page_end and _has_visible_text(cell.text)
            for cell in detail.cells
        )
        or any(
            caption.page_start <= page <= caption.page_end
            and _has_visible_text(caption.text)
            for caption in detail.captions
        )
        or any(
            control.page_start <= page <= control.page_end
            and is_picture_control_type(control.control_type)
            for control in detail.controls
        )
    )


def render_page(
    candidate: PreviewCandidate,
    hwp: PreviewApplication,
    page: int,
    dpi: int,
    session_id: str,
    guard: Callable[[], None],
    *,
    directory: Path | None = None,
) -> PreviewResult:
    # 렌더는 파이썬이 한컴 COM을 직접 때리는 경로다. 네이티브 Invoke를 거치지
    # 않으니 BatchAutomation::Invoke의 가드가 이 구간을 덮지 못한다. 그래서
    # 브리지에 가드를 열고 닫으라고만 시킨다. 무장할지, 활성화를 거부할지,
    # 사용자가 직접 올린 창인지, 모달이면 어떻게 할지는 전부 C++ 쪽 판단이다.
    #
    # 가드가 실패해도 렌더는 그대로 진행된다. 포커스 때문에 미리보기를 막지 않는다.
    with native_foreground_guard(candidate.window_handle):
        return _render_page(
            candidate,
            hwp,
            page,
            dpi,
            session_id,
            guard,
            directory=directory,
        )


def _render_page(
    candidate: PreviewCandidate,
    hwp: PreviewApplication,
    page: int,
    dpi: int,
    session_id: str,
    guard: Callable[[], None],
    *,
    directory: Path | None = None,
) -> PreviewResult:
    guard()
    if dpi < 72 or dpi > 600:
        raise HwpLiveError("미리보기 DPI는 72부터 600까지 지원합니다")
    if page == 0:
        target_page = hwp.current_page
        guard()
    else:
        target_page = page
    page_count = hwp.PageCount
    guard()
    if target_page < 1 or target_page > page_count:
        raise HwpLiveError("미리보기 쪽 번호가 문서 범위를 벗어났습니다")
    cursor = hwp.get_pos()
    guard()
    selection = hwp.get_selected_pos()
    guard()
    selection_mode = int(hwp.SelectionMode)
    guard()
    has_selection = selection[0] or (selection_mode & 0x0F) != 0
    cell_selection = (
        _capture_cell_selection(
            candidate,
            selection_mode,
            guard,
        )
        if (selection_mode & 0x0F) == 3
        else None
    )
    restorable_selection = cell_selection is not None or selection[0]
    modified = candidate.document.Modified
    guard()
    before = (modified, cursor, selection, selection_mode)
    output_directory = (
        Path(tempfile.gettempdir()) / "hancom-live-agent" / session_id
        if directory is None
        else directory
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    render_id = secrets.token_hex(6)
    partial_path = (
        output_directory / f".rendering-page-{target_page:03}-{render_id}.png"
    )
    bitmap_path = partial_path.with_suffix(".bmp")
    path = output_directory / f"page-{target_page:03}-{render_id}.png"
    published = False
    try:
        try:
            option_flags = _hide_guidelines(hwp, guard)
            try:
                if restorable_selection:
                    _ = hwp.Cancel()
                    guard()
                    if not hwp.set_pos(*cursor):
                        raise HwpLiveError(
                            "한컴 미리보기 전에 선택 영역을 접지 못했습니다"
                        )
                    guard()
                    _wait_for_collapsed_selection(hwp, cursor, guard)

                def render_once() -> bool:
                    partial_path.unlink(missing_ok=True)
                    bitmap_path.unlink(missing_ok=True)
                    if not has_selection or restorable_selection:
                        if cursor[0] != 0:
                            if not hwp.set_pos(0, 0, 0):
                                raise HwpLiveError(
                                    "한컴 미리보기 전에 본문으로 이동하지 못했습니다"
                                )
                            guard()
                        _ = hwp.RecalcPageCount()
                        guard()
                        moved = hwp.goto_page(target_page)
                        guard()
                        # goto_page() finishes by reading the caret page and
                        # returns (print page, page index), so the second element
                        # is the value hwp.current_page would report. Reusing it
                        # avoids repeating a property walk that costs four COM
                        # round trips. Older shapes fall back to the direct read.
                        if isinstance(moved, tuple) and len(moved) == 2:
                            current_page = int(moved[1])
                        else:
                            current_page = hwp.current_page
                            guard()
                        if current_page != target_page:
                            raise HwpLiveError(
                                "한컴 미리보기 쪽으로 이동하지 못했습니다"
                            )
                        rendered_once = hwp.create_page_image(
                            str(bitmap_path),
                            pgno=0,
                            resolution=dpi,
                        )
                    else:
                        rendered_once = hwp.create_page_image(
                            str(bitmap_path),
                            pgno=target_page,
                            resolution=dpi,
                        )
                    guard()
                    if rendered_once:
                        with Image.open(bitmap_path) as image:
                            image.save(partial_path, format="PNG")
                    return rendered_once

                rendered = render_once()
                if rendered and _rendered_png_is_uniform(partial_path):
                    detail = inspect_native_structure(
                        candidate.window_handle,
                        target_page,
                    )
                    guard()
                    if detail is None:
                        raise HwpLiveError(
                            "한컴 빈 미리보기의 페이지 구조를 확인하지 못했습니다"
                        )
                    if _page_has_visible_structure(detail):
                        rendered = render_once()
                        if rendered and _rendered_png_is_uniform(partial_path):
                            raise HwpLiveError(
                                "내용이 있는 쪽의 빈 미리보기가 재시도 후에도 계속되었습니다"
                            )
            finally:
                _restore_guidelines(hwp, option_flags, guard)
        finally:
            if cell_selection is not None:
                guard()
                _restore_cell_selection(hwp, cell_selection, guard)
            elif selection[0]:
                guard()
                if not hwp.select_text(selection):
                    raise HwpLiveError(
                        "한컴 미리보기 후 텍스트 선택 영역을 복원하지 못했습니다"
                    )
                guard()
            elif not has_selection:
                guard()
                if not hwp.set_pos(*cursor):
                    raise HwpLiveError("한컴 미리보기 후 커서를 복원하지 못했습니다")
                guard()
        if not rendered:
            raise HwpLiveError("한컴 현재 쪽 미리보기를 만들지 못했습니다")
        modified = candidate.document.Modified
        guard()
        restored_cursor = hwp.get_pos()
        guard()
        restored_selection = hwp.get_selected_pos()
        guard()
        restored_selection_mode = int(hwp.SelectionMode)
        guard()
        after = (
            modified,
            restored_cursor,
            restored_selection,
            restored_selection_mode,
        )
        if (
            before != after
            or not partial_path.is_file()
            or partial_path.stat().st_size == 0
        ):
            raise HwpLiveError("한컴 미리보기 상태 보존 검증에 실패했습니다")
        _ = partial_path.replace(path)
        published = True
        return PreviewResult(page=target_page, path=path)
    finally:
        bitmap_path.unlink(missing_ok=True)
        if not published:
            partial_path.unlink(missing_ok=True)

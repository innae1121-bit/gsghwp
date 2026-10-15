from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from hwp_errors import HwpLiveError
from hwp_live_api import HwpComApplication
from hwp_live_native_action_models import NativeDetailedCell, NativeDetailedInspection
from hwp_live_native_format_inputs import SplitSpec, table_cell_coordinate


_CELL_ADDRESS = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]+[1-9][0-9]*)(?![A-Za-z0-9_])")


@dataclass(frozen=True, slots=True)
class TableTopology:
    table_instance_id: str
    rows: int
    columns: int
    width_hwpunit: int | None
    height_hwpunit: int | None
    cells: tuple[NativeDetailedCell, ...]
    by_address: Mapping[str, NativeDetailedCell]

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    def merge_region(self, first: str, second: str) -> tuple[str, ...]:
        start = self.by_address.get(first.upper())
        end = self.by_address.get(second.upper())
        if start is None or end is None:
            raise HwpLiveError("병합 시작·끝 셀이 실제 표 토폴로지에 없습니다")
        start_row, start_column = table_cell_coordinate(start.address)
        end_row, end_column = table_cell_coordinate(end.address)
        if end_row < start_row or end_column < start_column:
            raise HwpLiveError("병합 셀 범위가 앞에서 뒤로 진행하지 않습니다")
        bottom = end_row + end.row_span - 1
        right = end_column + end.column_span - 1
        region: list[NativeDetailedCell] = []
        covered = 0
        for cell in self.cells:
            row, column = table_cell_coordinate(cell.address)
            cell_bottom = row + cell.row_span - 1
            cell_right = column + cell.column_span - 1
            intersects = (
                row <= bottom
                and start_row <= cell_bottom
                and column <= right
                and start_column <= cell_right
            )
            if not intersects:
                continue
            contained = (
                row >= start_row
                and column >= start_column
                and cell_bottom <= bottom
                and cell_right <= right
            )
            if not contained:
                raise HwpLiveError(
                    "병합 영역이 기존 셀을 가로질러 빈틈 없는 직사각형이 아닙니다"
                )
            region.append(cell)
            covered += cell.row_span * cell.column_span
        expected = (bottom - start_row + 1) * (right - start_column + 1)
        if covered != expected or len(region) < 2:
            raise HwpLiveError("병합 영역이 빈틈 없는 직사각형 셀 블록이 아닙니다")
        return tuple(
            cell.address
            for cell in sorted(
                region, key=lambda item: table_cell_coordinate(item.address)
            )
        )

    def selection_region_by_list_ids(
        self,
        first_list_id: int,
        second_list_id: int,
    ) -> tuple[str, ...]:
        first_matches = tuple(
            cell for cell in self.cells if cell.list_id == first_list_id
        )
        second_matches = tuple(
            cell for cell in self.cells if cell.list_id == second_list_id
        )
        if len(first_matches) != 1 or len(second_matches) != 1:
            raise HwpLiveError("선택 시작·끝 위치가 대상 표의 실제 셀에 없습니다")
        first, second = first_matches[0], second_matches[0]
        first_row, first_column = table_cell_coordinate(first.address)
        second_row, second_column = table_cell_coordinate(second.address)
        top = min(first_row, second_row)
        left = min(first_column, second_column)
        bottom = max(
            first_row + first.row_span - 1,
            second_row + second.row_span - 1,
        )
        right = max(
            first_column + first.column_span - 1,
            second_column + second.column_span - 1,
        )
        region: list[NativeDetailedCell] = []
        covered = 0
        for cell in self.cells:
            row, column = table_cell_coordinate(cell.address)
            cell_bottom = row + cell.row_span - 1
            cell_right = column + cell.column_span - 1
            intersects = (
                row <= bottom
                and top <= cell_bottom
                and column <= right
                and left <= cell_right
            )
            if not intersects:
                continue
            if row < top or column < left or cell_bottom > bottom or cell_right > right:
                raise HwpLiveError("선택 셀 영역이 빈틈 없는 직사각형이 아닙니다")
            region.append(cell)
            covered += cell.row_span * cell.column_span
        expected = (bottom - top + 1) * (right - left + 1)
        if covered != expected:
            raise HwpLiveError("선택 셀 영역이 빈틈 없는 직사각형이 아닙니다")
        return tuple(
            cell.address
            for cell in sorted(
                region, key=lambda item: table_cell_coordinate(item.address)
            )
        )

    def selection_region_by_addresses(
        self,
        addresses: tuple[str, ...],
    ) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(address.upper() for address in addresses))
        selected = tuple(self.selection_owner(address) for address in normalized)
        if not normalized or any(cell is None for cell in selected):
            raise HwpLiveError("선택 주소가 대상 표의 실제 셀에 없습니다")
        physical = tuple(cell for cell in selected if cell is not None)
        normalized = tuple(dict.fromkeys(cell.address for cell in physical))
        top = min(table_cell_coordinate(cell.address)[0] for cell in physical)
        left = min(table_cell_coordinate(cell.address)[1] for cell in physical)
        bottom = max(
            table_cell_coordinate(cell.address)[0] + cell.row_span - 1
            for cell in physical
        )
        right = max(
            table_cell_coordinate(cell.address)[1] + cell.column_span - 1
            for cell in physical
        )
        first = next(
            (
                cell
                for cell in self.cells
                if table_cell_coordinate(cell.address)[0]
                <= top
                < table_cell_coordinate(cell.address)[0] + cell.row_span
                and table_cell_coordinate(cell.address)[1]
                <= left
                < table_cell_coordinate(cell.address)[1] + cell.column_span
            ),
            None,
        )
        second = next(
            (
                cell
                for cell in self.cells
                if table_cell_coordinate(cell.address)[0]
                <= bottom
                < table_cell_coordinate(cell.address)[0] + cell.row_span
                and table_cell_coordinate(cell.address)[1]
                <= right
                < table_cell_coordinate(cell.address)[1] + cell.column_span
            ),
            None,
        )
        if first is None or second is None:
            raise HwpLiveError("선택 주소의 직사각형 모서리를 복원하지 못했습니다")
        region = self.selection_region_by_list_ids(first.list_id, second.list_id)
        if len(normalized) > 2 and frozenset(normalized) != frozenset(region):
            raise HwpLiveError("선택 주소가 빈틈 없는 직사각형 셀 블록이 아닙니다")
        return region

    def selection_owner(self, address: str) -> NativeDetailedCell | None:
        """Resolve HWP selection grid corners, including covered merge cells."""
        direct = self.by_address.get(address.upper())
        if direct is not None:
            return direct
        row, column = table_cell_coordinate(address)
        return next(
            (
                cell
                for cell in self.cells
                if table_cell_coordinate(cell.address)[0]
                <= row
                < table_cell_coordinate(cell.address)[0] + cell.row_span
                and table_cell_coordinate(cell.address)[1]
                <= column
                < table_cell_coordinate(cell.address)[1] + cell.column_span
            ),
            None,
        )


def table_formula_selection_region(
    application: HwpComApplication,
    topology: TableTopology,
) -> tuple[str, ...]:
    # 실패 메시지에 "셀 주소를 지정해 다시 요청하라"고 적지 않는다.
    # 그 문구를 받은 모델은 실제로 A1, F3 같은 주소를 지어내서 엉뚱한 셀에 쓴다.
    # 사용자가 겪은 증상이 이것이다. 무엇이 안 됐는지만 알리고 판단은 호출부에 맡긴다.
    # 이 경로는 이제 마지막 대안이다. 선택이나 셀 주소가 있으면 topology 로 먼저 푼다.
    if topology.rows * topology.columns > 81:
        raise HwpLiveError(
            "대상 표의 행×열 격자가 81셀을 초과해 TableFormula로 선택 주소를 "
            + "확인할 수 없습니다"
        )
    try:
        field = application.HParameterSet.HFieldCtrl
        if not application.HAction.GetDefault("TableFormula", field.HSet):
            raise HwpLiveError("TableFormula 선택 주소 초기화에 실패했습니다")
        command = str(field.Command)
    except HwpLiveError:
        raise
    except Exception as error:
        raise HwpLiveError("TableFormula 선택 주소를 읽지 못했습니다") from error
    addresses = tuple(
        dict.fromkeys(
            match.group(1).upper() for match in _CELL_ADDRESS.finditer(command)
        )
    )
    if len(addresses) < 2:
        raise HwpLiveError("TableFormula가 다중 셀 선택 주소를 반환하지 않았습니다")
    return topology.selection_region_by_addresses(addresses)


def table_topology(
    detail: NativeDetailedInspection,
    table_instance_id: str,
) -> TableTopology:
    control = next(
        (
            item
            for item in detail.controls
            if item.control_type == "tbl" and item.instance_id == table_instance_id
        ),
        None,
    )
    cells = tuple(
        cell for cell in detail.cells if cell.table_instance_id == table_instance_id
    )
    if control is None or not cells:
        raise HwpLiveError("대상 표의 CellTopology를 구성할 구조 정보가 없습니다")
    by_address: dict[str, NativeDetailedCell] = {}
    derived_rows = 0
    derived_columns = 0
    owners: dict[tuple[int, int], str] = {}
    for cell in cells:
        address = cell.address.upper()
        if address in by_address or cell.row_span < 1 or cell.column_span < 1:
            raise HwpLiveError("CellTopology에 중복 주소나 잘못된 span이 있습니다")
        by_address[address] = cell
        row, column = table_cell_coordinate(address)
        derived_rows = max(derived_rows, row + cell.row_span - 1)
        derived_columns = max(derived_columns, column + cell.column_span - 1)
        for physical_row in range(row, row + cell.row_span):
            for physical_column in range(column, column + cell.column_span):
                slot = physical_row, physical_column
                if slot in owners:
                    raise HwpLiveError("CellTopology의 셀 span이 서로 겹칩니다")
                owners[slot] = address
    rows = derived_rows if control.rows is None else control.rows
    columns = derived_columns if control.columns is None else control.columns
    if (
        rows < derived_rows
        or columns < derived_columns
        or len(owners) != rows * columns
    ):
        raise HwpLiveError("CellTopology의 물리 격자에 겹침 또는 빈틈이 있습니다")
    return TableTopology(
        table_instance_id,
        rows,
        columns,
        control.width_hwpunit,
        control.height_hwpunit,
        cells,
        MappingProxyType(by_address),
    )


def doomed_horizontal_merge_neighbours(
    topology: TableTopology,
    first: str,
    second: str,
) -> tuple[str, str] | None:
    """Describe the exact adjacent-span pattern HWP cannot merge safely."""
    _ = topology.merge_region(first, second)
    start = topology.by_address[first.upper()]
    end = topology.by_address[second.upper()]
    row, left = table_cell_coordinate(start.address)
    end_row, end_column = table_cell_coordinate(end.address)
    bottom = end_row + end.row_span - 1
    right = end_column + end.column_span - 1
    if bottom != row or right <= left:
        return None

    neighbours: list[str] = []
    for neighbour_row in (row - 1, row + 1):
        if neighbour_row < 1 or neighbour_row > topology.rows:
            neighbours.append(f"{neighbour_row}행=표 경계(범위 밖)")
            continue
        spans = tuple(
            cell
            for cell in topology.cells
            if (
                table_cell_coordinate(cell.address)[0] == neighbour_row
                and cell.row_span == 1
                and left <= table_cell_coordinate(cell.address)[1]
                and table_cell_coordinate(cell.address)[1] + cell.column_span - 1
                <= right
            )
        )
        if not spans or any(cell.column_span <= 1 for cell in spans):
            return None
        covered_columns = {
            column
            for cell in spans
            for column in range(
                table_cell_coordinate(cell.address)[1],
                table_cell_coordinate(cell.address)[1] + cell.column_span,
            )
        }
        if not set(range(left, right + 1)).issubset(covered_columns):
            return None
        descriptions = ", ".join(
            f"{cell.address}(row_span={cell.row_span},column_span={cell.column_span},"
            f"columns={table_cell_coordinate(cell.address)[1]}.."
            f"{table_cell_coordinate(cell.address)[1] + cell.column_span - 1})"
            for cell in sorted(
                spans, key=lambda item: table_cell_coordinate(item.address)
            )
        )
        neighbours.append(f"{neighbour_row}행=[{descriptions}]")
    return neighbours[0], neighbours[1]


def verify_split_preflight(topology: TableTopology, split: SplitSpec) -> None:
    target = topology.by_address.get(split.cell.upper())
    if target is None:
        raise HwpLiveError("나눌 셀이 실제 표 CellTopology에 없습니다")
    if split.split_mode == "existing_grid":
        if target.column_span != split.columns or target.row_span != split.rows:
            raise HwpLiveError(
                "기존 격자선 복원 분할은 대상 셀의 column_span·row_span과 정확히 같아야 합니다"
            )
        return
    if target.column_span != 1 or target.row_span != 1:
        raise HwpLiveError(
            "이미 격자선을 덮는 병합 셀은 equal로 나눌 수 없습니다. split_mode='existing_grid'를 사용하세요"
        )


def _dimension_tolerance(value: int | None) -> int:
    return 0 if value is None else max(2, round(abs(value) * 0.005))


def _same_outer_size(before: int | None, after: int | None) -> bool:
    if before is None or after is None:
        return before == after
    return abs(after - before) <= _dimension_tolerance(before)


def verify_split_transition(
    before: TableTopology,
    after: TableTopology,
    split: SplitSpec,
) -> None:
    verify_split_preflight(before, split)
    if split.split_mode == "existing_grid":
        expected_rows = before.rows
        expected_columns = before.columns
    else:
        expected_rows = before.rows + split.rows - 1
        expected_columns = before.columns + split.columns - 1
    expected_cells = before.cell_count + split.rows * split.columns - 1
    if after.columns != expected_columns:
        raise HwpLiveError(
            f"셀 분할 후 열 수가 예상 {expected_columns}에서 {after.columns}(으)로 급증하거나 달라졌습니다"
        )
    if after.rows != expected_rows:
        raise HwpLiveError(
            f"셀 분할 후 행 수가 예상 {expected_rows}에서 {after.rows}(으)로 달라졌습니다"
        )
    if after.cell_count != expected_cells:
        raise HwpLiveError(f"셀 분할 후 실제 셀 수가 예상 {expected_cells}와 다릅니다")
    if not _same_outer_size(before.width_hwpunit, after.width_hwpunit):
        raise HwpLiveError("셀 분할 후 표 전체 너비가 비정상적으로 변했습니다")
    if not _same_outer_size(before.height_hwpunit, after.height_hwpunit):
        raise HwpLiveError(
            "셀 분할 후 표 높이가 허용 오차를 넘어 폭증하거나 변했습니다"
        )
    source = before.by_address[split.cell.upper()]
    anchor = after.by_address.get(split.cell.upper())
    if anchor is None or source.text.strip() not in anchor.text:
        raise HwpLiveError(
            "셀 분할 후 원래 셀의 텍스트가 기준 셀에 보존되지 않았습니다"
        )


def verify_merge_transition(
    before: TableTopology,
    after: TableTopology,
    first: str,
    second: str,
) -> None:
    region = before.merge_region(first, second)
    start = before.by_address[first.upper()]
    end = before.by_address[second.upper()]
    start_row, start_column = table_cell_coordinate(start.address)
    end_row, end_column = table_cell_coordinate(end.address)
    anchor_address = first.upper()
    anchor = after.by_address.get(anchor_address)
    expected_cell_count = before.cell_count - len(region) + 1
    expected_row_span = end_row + end.row_span - start_row
    expected_column_span = end_column + end.column_span - start_column
    failures: list[str] = []
    if anchor is None:
        failures.append(f"owner_at_anchor(expected={anchor_address}, actual=missing)")
    if after.cell_count != expected_cell_count:
        failures.append(
            f"cell_count(expected={expected_cell_count}, actual={after.cell_count})"
        )
    if anchor is not None and anchor.row_span != expected_row_span:
        failures.append(
            f"anchor_row_span(expected={expected_row_span}, actual={anchor.row_span})"
        )
    if anchor is not None and anchor.column_span != expected_column_span:
        failures.append(
            "anchor_column_span"
            f"(expected={expected_column_span}, actual={anchor.column_span})"
        )
    if failures:
        observed_anchor = (
            "missing"
            if anchor is None
            else f"{anchor.address}(row_span={anchor.row_span},column_span={anchor.column_span})"
        )
        disappeared = tuple(
            address for address in before.by_address if address not in after.by_address
        )
        span_changes = tuple(
            (
                address,
                previous.row_span,
                previous.column_span,
                current.row_span,
                current.column_span,
            )
            for address, previous in before.by_address.items()
            if (current := after.by_address.get(address)) is not None
            and (previous.row_span, previous.column_span)
            != (current.row_span, current.column_span)
        )
        delta_items = tuple(
            [f"owner_disappeared={address}" for address in disappeared]
            + [
                f"span_changed={address}({old_row}x{old_column}->{new_row}x{new_column})"
                for address, old_row, old_column, new_row, new_column in span_changes
            ]
        )
        delta_count = len(disappeared) + len(span_changes)
        delta_preview = "; ".join(delta_items[:6]) or "없음"
        if len(delta_items) > 6:
            delta_preview = f"{delta_preview}; ..."
        raise HwpLiveError(
            "요청한 직사각형 CellTopology 영역이 실제 표에서 병합되지 않았습니다: "
            f"failed_terms=[{'; '.join(failures)}]; "
            f"after_anchor={observed_anchor}; after_cell_count={after.cell_count}; "
            f"span_delta_changed_cells={delta_count}; span_delta=[{delta_preview}]"
        )

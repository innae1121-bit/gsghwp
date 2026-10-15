from __future__ import annotations

# noqa: E501  # noqa: SIZE_OK — native format state machine; splitting obscures mutation ordering
# pyright: reportImplicitStringConcatenation=false

from dataclasses import dataclass, replace
from typing import Literal

from hwp_errors import HwpLiveError
from hwp_live_api import HwpComApplication
from hwp_live_native_action_contract import NativeActionFailure
from hwp_live_native_action_commands import (
    CaptureTableCommand,
    CellCommand,
    MovePositionCommand,
    RunCommand,
    SelectControlCommand,
)
from hwp_live_native_action_models import (
    NativeActionRequest,
    NativeCellFormat,
    NativeDetailedCell,
    NativeDetailedInspection,
    NativePosition,
    NativeSnapshot,
)
from hwp_live_native_batch import (
    execute_native_actions,
    inspect_native_structure,
    read_native_snapshot,
)
from hwp_live_native_format_commands import (
    AxisSizeTarget,
    CellRangeTarget,
    MergeCommandPlan,
    NativeFormatCommandBatch,
    NativeFormatExecutionPlan,
    SplitCommandPlan,
    TableFormatCommandPlan,
    TextFormatCommandPlan,
    build_native_format_commands,
    build_native_format_execution_plan,
)
from hwp_live_native_format_contract import (
    NativeFormatRecipeRequest as NativeFormatRecipeRequest,
    NativeFormatWorkflow,
    PreparedFormatOperation,
    is_native_format_workflow,
)
from hwp_live_native_format_inputs import (
    InputFailure,
    MergeSpec,
    TextFormatSpec,
    table_cell_address,
    table_cell_coordinate,
)
from hwp_live_native_history import execute_native_history
from hwp_merge_selection_state import (
    MergeSelectionExpectation,
    merge_selection_expectation,
    merge_selection_restore_commands,
    selection_restore_matches,
)
from hwp_live_native_format_prepare import (
    PreparationFailure,
    has_explicit_table_locator,
    prepare_native_format_operation,
)
from hwp_live_native_table_topology import (
    TableTopology,
    doomed_horizontal_merge_neighbours,
    table_formula_selection_region,
    table_topology,
    verify_merge_transition,
    verify_split_preflight,
    verify_split_transition,
)
from hwp_live_native_format_target import (
    NativeTableTargetRequest,
    TargetFailure,
    resolve_native_table_target,
)
from hwp_live_native_layout_format import cell_format_commands
from hwp_live_table_contract import TableCell
from hwp_live_table_size_protection import execute_table_size_protection
from hwp_live_table_format_copy import execute_table_format_copy
from hwp_live_text_format_verification import (
    verify_requested_text_format,
    verify_text_format,
)
from hwp_reference_layout_contract import ReferenceStyle
from hwp_reference_layout_geometry import mm_to_hwpunit
from hwp_reference_layout_text_flow import (
    reference_line_height,
    wrapped_line_count,
)
from hwp_operation_certification import certified_recipe
from hwp_operation_contract import OperationResult, OperationStatus
from hwp_operation_registry import operation_registry


class _TableTargetReanchorError(HwpLiveError):
    """The size change ran; the address it was aimed at could not be re-read.

    Separated from every other ``HwpLiveError`` in this module because it is
    the one failure that says nothing about whether the document changed. The
    commands completed. What failed afterwards is a readback: the requested
    address no longer resolves to a cell in the post-change table -- HWP had
    reflowed the grid, or absorbed the address into a merged neighbour, or
    moved the table off the page the plan recorded.

    Reported as ``partial_change`` with ``failed_step="table_target_reanchor"``
    rather than travelling as a transport error. A live 2026-08-30 call
    (operation-journal 15fc207ecc, ``hwp_format_table`` row_height_mm=18.5 on a
    row-merged table) came back ``transport_error`` / ``changed=true`` /
    ``verified=false`` with "대상 셀을 다시 찾지 못했습니다" while the row
    heights had in fact been applied -- B1 and B2 both measured 18.5mm live.
    "transport" named the wrong subsystem and the message let the model read a
    successful edit as a failed one.
    """

    def __init__(self, reason: str, *, addresses: tuple[str, ...]) -> None:
        super().__init__(reason, mutation_started=True, safe_to_repeat=False)
        self.addresses = addresses


@dataclass(frozen=True, slots=True)
class _SelectionResolvedRequest:
    request: NativeFormatRecipeRequest
    before_detail: NativeDetailedInspection


def _result(
    request: NativeFormatRecipeRequest,
    status: OperationStatus,
    message: str,
    *,
    required_inputs: tuple[str, ...] = (),
) -> OperationResult:
    workflow = request.resolution.workflow_id
    recipe = None if workflow is None else certified_recipe(workflow)
    return OperationResult(
        status=status,
        query=request.resolution.query,
        registry_entries=operation_registry().count,
        lookup_microseconds=request.resolution.lookup_microseconds,
        workflow_candidates=request.resolution.candidates,
        required_inputs=required_inputs,
        message=message,
        recipe_id=None if recipe is None else f"recipe:{recipe.recipe_id}",
        recipe_steps=request.resolution.steps,
    )


def _failure_result(
    request: NativeFormatRecipeRequest,
    failure: PreparationFailure,
) -> OperationResult:
    if isinstance(failure, InputFailure):
        return _result(
            request,
            failure.status,
            failure.message,
            required_inputs=failure.required_inputs,
        )
    return _result(
        request,
        failure.status,
        failure.message,
        required_inputs=failure.required_inputs,
    )


def _unshifted_address(
    address: str,
    last_row: int,
    last_column: int,
    row_shift: int,
    column_shift: int,
) -> bool:
    row, column = table_cell_coordinate(address)
    return (column_shift == 0 or column <= last_column) and (
        row_shift == 0 or row <= last_row
    )


def _grid_change_note(
    plan: MergeCommandPlan | SplitCommandPlan,
    before: TableTopology,
    after: TableTopology,
) -> str | None:
    """State the structure change a verified merge/split left behind.

    한/글 표는 격자가 전역이라 셀 하나를 세로로 나누면 표 전체에 세로선이
    생기고, 손대지 않은 행은 병합으로 폭을 보정한다. 사람이 UI에서 나눠도
    같으므로 이것은 결함이 아니다. 결함은 그 다음이다: 나눈 칸 오른쪽·아래
    셀의 주소가 밀렸는데 응답이 verified=true 만 말하고 넘어가면, 같은 표를
    가리키는 다음 호출이 옛 주소로 엉뚱한 셀을 친다.

    그래서 알리기만 한다. 실시간 편집 도구라 구조가 바뀐다는 이유로 거절하지
    않는다. 여기서 만드는 문장은 전부 실제로 읽어온 CellTopology에서 나오고,
    격자도 셀 수도 그대로면 아무 말도 하지 않는다.
    """
    if (
        before.rows == after.rows
        and before.columns == after.columns
        and before.cell_count == after.cell_count
    ):
        return None
    parts = [
        f"표 구조가 바뀌었습니다: 격자 {before.rows}행×{before.columns}열 → "
        f"{after.rows}행×{after.columns}열, 셀 {before.cell_count}개 → "
        f"{after.cell_count}개"
    ]
    if isinstance(plan, MergeCommandPlan):
        parts.append(
            f"{plan.merge.start}에 합쳐진 나머지 셀 주소는 이 표에서 사라졌습니다"
        )
        return ". ".join(parts)
    row_shift = after.rows - before.rows
    column_shift = after.columns - before.columns
    target = before.by_address[plan.split.cell.upper()]
    origin_row, origin_column = table_cell_coordinate(target.address)
    last_row = origin_row + target.row_span - 1
    last_column = origin_column + target.column_span - 1
    shifted = tuple(
        cell.address
        for cell in before.cells
        if not _unshifted_address(
            cell.address,
            last_row,
            last_column,
            row_shift,
            column_shift,
        )
    )
    if shifted:
        # 구체적 이동 예시(예전의 "C2 → D2")를 말하지 않는다. 그 값은 전후
        # TableTopology 의 셀을 동일성으로 대응시켜 얻은 것이 아니라, 나눈 셀
        # 위치에 전체 row_shift/column_shift 를 더해 합성한 것이었다. 진짜
        # 대응을 계산하려면 나누기 전후로 셀 식별자가 보존돼야 하는데,
        # NativeDetailedCell.list_id 는 TableInspection.cpp:785 의 firstList~
        # lastList 연속 스캔에서 나오는 문서 리스트 색인이고, TableSplitCell 이
        # 새 리스트를 만든 뒤에도 기존 색인이 그대로인지는 이 저장소의 어떤
        # 코드·문서로도 증명되지 않는다. 증명 못 하는 전제 위에 구체적 주소를
        # 지어내면 그 안내를 믿은 후속 호출이 엉뚱한 셀을 친다. 틀린 구체값보다
        # 정확한 일반 안내가 낫다.
        parts.append(
            f"{plan.split.cell} 오른쪽·아래에 있던 셀 {len(shifted)}개는 주소가"
            " 그대로가 아닙니다. 어느 주소가 어디로 갔는지는 이 응답이 알지"
            " 못합니다. 이 표를 다시 조회해 새 주소를 읽은 뒤 그 주소를 쓰세요"
        )
    # 주소가 밀린 셀은 같은 주소가 다른 셀을 가리키므로 span을 비교할 수 없다.
    # 자리를 지킨 주소만 비교해서, 한/글이 새 격자선을 덮으려고 병합해 버린
    # 셀을 센다.
    widened = tuple(
        address
        for address, cell in after.by_address.items()
        if address != plan.split.cell.upper()
        and (previous := before.by_address.get(address)) is not None
        and _unshifted_address(address, last_row, last_column, row_shift, column_shift)
        and (
            cell.column_span > previous.column_span or cell.row_span > previous.row_span
        )
    )
    if widened:
        parts.append(
            f"새 격자선을 덮도록 다른 셀 {len(widened)}개가 자동 병합됐습니다"
            f"({', '.join(sorted(widened, key=table_cell_coordinate)[:4])} 등)"
        )
    return ". ".join(parts)


def _table_cell_coverage(
    detail: NativeDetailedInspection,
    table_instance_id: str,
) -> dict[str, NativeDetailedCell]:
    """[every address a cell occupies -> that cell], for one table.

    ``detail.cells`` lists one entry per *cell*, keyed by its anchor address.
    A merged cell therefore answers to addresses that are not in that list at
    all: a row-merged A1 owns A2, and asking for A2 by name finds nothing.
    Expanding each cell over its own spans is what turns the caller's address
    back into the cell HWP now holds there.
    """
    coverage: dict[str, NativeDetailedCell] = {}
    for cell in detail.cells:
        if cell.table_instance_id != table_instance_id:
            continue
        row, column = table_cell_coordinate(cell.address)
        for row_offset in range(max(1, cell.row_span)):
            for column_offset in range(max(1, cell.column_span)):
                _ = coverage.setdefault(
                    table_cell_address(row + row_offset, column + column_offset),
                    cell,
                )
    return coverage


# The page the plan recorded, then its neighbours. A row-height change reflows
# the table, and a table that was the last thing on its page can end up one
# page further on -- the same reflow the size change was asked for. Looking
# only at the recorded page reported "cell not found" for a table that was
# intact one page away.
def _reanchor_pages(page: int) -> tuple[int, ...]:
    seen: list[int] = []
    for candidate in (page, page + 1, page - 1):
        if candidate >= 1 and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)


def _reanchored_table_cells(
    window_handle: int,
    plan: TableFormatCommandPlan,
    detail: NativeDetailedInspection,
    after_page: int,
) -> tuple[
    dict[str, NativeDetailedCell] | None,
    NativeDetailedInspection,
    int,
]:
    """Requested address -> post-change cell, re-reading pages if it has to.

    Returns ``None`` for the mapping only when an address could not be
    resolved anywhere this looked; the detail and page that were actually used
    come back with it so the caller verifies against what it read.
    """
    requested = tuple(address.upper() for address in plan.cells)
    if not requested:
        # Nothing was addressed, so nothing can fail to re-anchor. The old
        # length comparison passed vacuously here and this keeps doing so.
        return {}, detail, after_page
    page = after_page
    for candidate in _reanchor_pages(after_page):
        if candidate == after_page:
            current = detail
        else:
            try:
                current = inspect_native_structure(window_handle, candidate)
            except HwpLiveError:
                # A neighbour page that cannot be read is not this call's
                # failure; the recorded page's own verdict still stands.
                continue
        if current is None:
            continue
        coverage = _table_cell_coverage(current, plan.table.instance_id)
        if not coverage:
            continue
        resolved = {
            address: coverage[address] for address in requested if address in coverage
        }
        if len(resolved) == len(requested):
            return resolved, current, candidate
        detail, page = current, candidate
    return None, detail, page


def _verify_structural_plan(
    prepared: PreparedFormatOperation,
    window_handle: int,
    before_detail: NativeDetailedInspection | None,
    after_page: int,
) -> str | None:
    plan = prepared.plan
    if isinstance(plan, TableFormatCommandPlan):
        formatting = plan.formatting
        if formatting.row_height_mm is None and formatting.column_width_mm is None:
            return None
        detail = inspect_native_structure(window_handle, after_page)
        if detail is None:
            raise HwpLiveError("표 행·열 크기 변경 후 실제 셀 속성을 읽지 못했습니다")
        # Re-anchor before judging. The old check compared
        # ``item.address in plan.cells`` against ``len(plan.cells)``, which
        # cannot survive its own success: a row-height change reflows the
        # table, a merged neighbour owns the requested address instead of
        # answering to it, and the table can land on the next page. All three
        # read as "cell not found" and were reported as a transport error over
        # row heights that had been applied.
        resolved, detail, _ = _reanchored_table_cells(
            window_handle,
            plan,
            detail,
            after_page,
        )
        if resolved is None:
            raise _TableTargetReanchorError(
                "표 행·열 크기 변경 명령은 실행됐지만 대상 셀 주소를 변경 후"
                " 표에서 다시 찾지 못해 결과를 확인하지 못했습니다."
                " 요청한 서식이 이미 적용됐을 수 있습니다 — 같은 호출을"
                " 반복하기 전에 이 표를 다시 조회해 실제 셀 크기를 읽으세요"
                "; mutation_started=true",
                addresses=tuple(address.upper() for address in plan.cells),
            )
        # One merged cell can own several requested addresses; verify each
        # distinct cell once.
        cells = tuple({id(cell): cell for cell in resolved.values()}.values())
        expected_width = (
            None
            if formatting.column_width_mm is None
            else round(formatting.column_width_mm * 283.4645669)
        )
        expected_height = (
            None
            if formatting.row_height_mm is None
            else round(formatting.row_height_mm * 283.4645669)
        )
        if expected_width is not None and any(
            cell.width_hwpunit is None
            or abs(cell.width_hwpunit - expected_width * cell.column_span)
            > cell.column_span
            for cell in cells
        ):
            raise HwpLiveError(
                "요청한 열 너비와 한컴의 실제 셀 너비가 일치하지 않습니다"
            )
        deviations: list[_RowHeightDeviation] = []
        if expected_height is not None:
            formats = _cell_format_index(detail, plan.table.instance_id)
            for cell in cells:
                expected = expected_height * cell.row_span
                verdict = _row_height_verdict(
                    cell.height_hwpunit,
                    expected,
                    cell.row_span,
                )
                if verdict == "invalid":
                    raise HwpLiveError(
                        "요청한 행 높이보다 한컴의 실제 셀 높이가 작거나 읽히지 않았습니다"
                    )
                if verdict == "adjusted":
                    assert cell.height_hwpunit is not None
                    deviations.append(
                        _row_height_deviation(
                            cell,
                            expected,
                            cell.height_hwpunit,
                            formats.get(cell.address.upper()),
                            formatting.formatting,
                        )
                    )
        if deviations:
            return _row_height_note(tuple(deviations))
        return None
    if isinstance(plan, (MergeCommandPlan, SplitCommandPlan)):
        table = plan.table
        if before_detail is None:
            raise HwpLiveError("표 구조 변경 전 대상 표의 실제 셀 구조가 없습니다")
        before_topology = table_topology(before_detail, table.instance_id)
        # Only a CellTopology we actually read back proves this call mutated the
        # table. Undo(1) reverts the newest history entry whatever it is, so
        # without that proof it would revert an earlier, unrelated edit instead.
        after_topology: TableTopology | None = None
        observed_mutation = False
        try:
            detail = inspect_native_structure(window_handle, after_page)
            if detail is None or not any(
                control.instance_id == table.instance_id for control in detail.controls
            ):
                raise HwpLiveError(
                    "표 구조 변경 후 대상 표를 네이티브 구조에서 확인하지 못했습니다"
                )
            after_topology = table_topology(detail, table.instance_id)
            observed_mutation = after_topology != before_topology
            if isinstance(plan, MergeCommandPlan):
                verify_merge_transition(
                    before_topology,
                    after_topology,
                    plan.merge.start,
                    plan.merge.end,
                )
            else:
                verify_split_transition(before_topology, after_topology, plan.split)
        except HwpLiveError as verification_error:
            if not observed_mutation:
                # Nothing recovers here, and that is deliberate: the caller has
                # to know an automatic recovery was declined, and that a
                # partial change of this call may still be in the document.
                raise HwpLiveError(
                    f"{verification_error}. "
                    + (
                        "작업 후 대상 표 구조를 읽지 못했습니다"
                        if after_topology is None
                        else "작업 후 대상 표의 CellTopology가 작업 전과 같습니다"
                    )
                    + ". 이 호출이 표를 바꿨다는 네이티브 증거가 없어 자동 Undo를"
                    + " 실행하지 않았습니다(실행하면 무관한 이전 편집을 되돌립니다)."
                    + " 자동 복구를 하지 않았으므로 문서가 작업 전 상태인지는"
                    + " 확정할 수 없습니다"
                ) from verification_error
            try:
                _ = execute_native_history(window_handle, "undo", 1)
                restored = inspect_native_structure(window_handle, table.page)
                if (
                    restored is None
                    or table_topology(restored, table.instance_id) != before_topology
                ):
                    raise HwpLiveError(
                        "자동 Undo 후 작업 전 CellTopology가 복원되지 않았습니다"
                    )
            except HwpLiveError as rollback_error:
                raise HwpLiveError(
                    f"{verification_error}. 자동 Undo 복구 검증에도 실패했습니다: {rollback_error}"
                ) from rollback_error
            # The evidence is one CellTopology comparison: grid, spans and the
            # cell text the native layer returns. Cell formatting and anything
            # outside this table were never read, so the claim stops here
            # instead of declaring the whole document pre-edit state.
            raise HwpLiveError(
                f"{verification_error}. 자동 Undo로 복구했습니다: 대상 표의"
                + " CellTopology(격자·span·셀 텍스트)가 작업 전과 같습니다."
                + " 셀 서식과 이 표 밖 문서 내용은 확인하지 않았습니다"
                + "; mutation_started=false",
                mutation_started=False,
            ) from verification_error
        return _grid_change_note(plan, before_topology, after_topology)
    return None


def _row_height_verdict(
    actual_hwpunit: int | None,
    expected_hwpunit: int,
    row_span: int,
) -> Literal["exact", "adjusted", "invalid"]:
    if actual_hwpunit is None or actual_hwpunit < expected_hwpunit - row_span:
        return "invalid"
    if actual_hwpunit > expected_hwpunit + row_span:
        return "adjusted"
    return "exact"


@dataclass(frozen=True, slots=True)
class _RowHeightDeviation:
    """One row Hancom left taller than the request asked for.

    ``content_minimum_hwpunit`` is a *lower* bound on the height this cell's
    own content needs. Everything in it was read back rather than assumed: the
    cell text and width come from the post-edit CellTopology, the font from the
    request itself or from the cell format the bridge sampled, the margins from
    the same two places. The wrap and line-height arithmetic is
    ``hwp_reference_layout_text_flow``'s, not a second copy of it -- two models
    in one repository that answer differently about the same paragraph is the
    defect regardless of which is closer. It stays a lower bound because the
    line spacing of an existing run is not on the wire, and 100% is the
    smallest Hancom can be laying out with unless the request set one.

    That one-sidedness is what keeps the verdict honest. ``content_fit`` is
    proved: the row is no taller than its own content already demands, so the
    excess over the request is explained. ``unexplained`` is not the opposite
    claim -- the excess may still be content this bound cannot see, or the row
    height may have been ignored or clamped. The caller is told which rows
    carry proof and which do not; neither refuses the operation, because a
    verified format application is not a failure just because the row grew.
    """

    address: str
    requested_hwpunit: int
    actual_hwpunit: int
    content_minimum_hwpunit: int | None
    verdict: Literal["content_fit", "unexplained", "unknown"]


# Unexplained rows are printed first and the cap is applied after that
# ordering, so the rows a caller has to act on are never the ones dropped.
_ROW_HEIGHT_NOTE_LIMIT = 20
_ROW_HEIGHT_VERDICT_ORDER: tuple[
    Literal["content_fit", "unexplained", "unknown"],
    ...,
] = ("unexplained", "unknown", "content_fit")


def _cell_format_index(
    detail: NativeDetailedInspection,
    table_instance_id: str,
) -> dict[str, NativeCellFormat]:
    """Sampled cell formats of one table, keyed by every address they cover.

    ``NativeCellFormat`` is a sample, not a summary: the bridge reports a few
    cells per table picked by position. A missing address therefore means "not
    sampled", never "no formatting", which is why a missing entry downgrades a
    row to ``unknown`` instead of being treated as zero margins.
    """
    index: dict[str, NativeCellFormat] = {}
    for cell_format in detail.cell_formats:
        if cell_format.table_instance_id != table_instance_id:
            continue
        for address in cell_format.addresses:
            if address.upper() not in index:
                index[address.upper()] = cell_format
    return index


def _cell_margins(
    cell_format: NativeCellFormat | None,
    requested: TableCell,
) -> tuple[int, int]:
    """(horizontal, vertical) cell margin totals in HWPUNIT."""
    padding = requested.padding
    if padding is not None:
        # The same call set the padding, so this is what the cell now has.
        return (
            mm_to_hwpunit(padding.left_mm) + mm_to_hwpunit(padding.right_mm),
            mm_to_hwpunit(padding.top_mm) + mm_to_hwpunit(padding.bottom_mm),
        )
    if cell_format is None:
        return 0, 0
    return (
        (cell_format.margin_left_hwpunit or 0)
        + (cell_format.margin_right_hwpunit or 0),
        (cell_format.margin_top_hwpunit or 0)
        + (cell_format.margin_bottom_hwpunit or 0),
    )


def _content_minimum_height(
    cell: NativeDetailedCell,
    cell_format: NativeCellFormat | None,
    requested: TableCell,
) -> int | None:
    """Smallest height this cell's own content can occupy, or None.

    None means the inputs to say anything were not there -- no font on the
    request and none sampled for this cell. Guessing one would turn a row that
    was silently clamped into a row that "fits its content".
    """
    font_hwpunit = (
        round(requested.font_size_pt * 100)
        if requested.font_size_pt is not None
        else (None if cell_format is None else cell_format.character_height)
    )
    # ReferenceStyle bounds font size at 1..96pt and line spacing at 50..500%.
    # A value outside them is not a smaller font, it is a value this model was
    # never measured against, so it produces no verdict.
    if font_hwpunit is None or not 100 <= font_hwpunit <= 9_600:
        return None
    line_spacing = requested.line_spacing_percent
    if line_spacing is not None and not 50 <= line_spacing <= 500:
        return None
    style = ReferenceStyle(
        key="cell",
        font_size_pt=font_hwpunit / 100,
        line_spacing_type="percent",
        line_spacing_percent=line_spacing,
    )
    line_height = reference_line_height(style)
    if line_height <= 0:
        return None
    horizontal, vertical = _cell_margins(cell_format, requested)
    content_width = (
        None if cell.width_hwpunit is None else max(1, cell.width_hwpunit - horizontal)
    )
    lines = wrapped_line_count(cell.text, style, content_width)
    return lines * line_height + vertical


def _row_height_deviation(
    cell: NativeDetailedCell,
    requested_hwpunit: int,
    actual_hwpunit: int,
    cell_format: NativeCellFormat | None,
    requested: TableCell,
) -> _RowHeightDeviation:
    minimum = _content_minimum_height(cell, cell_format, requested)
    verdict: Literal["content_fit", "unexplained", "unknown"]
    if minimum is None:
        verdict = "unknown"
    elif actual_hwpunit <= minimum + cell.row_span:
        verdict = "content_fit"
    else:
        verdict = "unexplained"
    return _RowHeightDeviation(
        cell.address,
        requested_hwpunit,
        actual_hwpunit,
        minimum,
        verdict,
    )


def _row_height_note(deviations: tuple[_RowHeightDeviation, ...]) -> str:
    counts = {
        verdict: sum(1 for item in deviations if item.verdict == verdict)
        for verdict in _ROW_HEIGHT_VERDICT_ORDER
    }
    ordered = sorted(
        deviations,
        key=lambda item: (
            _ROW_HEIGHT_VERDICT_ORDER.index(item.verdict),
            item.address,
        ),
    )
    rows = "; ".join(
        f"{item.address} verdict={item.verdict}"
        f" requested={item.requested_hwpunit}"
        f" actual={item.actual_hwpunit}"
        " content_minimum="
        + (
            "null"
            if item.content_minimum_hwpunit is None
            else str(item.content_minimum_hwpunit)
        )
        for item in ordered[:_ROW_HEIGHT_NOTE_LIMIT]
    )
    omitted = len(ordered) - len(ordered[:_ROW_HEIGHT_NOTE_LIMIT])
    note = (
        "한컴의 실제 행 높이가 요청보다 큽니다"
        f"(row_height_deviations={len(deviations)}"
        f", content_fit={counts['content_fit']}"
        f", unexplained={counts['unexplained']}"
        f", unknown={counts['unknown']})"
        f"[{rows}" + (f"; 외 {omitted}행 생략" if omitted else "") + "]"
    )
    if counts["unexplained"] or counts["unknown"]:
        note += (
            ". row_height_warning=true: 이 행들이 커진 이유가 셀 내용이라는"
            " 근거가 이 응답에 없습니다. 요청한 행 높이가 적용되지 않았거나"
            " 한컴이 클램프했을 수 있으니 필요하면 표를 다시 조회해"
            " 확인하세요. content_fit 행만 셀 내용이 요구하는 최소 높이"
            " 안에 있음을 확인했습니다"
        )
    return note


def _single_cell_target(address: str) -> CellRangeTarget:
    return CellRangeTarget(f"text_color:{address}", address, 0, 0, (address,))


def _rectangular_cell_blocks(
    plan: TableFormatCommandPlan,
    detail: NativeDetailedInspection,
) -> tuple[CellRangeTarget, ...] | None:
    """Largest rectangles that cover ``plan.cells`` and nothing else.

    One merged cell anywhere in a photo table used to force every cell of the
    request onto its own select/read/Cancel trio. It no longer does: the
    unmerged cells are tiled into maximal rectangles and only the merged ones
    keep a selection of their own.

    The tiling never crosses a merged cell, and that restriction is the
    accuracy invariant, not a leftover. ``TableCellBlockExtend`` plus N
    ``TableRightCell``/``TableLowerCell`` steps is a walk through Hancom's own
    neighbour graph, and ``NativeDetailedCell`` does not expose that graph --
    only addresses and spans. Between span-1 cells the walk is forced (the cell
    right of (r, c) is the cell at (r, c+1) and there is exactly one), so the
    block provably covers the addresses it names. Across a merged rectangle it
    is an inference, and an inference that lands one cell off would verify the
    wrong cells and report the answer as fact.

    None means no rectangle could be proved and the caller keeps its
    cell-at-a-time path.
    """
    try:
        topology = table_topology(detail, plan.table.instance_id)
    except HwpLiveError:
        return None
    selected: dict[str, NativeDetailedCell] = {}
    for address in plan.cells:
        cell = topology.by_address.get(address.upper())
        if cell is None:
            return None
        selected[cell.address.upper()] = cell
    if not selected:
        return None
    grid = {
        table_cell_coordinate(cell.address): address
        for address, cell in selected.items()
        if cell.row_span == 1 and cell.column_span == 1
    }
    taken: set[tuple[int, int]] = set()
    targets: list[CellRangeTarget] = []
    for position in sorted(grid):
        if position in taken:
            continue
        row, column = position
        width = 1
        while (row, column + width) in grid and (row, column + width) not in taken:
            width += 1
        height = 1
        while all(
            (row + height, column + offset) in grid
            and (row + height, column + offset) not in taken
            for offset in range(width)
        ):
            height += 1
        covered = tuple(
            (row + down, column + right)
            for down in range(height)
            for right in range(width)
        )
        taken.update(covered)
        targets.append(
            CellRangeTarget(
                f"text_color:{grid[position]}",
                grid[position],
                width - 1,
                height - 1,
                tuple(grid[item] for item in covered),
            )
        )
    targets.extend(
        _single_cell_target(address)
        for address, cell in selected.items()
        if cell.row_span != 1 or cell.column_span != 1
    )
    verified = tuple(address for target in targets for address in target.addresses)
    if frozenset(verified) != frozenset(selected) or len(verified) != len(selected):
        return None
    return tuple(
        sorted(targets, key=lambda target: table_cell_coordinate(target.anchor))
    )


def _text_color_verification_targets(
    plan: TableFormatCommandPlan,
    detail: NativeDetailedInspection | None,
) -> tuple[CellRangeTarget, ...]:
    if plan.cell_geometry_targets:
        return plan.cell_geometry_targets
    blocks = None if detail is None else _rectangular_cell_blocks(plan, detail)
    if blocks is not None:
        return blocks
    return tuple(_single_cell_target(cell) for cell in plan.cells)


def _read_cell_block_format(
    request: NativeFormatRecipeRequest,
    plan: TableFormatCommandPlan,
    target: CellRangeTarget,
) -> NativeSnapshot:
    selection_commands = (
        SelectControlCommand(plan.table.instance_id),
        CaptureTableCommand(),
        CellCommand(target.anchor),
        RunCommand("TableCellBlock"),
        *(
            (RunCommand("TableCellBlockExtend"),)
            if target.right_steps or target.down_steps
            else ()
        ),
        *(RunCommand("TableRightCell") for _ in range(target.right_steps)),
        *(RunCommand("TableLowerCell") for _ in range(target.down_steps)),
    )
    selected = execute_native_actions(
        request.candidate.window_handle,
        NativeActionRequest(
            request.routing_page.document_id,
            request.routing_page.full_name,
            selection_commands,
        ),
        minimum_version=9,
    )
    if selected is None:
        raise HwpLiveError("표 기존 글자색 검증 대상을 선택하지 못했습니다")
    try:
        observed = read_native_snapshot(request.candidate.window_handle)
        if observed is None:
            raise HwpLiveError("표 기존 글자색 적용 결과를 읽지 못했습니다")
        return observed
    finally:
        cancelled = execute_native_actions(
            request.candidate.window_handle,
            NativeActionRequest(
                request.routing_page.document_id,
                request.routing_page.full_name,
                (RunCommand("Cancel"),),
            ),
            minimum_version=9,
        )
        if cancelled is None:
            raise HwpLiveError("표 기존 글자색 검증 선택을 해제하지 못했습니다")


def _verify_table_text_color(
    request: NativeFormatRecipeRequest,
    plan: TableFormatCommandPlan,
    detail: NativeDetailedInspection | None = None,
) -> None:
    color = plan.formatting.formatting.text_color
    if color is None:
        return
    requested = TextFormatSpec(None, None, None, color, "inherit", None)
    for target in _text_color_verification_targets(plan, detail):
        observed = _read_cell_block_format(request, plan, target)
        try:
            verify_requested_text_format(requested, observed)
        except HwpLiveError as block_mismatch:
            if len(target.addresses) < 2:
                raise
            # A block selection reports one CharShape for the whole region.
            # Where the runs inside it differ, Hancom answers with a single
            # value and says nothing about which cells disagreed, so a
            # mismatch on a block is not yet evidence that the colour failed
            # to apply. Re-read the cells one at a time -- once, and only on
            # this path -- and let that decide. Round trips are the thing
            # being saved here, never the verdict.
            for address in target.addresses:
                cell_observed = _read_cell_block_format(
                    request,
                    plan,
                    _single_cell_target(address),
                )
                try:
                    verify_requested_text_format(requested, cell_observed)
                except HwpLiveError as cell_mismatch:
                    raise cell_mismatch from block_mismatch


def _selected_structure_addresses(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
) -> tuple[tuple[str, ...], NativeDetailedInspection | None] | InputFailure:
    """Cell addresses the live selection covers inside the target table.

    This resolves, it does not validate. The only judgement is made by
    ``TableTopology.selection_region_by_list_ids``, the same function an
    explicit address pair reaches through ``topology_preflight``: it already
    refuses a selection whose endpoints are not cells of this table (selection
    outside the table, or spanning two tables) and a region that is not a
    gap-free rectangle. Adding a second copy of those rules here would let the
    selection path drift away from the address path, so there is none.

    An empty result means the selection cannot supply what the caller omitted.
    Nothing is rejected for that; the request is left exactly as it arrived so
    the ordinary missing-input failure reports it.
    """
    resolved = resolve_native_table_target(
        NativeTableTargetRequest(
            candidate=request.candidate,
            routing_page=request.routing_page,
            target=request.target,
            snapshot_control_type=before.control_type,
            snapshot_control_id=before.control_instance_id,
        )
    )
    if isinstance(resolved, TargetFailure):
        # prepare_native_format_operation resolves the same target and reports
        # this failure itself, so it is not duplicated here.
        return (), None
    detail = inspect_native_structure(request.candidate.window_handle, resolved.page)
    if detail is None:
        return (), None
    selection = before.selection
    try:
        topology = table_topology(detail, resolved.instance_id)
        if (
            (selection.mode & 0x0F) == 3
            and selection.cell_addresses
            and before.control_instance_id == resolved.instance_id
        ):
            return topology.selection_region_by_addresses(
                selection.cell_addresses
            ), detail
        return (
            topology.selection_region_by_list_ids(
                selection.start.list_id,
                selection.end.list_id,
            ),
            detail,
        )
    except HwpLiveError as error:
        return InputFailure("schema_conflict", str(error))


def _request_with_selected_cells(
    workflow: NativeFormatWorkflow,
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
) -> NativeFormatRecipeRequest | _SelectionResolvedRequest | InputFailure:
    """Fill omitted merge/split cell addresses from the live cell selection.

    Only names the caller left out are filled in. Addresses the caller supplied
    are never touched, so a request that carries every address takes
    byte-identical parameters into ``prepare_native_format_operation`` and
    never reads the selection at all.

    A merge that named one corner and left the other out used to be sent on
    unchanged, and the missing-input failure that came back is exactly what
    made models invent the second address. The completed pair is validated by
    ``merge_region`` like any other pair; there is no rule that applies only
    because an address came from the selection.
    """
    if workflow == "table.merge_cells":
        missing = tuple(
            name for name in ("start", "end") if name not in request.parameters
        )
    elif workflow == "table.split_cells":
        missing = () if "cell" in request.parameters else ("cell",)
    else:
        return request
    if not missing:
        return request
    selected = _selected_structure_addresses(request, before)
    if isinstance(selected, InputFailure):
        return selected
    region, before_detail = selected
    if not region:
        return request
    if workflow == "table.merge_cells":
        # A caret with no cell block resolves to one cell, so a pair completed
        # from it names the same cell twice and parse_merge rejects it exactly
        # as it rejects a caller that sent the same address twice.
        corners = {"start": region[0], "end": region[-1]}
        added = {name: corners[name] for name in missing}
    elif len(region) == 1:
        added = {"cell": region[0]}
    else:
        # A multi-cell selection does not name the one cell a split applies to.
        # Nothing is injected, so parse_split reports the missing input.
        return request
    resolved_request = replace(request, parameters={**request.parameters, **added})
    if before_detail is None:
        return resolved_request
    return _SelectionResolvedRequest(resolved_request, before_detail)


def _normalized_merge_plan(
    prepared: PreparedFormatOperation,
    before_detail: NativeDetailedInspection,
) -> PreparedFormatOperation | InputFailure:
    """Restore the rectangle when the two merge corners arrive back to front.

    A merged cell covers slots its own address never names, so the rectangle
    cannot be recovered by sorting the two address strings. The corners are
    resolved against the live CellTopology instead, which rejects anything that
    is not a gap-free rectangle of whole cells.
    """
    plan = prepared.plan
    assert isinstance(plan, MergeCommandPlan)
    start, end = plan.merge.start, plan.merge.end
    start_row, start_column = table_cell_coordinate(start)
    end_row, end_column = table_cell_coordinate(end)
    if end_row >= start_row and end_column >= start_column:
        return prepared
    try:
        topology = table_topology(before_detail, plan.table.instance_id)
        first = topology.by_address.get(start.upper())
        second = topology.by_address.get(end.upper())
        if first is None or second is None:
            raise HwpLiveError("병합 시작·끝 셀이 실제 표 토폴로지에 없습니다")
        # Explicit merge endpoints must be physical cells even when reversed;
        # grid aliases are only meaningful in an observed native selection.
        region = topology.selection_region_by_list_ids(first.list_id, second.list_id)
    except HwpLiveError as error:
        return InputFailure("schema_conflict", str(error))
    if len(region) < 2:
        return InputFailure(
            "schema_conflict",
            "병합 시작·끝 주소가 서로 다른 두 셀을 덮지 않습니다",
        )
    return PreparedFormatOperation(
        MergeCommandPlan(MergeSpec(region[0], region[-1]), plan.table),
        prepared.target_id,
        prepared.target_basis,
        (region[0], region[-1]),
    )


def topology_preflight(
    prepared: PreparedFormatOperation,
    before_detail: NativeDetailedInspection,
) -> InputFailure | None:
    plan = prepared.plan
    try:
        match plan:  # noqa: E501  # noqa: MATCH_OK — closed union is fully enumerated
            case MergeCommandPlan():
                topology = table_topology(before_detail, plan.table.instance_id)
                _ = topology.merge_region(plan.merge.start, plan.merge.end)
                doomed_neighbours = doomed_horizontal_merge_neighbours(
                    topology,
                    plan.merge.start,
                    plan.merge.end,
                )
                if doomed_neighbours is not None:
                    start = topology.by_address[plan.merge.start.upper()]
                    end = topology.by_address[plan.merge.end.upper()]
                    _, left = table_cell_coordinate(start.address)
                    _, end_column = table_cell_coordinate(end.address)
                    right = end_column + end.column_span - 1
                    neighbours = "; ".join(doomed_neighbours)
                    return InputFailure(
                        "needs_input",
                        "요청한 병합은 HWP 선택 엔진으로 적용할 수 없습니다. "
                        f"단일 행 가로 병합 열 범위 [{left}..{right}]의 위·아래 "
                        "인접 위치가 모두 같은 열 범위를 덮는 기존 병합 span이거나 "
                        f"표 경계입니다. 인접 행 현재 span: {neighbours}. "
                        "시도하면 인접 span 메타데이터가 손상되며 사후 검증의 "
                        "자동 Undo가 복원합니다. 이미 나뉜 제목 칸의 입력은 "
                        "hwp_fill_table을 사용하세요. hwp_restructure_table은 "
                        "사진·라벨 전용이며 본문이 있는 표의 병합 우회에 사용하지 마세요. "
                        "mutation_started=false",
                    )
            case SplitCommandPlan():
                verify_split_preflight(
                    table_topology(before_detail, plan.table.instance_id),
                    plan.split,
                )
            case TableFormatCommandPlan() | TextFormatCommandPlan():
                pass
    except HwpLiveError as error:
        return InputFailure("schema_conflict", str(error))
    return None


def _selected_cells_failure(
    error: HwpLiveError,
    cell_address_error: str,
) -> HwpLiveError:
    reason = error.reason.partition(" 표 서식은 ")[0].rstrip()
    native_reason = cell_address_error.strip()
    if native_reason and native_reason not in reason:
        reason = f"{reason}. 네이티브 셀 주소 검사 오류: {native_reason}"
    return HwpLiveError(reason, mutation_started=error.mutation_started)


def _resolve_selected_table_cells(
    prepared: PreparedFormatOperation,
    before: NativeSnapshot,
    detail: NativeDetailedInspection,
    application: HwpComApplication | None = None,
    *,
    explicit_target: bool = False,
) -> PreparedFormatOperation | InputFailure:
    plan = prepared.plan
    if not isinstance(plan, TableFormatCommandPlan) or plan.cells:
        return prepared
    selection_mode = before.selection.mode
    base_mode = selection_mode & 0x0F
    strict_selection = bool(selection_mode & 0x10)
    try:
        topology = table_topology(detail, plan.table.instance_id)
        if explicit_target:
            addresses = tuple(cell.address for cell in topology.cells)
        else:
            if selection_mode == 0 and application is not None:
                selection_mode = int(application.SelectionMode)
                base_mode = selection_mode & 0x0F
                strict_selection = bool(selection_mode & 0x10)
            if base_mode == 3:
                if before.selection.cell_addresses:
                    addresses = topology.selection_region_by_addresses(
                        before.selection.cell_addresses
                    )
                elif before.selection.selected:
                    addresses = topology.selection_region_by_list_ids(
                        before.selection.start.list_id,
                        before.selection.end.list_id,
                    )
                elif before.selection.cell_address_error:
                    raise HwpLiveError(before.selection.cell_address_error)
                elif strict_selection and application is not None:
                    addresses = table_formula_selection_region(application, topology)
                else:
                    raise HwpLiveError("현재 선택한 표 셀 범위를 확인하지 못했습니다")
            elif base_mode == 4 and before.control_type == "tbl":
                addresses = tuple(cell.address for cell in topology.cells)
            else:
                return InputFailure(
                    "needs_input",
                    "현재 선택한 표 셀 범위를 확인하지 못했습니다",
                    ("inputs.parameters.cell",),
                )
    except HwpLiveError as error:
        if not explicit_target and base_mode == 3:
            error = _selected_cells_failure(
                error,
                before.selection.cell_address_error,
            )
        return InputFailure("schema_conflict", str(error))
    if not addresses:
        if explicit_target:
            return InputFailure(
                "schema_conflict",
                "지정한 표의 실제 셀 주소를 네이티브 topology에서 확인하지 못했습니다",
            )
        return InputFailure(
            "needs_input",
            "현재 선택한 표 셀이 없습니다",
            ("inputs.parameters.cell",),
        )
    resolved_plan = TableFormatCommandPlan(plan.formatting, plan.table, addresses)
    return PreparedFormatOperation(
        resolved_plan,
        prepared.target_id,
        prepared.target_basis,
        addresses,
    )


def _topology_axis_targets(
    prepared: PreparedFormatOperation,
    detail: NativeDetailedInspection,
    *,
    column: bool,
) -> tuple[AxisSizeTarget, ...]:
    plan = prepared.plan
    assert isinstance(plan, TableFormatCommandPlan)
    topology = table_topology(detail, plan.table.instance_id)
    selected = tuple(topology.by_address.get(address.upper()) for address in plan.cells)
    if any(cell is None for cell in selected):
        raise HwpLiveError("표 크기 변경 대상 셀이 실제 CellTopology에 없습니다")
    physical = tuple(cell for cell in selected if cell is not None)
    axes = sorted(
        {
            axis
            for cell in physical
            for axis in range(
                (
                    table_cell_coordinate(cell.address)[1]
                    if column
                    else table_cell_coordinate(cell.address)[0]
                ),
                (
                    table_cell_coordinate(cell.address)[1] + cell.column_span
                    if column
                    else table_cell_coordinate(cell.address)[0] + cell.row_span
                ),
            )
        }
    )
    label = "column" if column else "row"
    targets: list[AxisSizeTarget] = []
    for axis in axes:
        anchor = next(
            (
                cell
                for cell in topology.cells
                if (
                    table_cell_coordinate(cell.address)[1]
                    if column
                    else table_cell_coordinate(cell.address)[0]
                )
                == axis
                and (cell.column_span if column else cell.row_span) == 1
            ),
            None,
        )
        if anchor is None:
            raise HwpLiveError(
                f"표 {label} {axis}의 크기를 독립적으로 변경할 "
                "single-span 셀이 없습니다. 병합 셀로 축을 선택하면 다른 "
                "행·열까지 바뀔 수 있어 변경 전에 중단했습니다."
            )
        affected = tuple(
            cell.address
            for cell in physical
            if (
                (
                    table_cell_coordinate(cell.address)[1]
                    if column
                    else table_cell_coordinate(cell.address)[0]
                )
                <= axis
                < (
                    table_cell_coordinate(cell.address)[1] + cell.column_span
                    if column
                    else table_cell_coordinate(cell.address)[0] + cell.row_span
                )
            )
        )
        targets.append(
            AxisSizeTarget(
                f"{label}:{axis}",
                anchor.address,
                affected,
            )
        )
    return tuple(targets)


def _with_topology_size_targets(
    prepared: PreparedFormatOperation,
    detail: NativeDetailedInspection,
) -> PreparedFormatOperation:
    plan = prepared.plan
    if not isinstance(plan, TableFormatCommandPlan):
        return prepared
    formatted = plan.formatting
    column_targets = (
        None
        if formatted.column_width_mm is None
        else _topology_axis_targets(prepared, detail, column=True)
    )
    row_targets = (
        None
        if formatted.row_height_mm is None
        else _topology_axis_targets(prepared, detail, column=False)
    )
    if column_targets is None and row_targets is None:
        return prepared
    resolved_plan = TableFormatCommandPlan(
        plan.formatting,
        plan.table,
        plan.cells,
        column_targets,
        row_targets,
        plan.cell_geometry_targets,
        plan.table_cell_count,
    )
    return PreparedFormatOperation(
        resolved_plan,
        prepared.target_id,
        prepared.target_basis,
        prepared.updated_addresses,
    )


def _topology_cell_geometry_targets(
    prepared: PreparedFormatOperation,
    detail: NativeDetailedInspection,
) -> tuple[tuple[CellRangeTarget, ...] | None, int]:
    plan = prepared.plan
    assert isinstance(plan, TableFormatCommandPlan)
    topology = table_topology(detail, plan.table.instance_id)
    selected = tuple(topology.by_address.get(address.upper()) for address in plan.cells)
    if any(cell is None for cell in selected):
        raise HwpLiveError("셀 geometry 변경 대상이 실제 CellTopology에 없습니다")
    if plan.formatting.formatting.borders is not None:
        # Border sides describe each requested cell. HWP applies those same
        # parameters to the outer edges of a selected block, so coalescing
        # cells would silently omit the interior edges.
        return None, topology.cell_count
    physical = tuple(cell for cell in selected if cell is not None)
    if any(cell.row_span != 1 or cell.column_span != 1 for cell in physical):
        # NativeDetailedCell does not expose the native right/down neighbour
        # graph. A merged rectangle therefore cannot prove the exact block
        # extension path in Python and must retain the cell-local sequence.
        return None, topology.cell_count
    try:
        region = topology.selection_region_by_addresses(plan.cells)
    except HwpLiveError:
        return None, topology.cell_count
    if frozenset(region) != frozenset(address.upper() for address in plan.cells):
        return None, topology.cell_count
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
        return None, topology.cell_count
    current = first
    right_steps = 0
    while table_cell_coordinate(current.address)[1] + current.column_span - 1 < right:
        next_column = table_cell_coordinate(current.address)[1] + current.column_span
        next_cell = cell_at(top, next_column)
        if next_cell is None or next_cell is current:
            return None, topology.cell_count
        right_steps += 1
        current = next_cell
    down_steps = 0
    while table_cell_coordinate(current.address)[0] + current.row_span - 1 < bottom:
        next_row = table_cell_coordinate(current.address)[0] + current.row_span
        next_cell = cell_at(next_row, right)
        if next_cell is None or next_cell is current:
            return None, topology.cell_count
        down_steps += 1
        current = next_cell
    if current.address != endpoint.address:
        return None, topology.cell_count
    return (
        (
            CellRangeTarget(
                "cell_geometry:range",
                first.address,
                right_steps,
                down_steps,
                plan.cells,
            ),
        ),
        topology.cell_count,
    )


def _with_topology_cell_geometry_targets(
    prepared: PreparedFormatOperation,
    detail: NativeDetailedInspection,
) -> PreparedFormatOperation:
    plan = prepared.plan
    if (
        not isinstance(plan, TableFormatCommandPlan)
        or len(plan.cells) < 2
        or not cell_format_commands(plan.formatting.formatting)
    ):
        return prepared
    targets, table_cell_count = _topology_cell_geometry_targets(prepared, detail)
    resolved_plan = TableFormatCommandPlan(
        plan.formatting,
        plan.table,
        plan.cells,
        plan.column_size_targets,
        plan.row_size_targets,
        targets,
        table_cell_count,
    )
    return PreparedFormatOperation(
        resolved_plan,
        prepared.target_id,
        prepared.target_basis,
        prepared.updated_addresses,
    )


def _pre_mutation_failure(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    prepared: PreparedFormatOperation,
    failure: InputFailure,
) -> OperationResult:
    """Envelope for a rejection raised before any native command ran."""
    return _failure_result(request, failure).model_copy(
        update={
            "verified": False,
            "commands_executed": 0,
            "commands_completed": 0,
            "current_page": before.current_page,
            "page_count": before.page_count,
            "modified": before.modified,
            "partial_mutation": False,
            "retry_safe": True,
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
        }
    )


def _failed_table_format_batch(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    prepared: PreparedFormatOperation,
    execution: NativeFormatExecutionPlan,
    batch: NativeFormatCommandBatch,
    batch_index: int,
    completed_group_keys: set[str],
    commands_completed_before: int,
    elapsed_before: int,
    error: HwpLiveError | None,
) -> OperationResult:
    failure_commands = (
        min(error.commands_completed, len(batch.commands))
        if isinstance(error, NativeActionFailure)
        else 0
    )
    completed_group_keys.update(batch.completed_group_keys(failure_commands))
    affected_addresses = execution.affected_addresses(completed_group_keys)
    uncertain_addresses = (
        batch.uncertain_addresses(failure_commands)
        if isinstance(error, NativeActionFailure) and error.partial_mutation
        else ()
    )
    prior_mutation = bool(completed_group_keys)
    if error is None:
        partial_mutation: bool | None = prior_mutation
        retry_safe = not prior_mutation
        failed_step = "native_format_protocol"
        detail = "프로토콜 9 네이티브 실행기를 사용할 수 없습니다"
    elif isinstance(error, NativeActionFailure):
        partial_mutation = prior_mutation or error.partial_mutation
        retry_safe = error.retry_safe and not partial_mutation
        failed_step = error.failed_step or error.location or "native_format_batch"
        detail = str(error)
    else:
        partial_mutation = True if prior_mutation else None
        retry_safe = False
        failed_step = "native_format_batch"
        detail = str(error)
    detail = detail[:1_000]
    uncertain_preview = ", ".join(uncertain_addresses[:8])
    uncertain = (
        (
            uncertain_preview
            if len(uncertain_addresses) <= 8
            else f"{len(uncertain_addresses)}개 중 {uncertain_preview}, ..."
        )
        if uncertain_addresses
        else "없음(실패 명령 내부 적용 여부는 네이티브 증거가 없으면 확정 불가)"
    )
    message = (
        f"표 서식 호출 {batch_index + 1}/{len(execution.batches)}에서 실패했습니다. "
        f"완료 명령 그룹이 적용한 주소 {len(affected_addresses)}개, "
        f"실패 그룹 적용 불확실 주소: {uncertain}. {detail}"
    )
    commands_completed = commands_completed_before + failure_commands
    return _result(
        request,
        "partial_change" if partial_mutation is True else "operation_failed",
        message,
    ).model_copy(
        update={
            "changed": partial_mutation is True,
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            "verified": False,
            "commands_executed": commands_completed,
            "commands_completed": commands_completed,
            "native_elapsed_microseconds": elapsed_before,
            "current_page": before.current_page,
            "page_count": before.page_count,
            "modified": True if partial_mutation is True else before.modified,
            "partial_change": partial_mutation is True,
            "partial_mutation": partial_mutation,
            "retry_safe": retry_safe,
            "reconcile_required": partial_mutation is not False,
            "failed_step": failed_step,
            "structure_digest_before": (
                error.structure_digest_before
                if isinstance(error, NativeActionFailure)
                else None
            ),
            "structure_digest_after": (
                error.structure_digest_after
                if isinstance(error, NativeActionFailure)
                else None
            ),
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
            "updated_addresses": affected_addresses,
        }
    )


def _execute_table_format_batches(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    prepared: PreparedFormatOperation,
) -> tuple[int, int, int] | OperationResult:
    plan = prepared.plan
    assert isinstance(plan, TableFormatCommandPlan)
    execution = build_native_format_execution_plan(plan)
    completed_group_keys: set[str] = set()
    commands_completed = 0
    elapsed_microseconds = 0
    for batch_index, batch in enumerate(execution.batches):
        try:
            native = execute_native_actions(
                request.candidate.window_handle,
                NativeActionRequest(
                    request.routing_page.document_id,
                    request.routing_page.full_name,
                    batch.commands,
                ),
                minimum_version=9,
            )
        except HwpLiveError as error:
            return _failed_table_format_batch(
                request,
                before,
                prepared,
                execution,
                batch,
                batch_index,
                completed_group_keys,
                commands_completed,
                elapsed_microseconds,
                error,
            )
        if native is None:
            return _failed_table_format_batch(
                request,
                before,
                prepared,
                execution,
                batch,
                batch_index,
                completed_group_keys,
                commands_completed,
                elapsed_microseconds,
                None,
            )
        commands_completed += native.commands_executed
        elapsed_microseconds += native.elapsed_microseconds
        completed_group_keys.update(item.group.key for item in batch.groups)
    return commands_completed, elapsed_microseconds, len(execution.batches)


def _merge_restore_failure(
    request: NativeFormatRecipeRequest,
    after: NativeSnapshot | None,
    prepared: PreparedFormatOperation,
    commands_executed: int,
    elapsed_microseconds: int,
    expectation: MergeSelectionExpectation,
    restore_error: HwpLiveError | None = None,
) -> OperationResult:
    # The merge already ran, so the pre-edit snapshot's page numbers describe a
    # document that no longer exists; a merge that reflowed the table would
    # have reported the old page as the current one. `after` is the freshest
    # snapshot this call actually read back. When there is none, the fields
    # stay null: "not read" is a state the caller can act on, a stale number
    # dressed as a measurement is not.
    return _result(
        request,
        "executed",
        "".join(
            (
                "셀 병합 mutation_verified=true; selection_restore_verified=false. ",
                "병합 구조는 적용·검증되었지만 선택/커서 복원 readback이 ",
                f"{expectation.observable} 상태를 확인하지 못했습니다",
                "" if restore_error is None else f". restore_error={restore_error}",
                ""
                if after is not None
                else ". 작업 후 문서 상태를 읽지 못해 current_page·page_count·"
                "modified 를 비웠습니다",
            )
        ),
    ).model_copy(
        update={
            "changed": True,
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            # `verified` is the mutation verdict. `failed_step` and the factual
            # message carry the independent selection-restore verdict.
            "verified": True,
            "commands_executed": commands_executed,
            "commands_completed": commands_executed,
            "native_elapsed_microseconds": elapsed_microseconds,
            "current_page": None if after is None else after.current_page,
            "page_count": None if after is None else after.page_count,
            "modified": None if after is None else after.modified,
            "partial_change": False,
            "partial_mutation": False,
            "retry_safe": True,
            "reconcile_required": False,
            "failed_step": "selection_restore",
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
            "updated_addresses": prepared.updated_addresses,
        }
    )


def _table_reanchor_failure(
    request: NativeFormatRecipeRequest,
    after: NativeSnapshot | None,
    prepared: PreparedFormatOperation,
    commands_executed: int,
    elapsed_microseconds: int,
    error: _TableTargetReanchorError,
) -> OperationResult:
    """A size change that ran but could not be read back at its own address.

    ``partial_change`` rather than ``transport_error``: the transport was
    fine, the document did change, and the caller's next move is a re-read of
    the table, not a retry of the call. ``failed_step`` names the readback so
    the public envelope carries ``failure_stage="table_target_reanchor"``
    instead of the generic ``"transport"`` the old path produced.
    """
    return _result(
        request,
        "partial_change",
        f"{error.reason}; 요청 주소={', '.join(error.addresses)}",
    ).model_copy(
        update={
            "changed": True,
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            "verified": False,
            "commands_executed": commands_executed,
            "commands_completed": commands_executed,
            "native_elapsed_microseconds": elapsed_microseconds,
            "current_page": None if after is None else after.current_page,
            "page_count": None if after is None else after.page_count,
            "modified": None if after is None else after.modified,
            "partial_change": True,
            # The commands completed; what is unknown is the result, not
            # whether half of them ran.
            "partial_mutation": False,
            # Repeating a size change that may already have landed is not free.
            "retry_safe": False,
            "failed_step": "table_target_reanchor",
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
            "updated_addresses": prepared.updated_addresses,
        }
    )


def _merge_exception_failure(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    prepared: PreparedFormatOperation,
    primary: HwpLiveError,
    restore_error: HwpLiveError | None,
    commands_executed: int,
) -> OperationResult:
    native = primary if isinstance(primary, NativeActionFailure) else None
    partial_mutation = True if native is None else native.partial_mutation
    detail = f"primary_error={primary}"
    if restore_error is not None:
        detail = f"{detail}; restore_error={restore_error}"
    return _result(
        request,
        "partial_change" if partial_mutation else "operation_failed",
        detail,
    ).model_copy(
        update={
            "changed": partial_mutation,
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            "verified": False,
            "commands_executed": commands_executed,
            "commands_completed": commands_executed,
            "current_page": before.current_page,
            "page_count": before.page_count,
            "modified": True if partial_mutation else before.modified,
            "partial_change": partial_mutation,
            "partial_mutation": partial_mutation,
            "retry_safe": False,
            "reconcile_required": partial_mutation or restore_error is not None,
            "failed_step": (
                native.failed_step
                if native is not None and native.failed_step
                else "native_merge"
            ),
            "structure_digest_before": (
                None if native is None else native.structure_digest_before
            ),
            "structure_digest_after": (
                None if native is None else native.structure_digest_after
            ),
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
            "updated_addresses": (),
        }
    )


def _attempt_merge_restore(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    expectation: MergeSelectionExpectation,
) -> tuple[int, int, HwpLiveError | None, NativeSnapshot | None]:
    commands = merge_selection_restore_commands(expectation, before)
    if not commands:
        return (
            0,
            0,
            HwpLiveError(
                f"복원 명령을 만들 수 없는 상태입니다: {expectation.observable}"
            ),
            None,
        )
    try:
        restored = execute_native_actions(
            request.candidate.window_handle,
            NativeActionRequest(
                request.routing_page.document_id,
                request.routing_page.full_name,
                commands,
            ),
            minimum_version=9,
        )
        if restored is None:
            return (
                0,
                0,
                HwpLiveError("선택/커서 복원 네이티브 실행기를 사용할 수 없습니다"),
                None,
            )
        snapshot = read_native_snapshot(request.candidate.window_handle)
        if snapshot is None or not selection_restore_matches(
            expectation, before, snapshot
        ):
            return (
                restored.commands_executed,
                restored.elapsed_microseconds,
                HwpLiveError(f"복원 readback 불일치: {expectation.observable}"),
                snapshot,
            )
        return (
            restored.commands_executed,
            restored.elapsed_microseconds,
            None,
            snapshot,
        )
    except HwpLiveError as error:
        return 0, 0, error, None


def _execute_prepared(
    request: NativeFormatRecipeRequest,
    before: NativeSnapshot,
    prepared: PreparedFormatOperation,
    before_detail: NativeDetailedInspection | None = None,
) -> OperationResult:
    plan = prepared.plan
    selection_commands = 0
    selection_elapsed = 0
    if isinstance(plan, TextFormatCommandPlan) and plan.whole_body:
        selected = execute_native_actions(
            request.candidate.window_handle,
            NativeActionRequest(
                request.routing_page.document_id,
                request.routing_page.full_name,
                (MovePositionCommand(NativePosition(0, 0, 0)), RunCommand("SelectAll")),
            ),
            minimum_version=9,
        )
        if selected is None:
            raise HwpLiveError("문서 전체 텍스트 선택을 실행하지 못했습니다")
        whole_before = read_native_snapshot(request.candidate.window_handle)
        if (
            whole_before is None
            or not whole_before.selection.selected
            # SelectAll may start after section/header control codes in the
            # first body paragraph. Those codes are not editable text.
            or whole_before.selection.start.list_id != 0
            or whole_before.selection.start.paragraph != 0
            or whole_before.selection.end.list_id != 0
            or whole_before.document_id != before.document_id
            or whole_before.full_name != before.full_name
        ):
            raise HwpLiveError("문서 전체 텍스트 선택을 확인하지 못했습니다")
        before = whole_before
        selection_commands = selected.commands_executed
        selection_elapsed = selected.elapsed_microseconds
    if (
        isinstance(plan, TableFormatCommandPlan)
        and plan.formatting.format_source_table_id is not None
    ):
        return execute_table_format_copy(
            request, before, prepared, _result(request, "executed", "")
        )
    if (
        isinstance(plan, TableFormatCommandPlan)
        and plan.formatting.protect_size is not None
    ):
        return execute_table_size_protection(
            request, before, prepared, _result(request, "executed", "")
        )
    detail_page: int | None = None
    match plan:  # noqa: E501  # noqa: MATCH_OK — closed union is fully enumerated
        case TableFormatCommandPlan():
            formatted = plan.formatting
            needs_axis_topology = (
                formatted.column_width_mm is not None
                or formatted.row_height_mm is not None
            )
            needs_cell_format_topology = len(plan.cells) > 1 and bool(
                cell_format_commands(formatted.formatting)
            )
            detail_page = (
                plan.table.page
                if not plan.cells or needs_axis_topology or needs_cell_format_topology
                else None
            )
        case MergeCommandPlan() | SplitCommandPlan():
            detail_page = plan.table.page
        case TextFormatCommandPlan():
            pass
    selection_cancelled = False
    if (
        before_detail is None
        and detail_page is not None
        and isinstance(plan, MergeCommandPlan)
        and (before.selection.mode & 0x0F) == 3
        and (before.selection.cell_addresses or before.cell_address)
    ):
        cancelled = execute_native_actions(
            request.candidate.window_handle,
            NativeActionRequest(
                request.routing_page.document_id,
                request.routing_page.full_name,
                (RunCommand("Cancel"),),
            ),
            minimum_version=9,
        )
        if cancelled is None:
            raise HwpLiveError(
                "선택 상태를 보존하기 위한 병합 preflight를 시작하지 못했습니다"
            )
        selection_cancelled = True
    if before_detail is None and detail_page is not None:
        before_detail = inspect_native_structure(
            request.candidate.window_handle,
            detail_page,
        )
    if detail_page is not None and before_detail is None:
        raise HwpLiveError("표 구조 변경 전 대상 표의 실제 셀 구조를 읽지 못했습니다")
    if isinstance(plan, MergeCommandPlan) and plan.merge.start == plan.merge.end:
        assert before_detail is not None
        topology = table_topology(before_detail, plan.table.instance_id)
        address = plan.merge.start.upper()
        if address not in topology.by_address:
            return _pre_mutation_failure(
                request,
                before,
                prepared,
                InputFailure("schema_conflict", "병합 no-op 셀이 실제 표에 없습니다"),
            )
        if selection_cancelled:
            noop_expectation = MergeSelectionExpectation(
                "merged_owner", address, plan.table.instance_id
            )
            _, _, noop_restore_error, after_noop = _attempt_merge_restore(
                request, before, noop_expectation
            )
        else:
            noop_restore_error = None
            after_noop = read_native_snapshot(request.candidate.window_handle)
        if (
            noop_restore_error is not None
            or after_noop is None
            or (not selection_cancelled and after_noop != before)
        ):
            return _result(
                request,
                "operation_failed",
                "단일 셀 병합 no-op 전후 선택/커서 상태가 일치하지 않습니다",
            ).model_copy(
                update={
                    "verified": False,
                    "commands_executed": 0,
                    "commands_completed": 0,
                    "partial_mutation": False,
                    "retry_safe": True,
                    "reconcile_required": False,
                    "failed_step": "selection_restore",
                }
            )
        return _result(
            request,
            "executed",
            "실제 표에서 단일 셀을 확인했고 구조와 선택/커서를 바꾸지 않았습니다",
        ).model_copy(
            update={
                "execution_mode": "native_in_process",
                "native_protocol": 9,
                "verification": "native_operation_specific_readback",
                "verified": True,
                "commands_executed": 0,
                "commands_completed": 0,
                "current_page": before.current_page,
                "page_count": before.page_count,
                "modified": before.modified,
                "partial_mutation": False,
                "retry_safe": True,
                "resolved_target_id": prepared.target_id,
                "target_resolution_basis": prepared.target_basis,
                "updated_addresses": (address,),
            }
        )
    if isinstance(plan, TableFormatCommandPlan) and not plan.cells:
        assert before_detail is not None
        selected = _resolve_selected_table_cells(
            prepared,
            before,
            before_detail,
            request.candidate.application,
            explicit_target=has_explicit_table_locator(request),
        )
        if isinstance(selected, InputFailure):
            return _pre_mutation_failure(request, before, prepared, selected)
        prepared = selected
        plan = prepared.plan
    if (
        isinstance(plan, TableFormatCommandPlan)
        and before_detail is not None
        and (
            plan.formatting.column_width_mm is not None
            or plan.formatting.row_height_mm is not None
        )
    ):
        try:
            prepared = _with_topology_size_targets(prepared, before_detail)
        except HwpLiveError as error:
            return _pre_mutation_failure(
                request,
                before,
                prepared,
                InputFailure("schema_conflict", str(error)),
            )
        plan = prepared.plan
    if (
        isinstance(plan, TableFormatCommandPlan)
        and before_detail is not None
        and len(plan.cells) > 1
        and bool(cell_format_commands(plan.formatting.formatting))
    ):
        prepared = _with_topology_cell_geometry_targets(prepared, before_detail)
        plan = prepared.plan
    if isinstance(plan, (MergeCommandPlan, SplitCommandPlan)):
        assert before_detail is not None
        if isinstance(plan, MergeCommandPlan):
            normalized = _normalized_merge_plan(prepared, before_detail)
            if isinstance(normalized, InputFailure):
                return _pre_mutation_failure(request, before, prepared, normalized)
            prepared = normalized
            plan = prepared.plan
        preflight = topology_preflight(prepared, before_detail)
        if preflight is not None:
            return _pre_mutation_failure(request, before, prepared, preflight)
    merge_restore: MergeSelectionExpectation | None = None
    if isinstance(plan, MergeCommandPlan):
        assert before_detail is not None
        region = table_topology(before_detail, plan.table.instance_id).merge_region(
            plan.merge.start, plan.merge.end
        )
        merge_restore = merge_selection_expectation(
            before,
            before_detail,
            plan.table.instance_id,
            region,
        )
    if isinstance(plan, TableFormatCommandPlan):
        executed = _execute_table_format_batches(
            request,
            before,
            prepared,
        )
        if isinstance(executed, OperationResult):
            return executed
        commands_executed, elapsed_microseconds, native_call_count = executed
    else:
        commands = build_native_format_commands(prepared.plan)
        try:
            native = execute_native_actions(
                request.candidate.window_handle,
                NativeActionRequest(
                    request.routing_page.document_id,
                    request.routing_page.full_name,
                    commands,
                ),
                minimum_version=9,
            )
            if native is None:
                raise HwpLiveError(
                    "한컴 프로토콜 9 네이티브 서식 recipe를 사용할 수 없습니다"
                )
        except HwpLiveError as primary:
            if merge_restore is None:
                raise
            restored_commands, _, restore_error, _ = _attempt_merge_restore(
                request, before, merge_restore
            )
            completed = (
                primary.commands_completed
                if isinstance(primary, NativeActionFailure)
                else 0
            )
            return _merge_exception_failure(
                request,
                before,
                prepared,
                primary,
                restore_error,
                completed + restored_commands,
            )
        commands_executed = native.commands_executed
        elapsed_microseconds = native.elapsed_microseconds
        native_call_count = 1
    commands_executed += selection_commands
    elapsed_microseconds += selection_elapsed
    native_call_count += int(selection_commands > 0)
    after = read_native_snapshot(request.candidate.window_handle)
    if after is None:
        raise HwpLiveError("네이티브 서식 작업 후 문서 상태를 읽지 못했습니다")
    if (
        request.postconditions.preserve_page_count
        and after.page_count != before.page_count
    ):
        raise HwpLiveError("서식 작업 후 페이지 수 보존 완료조건을 만족하지 못했습니다")
    if isinstance(prepared.plan, TextFormatCommandPlan):
        verify_text_format(prepared.plan.formatting, before, after)
    verification_page = (
        prepared.plan.table.page
        if isinstance(
            prepared.plan,
            (MergeCommandPlan, SplitCommandPlan, TableFormatCommandPlan),
        )
        else after.current_page
    )
    try:
        structure_note = _verify_structural_plan(
            prepared,
            request.candidate.window_handle,
            before_detail,
            verification_page,
        )
    except _TableTargetReanchorError as reanchor_error:
        # The commands ran and the readback is what failed. Saying so as a
        # partial change with its own failed_step keeps the caller from
        # reading a transport fault into a size change that landed.
        return _table_reanchor_failure(
            request,
            after,
            prepared,
            commands_executed,
            elapsed_microseconds,
            reanchor_error,
        )
    if isinstance(prepared.plan, TableFormatCommandPlan):
        # A cell format changes no grid, so the pre-edit CellTopology is still
        # the topology of the table being verified. It is what lets the
        # verification group cells into blocks instead of one round trip each.
        _verify_table_text_color(request, prepared.plan, before_detail)
    if merge_restore is not None and not selection_restore_matches(
        merge_restore, before, after
    ):
        (
            restored_commands,
            restored_elapsed,
            restore_error,
            restored_snapshot,
        ) = _attempt_merge_restore(request, before, merge_restore)
        commands_executed += restored_commands
        elapsed_microseconds += restored_elapsed
        # The restore attempt's own readback is newer than the post-merge one;
        # when it produced nothing, the post-merge snapshot is still a real
        # measurement of this document after the mutation.
        latest = after if restored_snapshot is None else restored_snapshot
        if restore_error is not None:
            return _merge_restore_failure(
                request,
                latest,
                prepared,
                commands_executed,
                elapsed_microseconds,
                merge_restore,
                restore_error,
            )
        after = restored_snapshot
        if after is None or not selection_restore_matches(merge_restore, before, after):
            return _merge_restore_failure(
                request,
                latest,
                prepared,
                commands_executed,
                elapsed_microseconds,
                merge_restore,
            )
    # verified=true 는 그대로 둔다. 검증은 실제로 통과했다. 다만 그 성공이
    # 표의 격자를 바꿔 놓았다면 그 사실까지 같이 말해야 다음 호출이 옛 주소로
    # 엉뚱한 셀을 치지 않는다.
    message = (
        "프로토콜 9 C++/ATL 네이티브 서식 recipe를 "
        f"{native_call_count}회 제한 호출로 실행하고 검증했습니다"
    )
    if structure_note is not None:
        message = f"{message}. {structure_note}"
    return _result(
        request,
        "executed",
        message,
    ).model_copy(
        update={
            "execution_mode": "native_in_process",
            "native_protocol": 9,
            "verification": "native_operation_specific_readback",
            "verified": True,
            "commands_executed": commands_executed,
            "commands_completed": commands_executed,
            "native_elapsed_microseconds": elapsed_microseconds,
            "current_page": after.current_page,
            "page_count": after.page_count,
            "modified": after.modified,
            "partial_mutation": False,
            "retry_safe": True,
            "resolved_target_id": prepared.target_id,
            "target_resolution_basis": prepared.target_basis,
            "updated_addresses": prepared.updated_addresses,
        }
    )


def operate_native_format_recipe(
    request: NativeFormatRecipeRequest,
) -> OperationResult | None:
    workflow = request.resolution.workflow_id
    if not is_native_format_workflow(workflow):
        return None
    if request.resolve_only:
        return _result(
            request,
            "resolved",
            "인증된 프로토콜 9 네이티브 서식 recipe를 확정했습니다",
        )
    if not request.allow_document_change:
        return _result(
            request,
            "confirmation_required",
            "문서 서식이나 표 구조를 변경하는 작업입니다",
        )
    before = read_native_snapshot(request.candidate.window_handle)
    if before is None:
        raise HwpLiveError("네이티브 서식 작업 전 문서 상태를 읽지 못했습니다")
    selected = _request_with_selected_cells(workflow, request, before)
    if isinstance(selected, InputFailure):
        return _failure_result(request, selected)
    before_detail: NativeDetailedInspection | None = None
    if isinstance(selected, _SelectionResolvedRequest):
        request = selected.request
        before_detail = selected.before_detail
    else:
        request = selected
    prepared = prepare_native_format_operation(workflow, request, before)
    if isinstance(prepared, (InputFailure, TargetFailure)):
        return _failure_result(request, prepared)
    return _execute_prepared(request, before, prepared, before_detail)

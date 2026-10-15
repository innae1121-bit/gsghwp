from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from hwp_document_style_observation import resolve_document_style_usage
from hwp_document_style_profile import document_body_style_id
from hwp_errors import HwpLiveError
from hwp_live_api import LiveHwpApplication, SelectionRange
from hwp_live_contract import (
    LayoutPlan,
    LayoutResult,
    MutationResult,
    SelectionPosition,
)
from hwp_live_inspection import inspect_styles
from hwp_live_layout import resolve_layout_styles, validate_layout_anchor
from hwp_live_native_action_contract import (
    NativeActionFailure,
    NativeActionFailureEvidence,
)
from hwp_live_native_action_models import (
    NativeActionRequest,
    NativeActionResult,
    NativePosition,
    NativeSelection,
    NativeSnapshot,
    ReplaceSelectionCommand,
    TextPatchCommand,
)
from hwp_live_native_batch import execute_native_actions, read_native_snapshot
from hwp_live_native_format_commands import (
    TextFormatCommandPlan,
    build_native_format_commands,
)
from hwp_live_native_layout import (
    NativeLayoutContext,
    build_native_layout_execution_plan,
    build_native_layout_request,
)
from hwp_live_rot import HwpDocumentCandidate
from hwp_live_safety import require_writable_document, run_layout_mutation
from hwp_live_text_format_verification import verify_requested_text_format
from hwp_live_text_patch_contract import (
    TextPatchRequest,
    TextPatchResult,
    matches_replacement_readback,
    text_patch_minimum_protocol,
    validate_text_patch_request,
)
from hwp_operation_local_precondition import native_failure_left_document_untouched


@dataclass(slots=True)
class _NativeLayoutBox:
    result: NativeActionResult | None = None
    snapshot: NativeSnapshot | None = None


@dataclass(slots=True)
class _NativeMutationBox:
    result: NativeActionResult | None = None
    snapshot: NativeSnapshot | None = None


class _LegacyLayoutBatchFailure(NativeActionFailure):
    completed_batches: int
    completed_addresses: tuple[str, ...]

    def __init__(
        self,
        evidence: NativeActionFailureEvidence,
        *,
        completed_batches: int,
        completed_addresses: tuple[str, ...],
    ) -> None:
        super().__init__(evidence)
        self.completed_batches = completed_batches
        self.completed_addresses = completed_addresses


def _merge_layout_results(
    first: NativeActionResult,
    second: NativeActionResult,
) -> NativeActionResult:
    return replace(
        first,
        commands_executed=first.commands_executed + second.commands_executed,
        actions_executed=first.actions_executed + second.actions_executed,
        text_insertions=first.text_insertions + second.text_insertions,
        image_insertions=first.image_insertions + second.image_insertions,
        elapsed_microseconds=(first.elapsed_microseconds + second.elapsed_microseconds),
        created_control_ids=(first.created_control_ids + second.created_control_ids),
        call_results=first.call_results + second.call_results,
        image_timing_count=(first.image_timing_count + second.image_timing_count),
        image_max_microseconds=max(
            first.image_max_microseconds,
            second.image_max_microseconds,
        ),
        image_total_microseconds=(
            first.image_total_microseconds + second.image_total_microseconds
        ),
    )


def _legacy_layout_failure(
    failure: NativeActionFailure,
    *,
    batch_index: int,
    commands_completed_before: int,
    command_limit: int,
    completed_addresses: tuple[str, ...],
) -> _LegacyLayoutBatchFailure:
    local_completed = min(max(failure.commands_completed, 0), command_limit)
    commands_completed = commands_completed_before + local_completed
    partial_mutation = batch_index > 0 or failure.partial_mutation
    return _LegacyLayoutBatchFailure(
        NativeActionFailureEvidence(
            code=failure.code,
            location=failure.location,
            message=(
                f"{failure.message}; 완료 batch {batch_index}개, "
                f"완료 셀 {len(completed_addresses)}개"
            ),
            commands_completed=commands_completed,
            failed_step=failure.failed_step,
            partial_mutation=partial_mutation,
            retry_safe=failure.retry_safe and not partial_mutation,
            structure_digest_before=failure.structure_digest_before,
            structure_digest_after=failure.structure_digest_after,
        ),
        completed_batches=batch_index,
        completed_addresses=completed_addresses,
    )


def _native_selection(selection: SelectionRange) -> NativeSelection:
    selected, slist, spara, spos, elist, epara, epos = selection

    def coordinate(value: int | None) -> int:
        return 0 if value is None else value

    return NativeSelection(
        selected,
        NativePosition(coordinate(slist), coordinate(spara), coordinate(spos)),
        NativePosition(coordinate(elist), coordinate(epara), coordinate(epos)),
    )


def _selection_range(expected: SelectionPosition) -> SelectionRange:
    if not expected.selected:
        raise HwpLiveError("수정할 선택 영역의 좌표가 없습니다")
    return (
        True,
        expected.start_list,
        expected.start_paragraph,
        expected.start_character,
        expected.end_list,
        expected.end_paragraph,
        expected.end_character,
    )


def _normalize_paragraph_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


#: 진단에 실어 보낼 관측 원문의 최대 길이. 캡션 한 줄을 통째로 보여주고도 남는
#: 길이이고, 선택이 한 문단보다 클 때 실패 메시지가 본문 덤프가 되는 것을 막는다.
_OBSERVED_TEXT_LIMIT: Final = 200


def _observed_selection_text(window_handle: int) -> str:
    """실패가 멈춰 세운 그 자리에 지금 실제로 들어 있는 글자.

    읽을 수 없거나 선택이 남아 있지 않으면 "". 브리지가 편집 전 검사에서
    멈출 때 어떤 경로는 커서를 원위치시키고(`text.find.restore`) 어떤 경로는
    찾아낸 선택을 그대로 둔다. 선택이 남아 있는 경우 -- 기대 원문과 실제가
    달라 멈춘 바로 그 경우 -- 에만 읽을 것이 있다.

    조회일 뿐이라 문서를 바꾸지 않고, 같은 스냅샷 읽기를 이 파일의 성공
    경로가 패치 전후로 이미 두 번 한다.
    """
    try:
        snapshot = read_native_snapshot(window_handle)
    except (HwpLiveError, OSError, ValueError):
        return ""
    if snapshot is None or not snapshot.selection.selected:
        return ""
    return _normalize_paragraph_text(snapshot.selected_text)


def _failure_with_observed_text(
    failure: NativeActionFailure,
    observed: str,
) -> NativeActionFailure:
    """실패에 "그 자리에 지금 있는 원문"을 실어 다시 만든 같은 실패.

    브리지는 기대와 실제가 다르다는 것까지만 말하고 무엇이 다른지는 말하지
    않는다(ActionTextPatch.cpp 의 `PatchSelectedText`). 그래서 호출자는 왜
    어긋났는지 모른 채 같은 요청을 한 번 더 보냈고, 현장에서 그 재시도 한 번이
    다시 40초였다.

    실제로 읽힌 글자를 보여주면 그 판단이 호출자에게 넘어간다. 자동 그림번호
    필드가 든 캡션이라면 요청한 `(그림 5.5.3-○)` 자리에 `(그림 5.5.3-16)` 이
    보이고, 그 한 줄이 "여기는 리터럴이 아니라 필드다"를 말한다. 규칙으로
    맞히지 않고 관측을 그대로 낸다.
    """
    trimmed = observed[:_OBSERVED_TEXT_LIMIT]
    ellipsis = "…" if len(observed) > _OBSERVED_TEXT_LIMIT else ""
    return NativeActionFailure(
        NativeActionFailureEvidence(
            code=failure.code,
            location=failure.location,
            message=(
                f'{failure.message}; 그 자리에서 실제로 읽은 원문은 "{trimmed}{ellipsis}"'
                + " 입니다"
            ),
            commands_completed=failure.commands_completed,
            failed_step=failure.failed_step,
            partial_mutation=failure.partial_mutation,
            retry_safe=failure.retry_safe,
            structure_digest_before=failure.structure_digest_before,
            structure_digest_after=failure.structure_digest_after,
        )
    )


def replace_validated_selection(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    expected_selection: SelectionPosition,
    expected_text: str,
    replacement: str,
    unsafe_selectors: set[str],
    guard: Callable[[], None],
) -> MutationResult:
    _ = hwp
    if len(expected_text) > 1_000_000 or len(replacement) > 1_000_000:
        raise HwpLiveError("라이브 텍스트 수정 크기 한도를 초과했습니다")
    selection = _selection_range(expected_selection)
    if selection[1] != selection[4]:
        raise HwpLiveError("서로 다른 한컴 컨트롤을 가로지른 선택은 수정하지 않습니다")
    require_writable_document(unsafe_selectors, candidate.selector)
    guard()
    native = _NativeMutationBox()

    def mutate_native_selection() -> None:
        native.result = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(
                document_id=candidate.document_id,
                full_name=candidate.full_name,
                commands=(ReplaceSelectionCommand(expected_text, replacement),),
                expected_selection=_native_selection(selection),
            ),
            minimum_version=9,
        )
        if native.result is None:
            return
        native.snapshot = read_native_snapshot(candidate.window_handle)
        if native.snapshot is None:
            raise HwpLiveError("네이티브 선택 교체 후 한컴 문서 상태를 읽지 못했습니다")

    run_layout_mutation(
        unsafe_selectors,
        candidate.selector,
        mutate_native_selection,
    )
    guard()
    if native.result is None or native.snapshot is None:
        raise HwpLiveError("한컴 네이티브 선택 교체를 사용할 수 없습니다")
    return MutationResult(
        action="replace_selection",
        current_page=native.snapshot.current_page,
        modified=native.snapshot.modified,
    )


def patch_validated_text(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    request: TextPatchRequest,
    unsafe_selectors: set[str],
    guard: Callable[[], None],
) -> TextPatchResult:
    _ = hwp
    validate_text_patch_request(request)
    require_writable_document(unsafe_selectors, candidate.selector)
    guard()
    before = read_native_snapshot(candidate.window_handle)
    if before is None:
        raise HwpLiveError("text.patch 전 한컴 문서 상태를 읽지 못했습니다")
    target = request.target
    commands = (
        TextPatchCommand(
            target=target.kind,
            expected_text=request.expected_text,
            replacement=request.replacement,
            start=target.start,
            end=target.end,
            occurrence=target.occurrence,
            match_case=target.match_case,
            table_instance_id=target.table_instance_id,
            cell_address=target.cell_address,
        ),
        *(
            ()
            if request.formatting is None
            else build_native_format_commands(TextFormatCommandPlan(request.formatting))
        ),
    )
    minimum_protocol = text_patch_minimum_protocol(request)
    try:
        native_result = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(
                document_id=candidate.document_id,
                full_name=candidate.full_name,
                commands=commands,
                expected_cursor=before.cursor,
                expected_selection=before.selection,
            ),
            minimum_version=minimum_protocol,
        )
    except NativeActionFailure as failure:
        # 편집 전 검사에서 멈춘 실패에만 읽을 것이 있다. 부분 변경이 있었을 수도
        # 있는 실패에서 지금 선택된 글자는 "어긋난 원문"이 아니라 반쯤 쓰인
        # 결과일 수 있고, 그것을 원문이라고 부르면 거짓말이 된다.
        if not native_failure_left_document_untouched(failure):
            raise
        observed = _observed_selection_text(candidate.window_handle)
        if not observed:
            raise
        raise _failure_with_observed_text(failure, observed) from failure
    if native_result is None:
        raise HwpLiveError(
            f"프로토콜 {minimum_protocol} 네이티브 text.patch를 사용할 수 없습니다"
        )
    after = read_native_snapshot(candidate.window_handle)
    if after is None:
        raise HwpLiveError("text.patch 후 한컴 문서 상태를 읽지 못했습니다")
    if request.replacement:
        if not after.selection.selected or not matches_replacement_readback(
            after.selected_text,
            request.replacement,
            target.kind,
        ):
            raise HwpLiveError(
                "text.patch 후 변경한 본문 범위를 다시 읽어 확인하지 못했습니다"
            )
    elif after.selection.selected:
        raise HwpLiveError("text.patch 삭제 후 선택 영역이 예상대로 접히지 않았습니다")
    if request.formatting is not None:
        verify_requested_text_format(request.formatting, after)
    if request.post_selection != "keep":
        if after.selection.selected:
            endpoint = (
                after.selection.start
                if request.post_selection == "collapse_to_start"
                else after.selection.end
            )
        else:
            # 삭제(빈 replacement)는 네이티브가 이미 삭제 시작점으로 커서를
            # 접었고 선택이 없다. 이때 after.selection의 끝점 좌표는 선택이
            # 아니라 잔상이므로 그 좌표로 접기를 검증하면 성공한 편집이
            # 실패로 뒤집힌다. 접을 곳의 유일한 진실은 현재 커서다.
            endpoint = after.cursor
        guard()
        collapsed = hwp.set_pos(
            endpoint.list_id,
            endpoint.paragraph,
            endpoint.character,
        )
        guard()
        if (
            not collapsed
            or hwp.get_pos()
            != (endpoint.list_id, endpoint.paragraph, endpoint.character)
            or hwp.get_selected_pos()[0]
        ):
            raise HwpLiveError(
                "text.patch 검증 후 선택 영역을 요청한 위치로 접지 못했습니다"
            )
    guard()
    return TextPatchResult(native_result, before, after)


def apply_validated_layout(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    plan: LayoutPlan,
    assets: dict[Path, Path],
    unsafe_selectors: set[str],
    expected_cursor: tuple[int, int, int],
    expected_selected_text: str | None,
    guard: Callable[[], None],
) -> LayoutResult:
    require_writable_document(unsafe_selectors, candidate.selector)
    guard()
    cursor = hwp.get_pos()
    guard()
    if cursor != expected_cursor:
        raise HwpLiveError("현재 커서가 에이전트가 확인한 위치와 다릅니다")
    selected = hwp.get_selected_pos()
    guard()
    if plan.replace_selection:
        raise HwpLiveError(
            "선택 영역을 복합 레이아웃으로 교체하는 기능은 원문 삭제 위험 때문에 사용하지 않습니다"
        )
    if selected[0]:
        raise HwpLiveError("선택 영역이 있어 레이아웃 삽입을 중단했습니다")
    if expected_selected_text is not None:
        raise HwpLiveError("선택 영역이 없으므로 예상 선택 텍스트를 보내지 마세요")
    resolved_plan, page_geometry = resolve_layout_styles(
        hwp,
        plan,
        guard,
        # Same observed [shape -> style id] table the recipe path uses, so the
        # bulk path does not fall back to name matching for "○"/"(1)".
        usage=resolve_document_style_usage(
            candidate.window_handle,
            candidate.document_id,
            candidate.full_name,
        ),
        candidate=candidate,
    )
    if resolved_plan.target == "current":
        validate_layout_anchor(hwp, resolved_plan, guard)
    # The caret style is what the recipe path hands the native layout compiler
    # (hwp_live_operation_recipe.py:415), so the bulk path reads it the same way
    # instead of shipping the 0 default. The native snapshot is a C++ bridge
    # call, not a COM round trip. Only when it is unavailable do we pay for a
    # style list to pick the document's own body style — the edit is never
    # refused over a base style we could not name.
    before = read_native_snapshot(candidate.window_handle)
    guard()
    # 스타일 목록 실패를 여기서 삼키지 않는다. 바로 위 resolve_layout_styles 가
    # 이미 같은 inspect_styles 를 예외 그대로 통과시키므로, 스타일을 못 읽는
    # 문서는 이 지점에 닿기 전에 실패한다. 여기서만 0 으로 되돌리면 그 문턱을
    # 넘어온 일시적 실패가 문서 본문 스타일 대신 조용히 기본 스타일 id 를 싣고
    # 나가고, 호출자는 위계가 무너진 것을 응답에서 볼 수 없다. LayoutResult 에는
    # 그 사실을 실을 자리가 없으므로 1.3.1(3aa71a1)처럼 소리내어 실패한다.
    base_style_id = (
        document_body_style_id(inspect_styles(hwp, guard).styles)
        if before is None
        else before.style_id
    )
    position = NativePosition(*cursor)
    native_request = build_native_layout_request(
        NativeLayoutContext(
            document_id=candidate.document_id,
            full_name=candidate.full_name,
            # resolve_layout_styles already bound every block to a concrete
            # document style id, so the name lookup table is dead weight here —
            # the recipe path passes () for the same reason
            # (hwp_live_operation_recipe.py:411).
            style_ids=(),
            page_geometry=page_geometry,
            base_style_id=base_style_id,
            expected_cursor=position,
            expected_selection=_native_selection(selected),
        ),
        resolved_plan,
        assets,
    )
    execution = build_native_layout_execution_plan(native_request)
    native = _NativeLayoutBox()

    def mutate_native_layout() -> None:
        commands_completed = 0
        completed_group_keys: set[int] = set()
        for batch_index, batch in enumerate(execution.batches):
            try:
                batch_result = execute_native_actions(
                    candidate.window_handle,
                    batch.request,
                    minimum_version=4,
                )
            except NativeActionFailure as failure:
                if len(execution.batches) == 1:
                    raise
                local_completed = min(
                    max(failure.commands_completed, 0),
                    len(batch.request.commands),
                )
                completed_group_keys.update(batch.completed_group_keys(local_completed))
                raise _legacy_layout_failure(
                    failure,
                    batch_index=batch_index,
                    commands_completed_before=commands_completed,
                    command_limit=len(batch.request.commands),
                    completed_addresses=execution.completed_addresses(
                        completed_group_keys
                    ),
                ) from failure
            except HwpLiveError as error:
                if len(execution.batches) == 1 or batch_index == 0:
                    raise
                completed_addresses = execution.completed_addresses(
                    completed_group_keys
                )
                raise _LegacyLayoutBatchFailure(
                    NativeActionFailureEvidence(
                        code="LAYOUT_BATCH_TRANSPORT",
                        location=f"{batch_index + 1}/{len(execution.batches)}",
                        message=(
                            f"{error}; 완료 batch {batch_index}개, "
                            f"완료 셀 {len(completed_addresses)}개"
                        ),
                        commands_completed=commands_completed,
                        failed_step=f"layout_batch:{batch_index + 1}",
                        partial_mutation=True,
                        retry_safe=False,
                        structure_digest_before=None,
                        structure_digest_after=None,
                    ),
                    completed_batches=batch_index,
                    completed_addresses=completed_addresses,
                ) from error
            if batch_result is None:
                if len(execution.batches) == 1:
                    return
                partial_mutation = batch_index > 0
                completed_addresses = execution.completed_addresses(
                    completed_group_keys
                )
                raise _LegacyLayoutBatchFailure(
                    NativeActionFailureEvidence(
                        code="LAYOUT_BATCH_UNAVAILABLE",
                        location=f"{batch_index + 1}/{len(execution.batches)}",
                        message=(
                            "한컴 네이티브 레이아웃 배치를 사용할 수 없습니다; "
                            f"완료 batch {batch_index}개, "
                            f"완료 셀 {len(completed_addresses)}개"
                        ),
                        commands_completed=commands_completed,
                        failed_step=f"layout_batch:{batch_index + 1}",
                        partial_mutation=partial_mutation,
                        retry_safe=not partial_mutation,
                        structure_digest_before=None,
                        structure_digest_after=None,
                    ),
                    completed_batches=batch_index,
                    completed_addresses=completed_addresses,
                )
            if batch_result.commands_executed != len(batch.request.commands):
                local_completed = min(
                    max(batch_result.commands_executed, 0),
                    len(batch.request.commands),
                )
                completed_group_keys.update(batch.completed_group_keys(local_completed))
                completed_addresses = execution.completed_addresses(
                    completed_group_keys
                )
                partial_mutation = batch_index > 0 or local_completed > 0
                raise _LegacyLayoutBatchFailure(
                    NativeActionFailureEvidence(
                        code="LAYOUT_BATCH_INCOMPLETE",
                        location=f"{batch_index + 1}/{len(execution.batches)}",
                        message=(
                            "레이아웃 batch 완료 명령 수가 요청과 다릅니다; "
                            f"완료 batch {batch_index}개, "
                            f"완료 셀 {len(completed_addresses)}개"
                        ),
                        commands_completed=commands_completed + local_completed,
                        failed_step=f"layout_batch:{batch_index + 1}",
                        partial_mutation=partial_mutation,
                        retry_safe=not partial_mutation,
                        structure_digest_before=None,
                        structure_digest_after=None,
                    ),
                    completed_batches=batch_index,
                    completed_addresses=completed_addresses,
                )
            native.result = (
                batch_result
                if native.result is None
                else _merge_layout_results(native.result, batch_result)
            )
            commands_completed += batch_result.commands_executed
            completed_group_keys.update(item.group.key for item in batch.groups)
        native.snapshot = read_native_snapshot(candidate.window_handle)
        if native.snapshot is None:
            raise HwpLiveError(
                "네이티브 레이아웃 배치 후 한컴 문서 상태를 읽지 못했습니다"
            )

    run_layout_mutation(
        unsafe_selectors,
        candidate.selector,
        mutate_native_layout,
    )
    if native.result is None or native.snapshot is None:
        raise HwpLiveError("한컴 네이티브 레이아웃 배치를 사용할 수 없습니다")
    return LayoutResult(
        blocks_applied=len(plan.blocks),
        current_page=native.snapshot.current_page,
        modified=native.snapshot.modified,
        created_control_ids=native.result.created_control_ids,
        native_elapsed_microseconds=native.result.elapsed_microseconds,
    )

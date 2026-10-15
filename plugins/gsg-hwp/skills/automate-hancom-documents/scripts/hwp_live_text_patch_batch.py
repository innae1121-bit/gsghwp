from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from time import perf_counter_ns
from typing import Final, Literal

from hwp_errors import HwpLiveError
from hwp_live_api import LiveHwpApplication
from hwp_live_native_action_models import (
    NativeActionCommand,
    NativeActionRequest,
    NativeSnapshot,
    PreparedTextPatchTarget,
    TextPatchCommand,
)
from hwp_live_native_format_commands import (
    TextFormatCommandPlan,
    build_native_format_commands,
)
from hwp_live_native_action_contract import (
    NativeActionFailure,
    action_request_payload_length,
    encode_action_request,
)
from hwp_live_native_batch import (
    content_revision_is_complete,
    execute_native_actions,
    forget_cached_content_signatures,
    preflight_native_text_patches,
    read_native_content_revision,
    read_native_snapshot,
)
from hwp_live_rot import HwpDocumentCandidate
from hwp_live_safety import require_writable_document
from hwp_live_text_patch_prepared import (
    canonical_prepared_text_patch_requests,
    effective_prepared_text_patch_requests,
)
from hwp_live_text_format_verification import verify_requested_text_format
from hwp_live_text_patch_contract import (
    TextPatchPhaseTiming,
    TextPatchPlanGuard,
    TextPatchRequest,
    TextPatchResult,
    TextPatchTargetKind,
    matches_replacement_readback,
    text_patch_minimum_protocol,
)


TEXT_PATCH_BATCH_MAX_ITEMS: Final = 1_000
TEXT_PATCH_BATCH_COMMAND_LIMIT: Final = 20_000
# The native wire ceiling is 8,000,000 ASCII bytes. Keeping one megabyte of
# headroom covers the later document-path and expected-selection envelope while
# this pre-check still accounts exactly for UTF-8 -> base64 command expansion.
TEXT_PATCH_BATCH_SAFE_TRANSPORT_BYTES: Final = 7_000_000


class TextPatchBatchVerificationError(HwpLiveError):
    """네이티브 실행이 끝난 뒤의 판독·검증 실패.

    실행 자체는 성공했고 그 결과가 몇 개의 명령을 실행했는지 말해 준다. 그 수를
    실패에 실어 보내는 이유는 하나다: 준비된 역패치 롤백이 "몇 개까지 되돌려야
    하는가"를 추측 대신 관측으로 정하게 하기 위해서다
    (hwp_live_text_patch_batch_history._completed_forward_commands).
    """

    native_commands_completed: int

    def __init__(self, error: HwpLiveError, native_commands_completed: int) -> None:
        super().__init__(
            error.reason,
            mutation_started=error.mutation_started,
            safe_to_repeat=error.safe_to_repeat,
            phase_timings=error.phase_timings,
        )
        self.native_commands_completed = native_commands_completed


@dataclass(frozen=True, slots=True)
class TextPatchBatchPlan:
    commands: tuple[NativeActionCommand, ...]
    minimum_protocol: Literal[11, 12]
    transport_bytes: int


@dataclass(frozen=True, slots=True)
class TextPatchPreparedReceipt:
    content_revision: str
    request_sha256: str
    target_count: int
    native_elapsed_microseconds: int = 0
    targets: tuple[PreparedTextPatchTarget, ...] = ()


def _prepared_request(
    candidate: HwpDocumentCandidate,
    plan: TextPatchBatchPlan,
) -> NativeActionRequest:
    return NativeActionRequest(
        candidate.document_id, candidate.full_name, plan.commands
    )


def _request_sha256(request: NativeActionRequest) -> str:
    return hashlib.sha256(encode_action_request(request).encode()).hexdigest()


def compile_text_patch_batch(
    requests: tuple[TextPatchRequest, ...],
    *,
    preflight: bool = False,
) -> TextPatchBatchPlan:
    """Compile and bound the complete native batch before checkpoint capture.

    ``preflight`` is the only way a PREPARE_TEXT sentinel record can be emitted.
    The native member refuses those records on the mutation path
    ("prepared text target records cannot execute as mutations"), so every
    execution caller must leave this False and the read-only preflight is the
    single caller that sets it.
    """
    commands: list[NativeActionCommand] = []
    minimum_protocol: Literal[11, 12] = 11
    for request in requests:
        target = request.target
        if request.literal_range is not None and not preflight:
            raise HwpLiveError(
                "inspected literal coordinates must be prepared before mutation",
                mutation_started=False,
                safe_to_repeat=True,
            )
        commands.append(
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
                preflight_only=(
                    preflight
                    and (
                        request.literal_range is not None
                        or (
                            request.expected_text is None
                            and target.kind in {"range", "table_cell"}
                        )
                    )
                ),
                literal_range=request.literal_range,
            )
        )
        if request.formatting is not None:
            commands.extend(
                build_native_format_commands(TextFormatCommandPlan(request.formatting))
            )
        minimum_protocol = max(minimum_protocol, text_patch_minimum_protocol(request))
    if len(commands) > TEXT_PATCH_BATCH_COMMAND_LIMIT:
        raise HwpLiveError(
            "text.patch batch가 네이티브 20,000 명령 한도를 초과했습니다"
        )
    if len(requests) > TEXT_PATCH_BATCH_MAX_ITEMS:
        raise HwpLiveError("text.patch batch 항목은 최대 1,000개입니다")
    native_commands = tuple(commands)
    transport_bytes = action_request_payload_length(
        NativeActionRequest(0, "", native_commands)
    )
    if transport_bytes > TEXT_PATCH_BATCH_SAFE_TRANSPORT_BYTES:
        raise HwpLiveError(
            "text.patch batch transport payload가 안전한 7,000,000 byte 한도를 초과했습니다"
        )
    return TextPatchBatchPlan(native_commands, minimum_protocol, transport_bytes)


def validate_text_patch_plan_guard(plan_guard: TextPatchPlanGuard) -> None:
    if (
        plan_guard.document_id <= 0
        or not plan_guard.full_name
        or not content_revision_is_complete(plan_guard.content_revision)
    ):
        raise HwpLiveError(
            "text.patch batch plan guard가 완전하거나 유효하지 않습니다",
            mutation_started=False,
            safe_to_repeat=True,
        )


def preflight_text_patch_batch(
    candidate: HwpDocumentCandidate,
    requests: tuple[TextPatchRequest, ...],
    plan_guard: TextPatchPlanGuard | None = None,
) -> TextPatchPreparedReceipt | None:
    """Resolve a complete batch read-only when the native member is available."""
    if plan_guard is not None:
        validate_text_patch_plan_guard(plan_guard)
        if (
            candidate.document_id != plan_guard.document_id
            or candidate.full_name.casefold() != plan_guard.full_name.casefold()
        ):
            raise HwpLiveError(
                "text.patch plan document identity가 현재 문서와 다릅니다",
                mutation_started=False,
                safe_to_repeat=True,
            )
    prepared_requests = canonical_prepared_text_patch_requests(requests)
    if prepared_requests is None:
        if plan_guard is not None:
            forget_cached_content_signatures(candidate.window_handle)
            current_revision = read_native_content_revision(candidate.window_handle)
            if current_revision != plan_guard.content_revision:
                raise HwpLiveError(
                    "text.patch plan content revision이 현재 문서와 다릅니다",
                    mutation_started=False,
                    safe_to_repeat=True,
                )
        # A semantic (find/current) member makes the whole batch unpreparable.
        # Any exact target that omitted expected_text then has no way to learn
        # its existing text, and returning None here would hand the batch to the
        # unprepared execution path, which the native member refuses. Name the
        # members on both sides of the conflict instead of failing at the wire.
        omitted = tuple(
            f"#{index} {request.target.kind}"
            for index, request in enumerate(requests)
            if request.expected_text is None
            and request.target.kind in {"range", "table_cell"}
        )
        if omitted:
            semantic = tuple(
                f"#{index} {request.target.kind}"
                for index, request in enumerate(requests)
                if request.target.kind in {"current", "find"}
            )
            raise HwpLiveError(
                "text.patch batch에 semantic 대상("
                + ", ".join(semantic)
                + ")이 섞여 있어 expected_text를 생략한 exact 대상("
                + ", ".join(omitted)
                + ")을 준비할 수 없습니다. find/current 패치는 별도 호출로 "
                "분리하거나, 모든 range/table_cell 패치에 expected_text를 "
                "제공하세요.",
                mutation_started=False,
                safe_to_repeat=True,
            )
        return None
    plan = compile_text_patch_batch(prepared_requests, preflight=True)
    request = _prepared_request(candidate, plan)
    prepared = preflight_native_text_patches(candidate.window_handle, request)
    if prepared is None:
        if plan_guard is not None:
            raise HwpLiveError(
                "guarded text.patch plan requires native prepared execution",
                mutation_started=False,
                safe_to_repeat=True,
            )
        return None
    if prepared.target_count != len(requests):
        raise HwpLiveError("text.patch preflight target 수가 요청과 다릅니다")
    targets = getattr(prepared, "targets", ())
    _ = effective_prepared_text_patch_requests(requests, targets)
    if (
        plan_guard is not None
        and prepared.content_revision != plan_guard.content_revision
    ):
        raise HwpLiveError(
            "text.patch plan content revision이 native preflight 전에 바뀌었습니다",
            mutation_started=False,
            safe_to_repeat=True,
        )
    return TextPatchPreparedReceipt(
        content_revision=prepared.content_revision,
        request_sha256=_request_sha256(request),
        target_count=prepared.target_count,
        native_elapsed_microseconds=prepared.elapsed_microseconds,
        targets=targets,
    )


def _validate_batch_verification_shape(
    execution_requests: tuple[TextPatchRequest, ...],
) -> None:
    """실행 전에, 이 배치가 실행 후 검증될 수 있는 모양인지 확인한다.

    배치가 끝난 뒤 한/글이 선택으로 들고 있는 것은 **마지막으로 실행된 대상**
    하나뿐이다. 그래서 파이썬 층이 왕복 한 번 없이 직접 확인할 수 있는 것도 그
    하나다. 나머지 대상의 서식은 "같은 요청 안에서 같은 명령이 실행됐다"는
    네이티브 보고로 확인한다 — 그러려면 실린 서식이 모두 같아야 한다.

    두 제약 모두 공개 경로가 이미 지키고 있다: ``hwp_patch_text_batch`` 는 배치
    전체에 서식 하나를 공유하고(항목별 서식은 스키마에 없다), ``hwp_patch_text``
    는 대상이 하나다. 지킬 수 없는 모양은 편집을 시작하기 전에 거절한다 —
    편집한 뒤에 "확인할 수 없다"고 말하면 되돌리기까지 끌고 가야 한다.
    """
    if not execution_requests:
        return
    final = execution_requests[-1]
    formatted = tuple(
        request for request in execution_requests if request.formatting is not None
    )
    if formatted and any(
        request.formatting != final.formatting for request in formatted
    ):
        raise HwpLiveError(
            "text.patch batch의 글자 서식은 모든 대상이 같아야 합니다"
            + "; 서식이 실제로 적용됐는지는 마지막 대상의 재판독으로 확인하므로,"
            + " 대상마다 다른 서식은 확인할 방법이 없습니다."
            + " 서식별로 호출을 나누세요.",
            mutation_started=False,
            safe_to_repeat=True,
        )
    misplaced = tuple(
        index
        for index, request in enumerate(execution_requests[:-1])
        if request.post_selection != "keep"
    )
    if misplaced:
        raise HwpLiveError(
            "text.patch batch의 post_selection은 마지막으로 실행되는 대상에만"
            + f" 지정할 수 있습니다; 실행 순서 {misplaced} 번째 대상이 지정했습니다."
            + " 선택 영역은 배치가 끝난 자리에 하나만 남기 때문입니다.",
            mutation_started=False,
            safe_to_repeat=True,
        )


def _verify_batch_readback(
    final: TextPatchRequest,
    readback_kind: TextPatchTargetKind,
    after: NativeSnapshot,
) -> None:
    """배치가 남긴 선택 영역을 파이썬 층이 다시 읽어 확인한다.

    단일 경로(hwp_live_session_structure_mutation.patch_validated_text)가 하는
    것과 같은 확인이고, 같은 계약(matches_replacement_readback)을 쓴다.

    ``readback_kind`` 를 따로 받는 이유: prepared 실행은 semantic 대상을 좌표
    범위로 바꿔 실행한다(effective_prepared_text_patch_requests). 그 자리에서
    실행 후의 kind 로 판독 규약을 고르면, 호출자가 find 로 요청한 서식 전용
    패치가 자동번호 문단에서 "range 는 정확히 일치해야 한다"에 걸린다. 규약을
    고르는 것은 호출자가 요청한 대상 종류다.
    """
    if final.replacement:
        if not after.selection.selected or not matches_replacement_readback(
            after.selected_text,
            final.replacement,
            readback_kind,
        ):
            raise HwpLiveError(
                "text.patch batch 후 변경한 본문 범위를 다시 읽어 확인하지 못했습니다"
            )
    elif after.selection.selected:
        raise HwpLiveError(
            "text.patch batch 삭제 후 선택 영역이 예상대로 접히지 않았습니다"
        )


def _verify_batch_requested_format(
    execution_requests: tuple[TextPatchRequest, ...],
    after: NativeSnapshot,
) -> None:
    """요청한 서식이 실제로 걸렸는지 확인한다 — 왕복 0회.

    ``_validate_batch_verification_shape`` 이 배치의 서식을 하나로 묶어 두었으므로
    확인해야 할 명제는 하나다. 그 명제는 두 관측이 함께 증명한다:

    * 네이티브가 요청한 명령을 **전부** 실행했다 — 하나라도 실패하면
      ``execute_native_actions`` 가 NativeActionFailure 로 멈춘다. 그래서 각
      대상마다 같은 CharShape/ParagraphShape 명령이 실행된 것은 관측이다.
    * 그 명령의 결과가 요청한 값이다 — 마지막 대상의 실제 글자·문단 서식을
      스냅샷에서 그대로 읽어 대조한다.

    두 번째만 빠져 있던 것이 이 수리의 대상이다. 배치 경로는 서식을 실어 보내고
    성공을 보고했지만, 그 서식이 요청한 값인지는 아무도 읽지 않았다.
    """
    final = execution_requests[-1]
    if final.formatting is None:
        return
    verify_requested_text_format(final.formatting, after)


def _apply_batch_post_selection(
    hwp: LiveHwpApplication,
    final: TextPatchRequest,
    after: NativeSnapshot,
    guard: Callable[[], None],
) -> None:
    """검증이 끝난 뒤 선택 영역을 요청한 위치로 접는다.

    배치 계약에는 post_selection 이 있는데 배치 실행 경로에는 없었다 — 서식이
    실린 find/range/table_cell 패치가 단일 경로에서 이 경로로 우회하면서 요청이
    조용히 사라졌다. 단일 경로와 같은 순서(재판독·서식 확인 후 접기)로 지원한다.
    """
    if final.post_selection == "keep":
        return
    if after.selection.selected:
        endpoint = (
            after.selection.start
            if final.post_selection == "collapse_to_start"
            else after.selection.end
        )
    else:
        # 삭제는 네이티브가 이미 삭제 시작점으로 커서를 접었고 선택이 없다.
        # 그때 after.selection 의 끝점은 선택이 아니라 잔상이므로, 접을 곳의
        # 유일한 진실은 현재 커서다(단일 경로와 같은 판단).
        endpoint = after.cursor
    guard()
    collapsed = hwp.set_pos(endpoint.list_id, endpoint.paragraph, endpoint.character)
    guard()
    if (
        not collapsed
        or hwp.get_pos() != (endpoint.list_id, endpoint.paragraph, endpoint.character)
        or hwp.get_selected_pos()[0]
    ):
        raise HwpLiveError(
            "text.patch batch 검증 후 선택 영역을 요청한 위치로 접지 못했습니다"
        )


def patch_validated_text_batch(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    requests: tuple[TextPatchRequest, ...],
    unsafe_selectors: set[str],
    guard: Callable[[], None],
    prepared: TextPatchPreparedReceipt | None = None,
) -> TextPatchResult:
    """Execute ordered text patches in one native action request."""
    planning_started = perf_counter_ns()
    execution_requests = requests
    # 판독 규약은 실행 후의 대상 종류가 아니라 호출자가 요청한 종류로 고른다.
    requested_kinds: tuple[TextPatchTargetKind, ...] = tuple(
        request.target.kind for request in requests
    )
    if prepared is not None:
        canonical = canonical_prepared_text_patch_requests(requests)
        if canonical is None:
            raise HwpLiveError(
                "semantic text.patch target cannot use a prepared receipt",
                mutation_started=False,
                safe_to_repeat=True,
            )
        # Reconstructs the read-only request the receipt was issued for, so this
        # compile must mirror the preflight one. The execution compile below is
        # the one that must never carry prepared sentinels.
        canonical_request = _prepared_request(
            candidate, compile_text_patch_batch(canonical, preflight=True)
        )
        if prepared.target_count != len(
            requests
        ) or prepared.request_sha256 != _request_sha256(canonical_request):
            raise HwpLiveError(
                "text.patch prepared receipt가 현재 batch와 다릅니다",
                mutation_started=False,
                safe_to_repeat=True,
            )
        requested_kinds = tuple(request.target.kind for request in canonical)
        execution_requests = effective_prepared_text_patch_requests(
            requests, prepared.targets
        )
    _validate_batch_verification_shape(execution_requests)
    plan = compile_text_patch_batch(execution_requests)
    planning_elapsed = (perf_counter_ns() - planning_started) // 1_000
    require_writable_document(unsafe_selectors, candidate.selector)
    guard()
    readback_started = perf_counter_ns()
    before = read_native_snapshot(candidate.window_handle)
    if before is None:
        raise HwpLiveError("text.patch batch 전 한컴 문서 상태를 읽지 못했습니다")
    before_readback_elapsed = (perf_counter_ns() - readback_started) // 1_000
    mutation_started = perf_counter_ns()
    action_request = NativeActionRequest(
        candidate.document_id,
        candidate.full_name,
        plan.commands,
        expected_cursor=before.cursor,
        expected_selection=before.selection,
    )
    native = (
        execute_native_actions(
            candidate.window_handle,
            action_request,
            minimum_version=plan.minimum_protocol,
        )
        if prepared is None
        else execute_native_actions(
            candidate.window_handle,
            action_request,
            minimum_version=plan.minimum_protocol,
            expected_content_revision=prepared.content_revision,
        )
    )
    mutation_elapsed = (perf_counter_ns() - mutation_started) // 1_000
    if native is None:
        raise HwpLiveError(
            f"프로토콜 {plan.minimum_protocol} 네이티브 text.patch batch를 사용할 수 없습니다"
        )
    readback_started = perf_counter_ns()
    # 네이티브 실행은 여기서 끝났다. 이 뒤의 실패는 "몇 개가 실행됐는지"를
    # 추측하게 두지 않는다 — 네이티브가 보고한 실행 명령 수를 실패에 실어
    # 보내고, 역패치 롤백은 그 관측만 근거로 쓴다(R8).
    try:
        after = read_native_snapshot(candidate.window_handle)
        if after is None:
            raise HwpLiveError("text.patch batch 후 한컴 문서 상태를 읽지 못했습니다")
        guard()
        if execution_requests:
            final_request = execution_requests[-1]
            _verify_batch_readback(final_request, requested_kinds[-1], after)
            _verify_batch_requested_format(execution_requests, after)
            _apply_batch_post_selection(hwp, final_request, after, guard)
    except NativeActionFailure:
        # 네이티브가 스스로 센 수를 이미 들고 있다. 다시 포장하지 않는다.
        raise
    except HwpLiveError as error:
        raise TextPatchBatchVerificationError(
            error, native.commands_executed
        ) from error
    readback_elapsed = (
        before_readback_elapsed + (perf_counter_ns() - readback_started) // 1_000
    )
    return TextPatchResult(
        native,
        before,
        after,
        phase_timings=(
            TextPatchPhaseTiming("planning_preflight", "python", planning_elapsed),
            TextPatchPhaseTiming("mutation", "python", mutation_elapsed),
            TextPatchPhaseTiming("mutation", "native", native.elapsed_microseconds),
            TextPatchPhaseTiming("readback", "python", readback_elapsed),
        ),
    )

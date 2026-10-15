from __future__ import annotations

# pyright: reportPrivateUsage=false

from collections.abc import Callable
from dataclasses import replace
from time import perf_counter_ns
from typing import Final

from hwp_checkpoint_signature import checkpoint_signatures_match
from hwp_errors import HwpLiveError
from hwp_live_api import LiveHwpApplication
from hwp_live_edit_history import (
    NO_CHECKPOINT_EVIDENCE,
    LiveEditHistoryStore,
    LogicalTextPatchBatchHistoryEntry,
)
from hwp_live_edit_history_runtime import (
    _CHECKPOINT_NOT_RECORDED_NOTICE,
    _broken_document_identity,
    _capture_failure_clause,
    _capture_reason_clause,
    _identity_notice_after_applied_edit,
    _prepare_text_patch_checkpoint,
    _record_completed_edit,
    _remove_path,
    _restore_prepared_before_failure,
    full_document_checkpoint_unavailable_reason as document_checkpoint_unavailable_reason,
)
from hwp_live_native_action_contract import (
    NativeActionFailure,
    NativeActionFailureEvidence,
)
from hwp_live_native_action_models import (
    BooleanValue,
    IntegerValue,
    NativeActionCommand,
    NativeActionRequest,
    NativePosition,
    NativeSetter,
    MovePositionCommand,
    ParameterActionCommand,
    TextPatchCommand,
    TextValue,
)
from hwp_live_native_batch import (
    execute_native_actions,
    preflight_native_text_patches,
    read_native_content_signature,
    remembered_content_signature_refusal,
)
from hwp_live_native_text_format import ParagraphFormatting, paragraph_command
from hwp_live_rot import HwpDocumentCandidate
from hwp_live_text_patch_batch import (
    TextPatchBatchVerificationError,
    TextPatchPreparedReceipt,
    compile_text_patch_batch,
    patch_validated_text_batch,
    preflight_text_patch_batch,
    validate_text_patch_plan_guard,
)
from hwp_live_text_patch_prepared import (
    canonical_prepared_text_patch_requests,
    effective_prepared_text_patch_requests,
)
from hwp_live_text_patch_contract import (
    TextPatchPhaseTiming,
    TextPatchPlanGuard,
    TextPatchRequest,
    TextPatchResult,
    validate_text_patch_request,
)


def _replacement_end(start: NativePosition, text: str) -> NativePosition:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return NativePosition(
        start.list_id,
        start.paragraph + len(lines) - 1,
        (start.character if len(lines) == 1 else 0)
        + len(lines[-1].encode("utf-16-le")) // 2,
    )


def _inverse_format_command(
    command: ParameterActionCommand,
    receipt: TextPatchPreparedReceipt,
    target_index: int,
) -> ParameterActionCommand:
    target = receipt.targets[target_index]
    if command.action == "CharShape":
        values: list[NativeSetter] = []
        for setter in command.setters:
            if setter.path == "Bold":
                value = BooleanValue(target.character_format.bold)
            elif setter.path == "Height":
                value = IntegerValue(target.character_format.height_hwpunit)
            elif setter.path == "TextColor":
                value = IntegerValue(target.character_format.text_color)
            elif setter.path.startswith("FaceName"):
                value = TextValue(target.character_format.face_name)
            elif setter.path.startswith("FontType"):
                value = IntegerValue(1)
            else:
                raise HwpLiveError(
                    f"prepared text.patch inverse가 {setter.path} 서식을 캡처하지 못했습니다"
                )
            values.append(NativeSetter(setter.path, value))
        return replace(command, setters=tuple(values))
    if command.action == "ParagraphShape":
        restored = paragraph_command(
            ParagraphFormatting(
                "inherit",
                align_type_raw=(
                    target.alignment
                    if any(item.path == "AlignType" for item in command.setters)
                    else None
                ),
                line_spacing=(
                    target.line_spacing
                    if any(item.path == "LineSpacing" for item in command.setters)
                    else None
                ),
            )
        )
        if restored is None:
            raise HwpLiveError("prepared paragraph inverse가 비어 있습니다")
        return restored
    raise HwpLiveError(
        f"prepared text.patch inverse가 {command.action}을 지원하지 않습니다"
    )


def _prepared_command_inverses(
    requests: tuple[TextPatchRequest, ...],
    receipt: TextPatchPreparedReceipt,
) -> tuple[
    tuple[NativeActionCommand, ...], tuple[tuple[NativeActionCommand, ...], ...]
]:
    canonical = canonical_prepared_text_patch_requests(requests)
    if canonical is None:
        raise HwpLiveError(
            "semantic text.patch target에는 logical inverse를 만들 수 없습니다"
        )
    effective = effective_prepared_text_patch_requests(requests, receipt.targets)
    forward = compile_text_patch_batch(effective).commands
    inverse_by_forward: list[tuple[NativeActionCommand, ...]] = []
    target_index = -1
    active_request: TextPatchRequest | None = None
    for command in forward:
        if isinstance(command, TextPatchCommand):
            target_index += 1
            active_request = effective[target_index]
            state = receipt.targets[target_index] if receipt.targets else None
            start = (
                state.selection.start
                if state is not None
                else active_request.target.start
            )
            if start is None:
                raise HwpLiveError("prepared text.patch inverse 시작 위치가 없습니다")
            old_text = (
                active_request.expected_text
                if active_request.formatting is not None
                and active_request.expected_text == active_request.replacement
                else state.text
                if state is not None
                else active_request.expected_text
            )
            if old_text is None:
                raise HwpLiveError("prepared text.patch inverse 원문이 없습니다")
            if active_request.replacement:
                inverse = TextPatchCommand(
                    "range",
                    active_request.replacement,
                    old_text,
                    start=start,
                    end=_replacement_end(start, active_request.replacement),
                )
                inverse_by_forward.append((inverse,))
            else:
                inverse_by_forward.append(
                    (
                        MovePositionCommand(start),
                        TextPatchCommand(
                            "current",
                            None,
                            old_text,
                        ),
                    )
                )
            continue
        if not isinstance(command, ParameterActionCommand) or active_request is None:
            raise HwpLiveError("prepared text.patch command 순서가 올바르지 않습니다")
        if not receipt.targets:
            raise HwpLiveError("prepared text.patch formatting inverse가 없습니다")
        inverse_by_forward.append(
            (_inverse_format_command(command, receipt, target_index),)
        )
    return forward, tuple(inverse_by_forward)


def _completed_prepared_inverse(
    inverse_by_forward: tuple[tuple[NativeActionCommand, ...], ...],
    completed: int,
) -> tuple[NativeActionCommand, ...]:
    """Bind each completed target's format inverses to its restored text selection."""
    target_groups: list[list[tuple[NativeActionCommand, ...]]] = []
    for inverse_group in inverse_by_forward[:completed]:
        if any(isinstance(command, TextPatchCommand) for command in inverse_group):
            target_groups.append([inverse_group])
        elif target_groups:
            target_groups[-1].append(inverse_group)
        else:
            raise HwpLiveError(
                "prepared text.patch inverse target 순서가 올바르지 않습니다"
            )
    return tuple(
        command
        for target_group in reversed(target_groups)
        for inverse_group in (target_group[0], *reversed(target_group[1:]))
        for command in inverse_group
    )


def _restored_prepared_failure(error: Exception) -> HwpLiveError:
    if isinstance(error, NativeActionFailure):
        return NativeActionFailure(
            NativeActionFailureEvidence(
                error.code,
                error.location,
                error.message,
                0,
                error.failed_step,
                False,
                True,
                None,
                None,
            )
        )
    if isinstance(error, HwpLiveError):
        error.mutation_started = False
        error.safe_to_repeat = True
        return error
    return HwpLiveError(
        str(error),
        mutation_started=False,
        safe_to_repeat=True,
    )


def _completed_forward_commands(error: Exception, total: int) -> int | None:
    """역패치를 몇 개까지 되돌려야 하는지에 대한 *확인 가능한* 근거.

    None 은 "모른다"이고, 그때는 되돌리지 않는다. 예전에는 여기서 모르는 경우를
    전량 실행으로 가정했는데(``completed = len(inverse_by_forward)``), 그
    가정이 틀리면 실행된 적 없는 명령의 역패치까지 문서에 쏘게 된다 — 되돌리기가
    아니라 새 편집이다.
    """
    if isinstance(error, NativeActionFailure):
        # 네이티브가 직접 센 수다. commands_completed 는 실패한 명령을 빼고
        # 세므로, 그 명령이 사후 검사에서 멈추기 전에 이미 문서를 바꿨다면
        # (partial_mutation) 그 하나의 역패치도 필요하다.
        completed = error.commands_completed + (1 if error.partial_mutation else 0)
        return min(max(completed, 0), total)
    if isinstance(error, HwpLiveError):
        if error.mutation_started is False:
            return 0
        if isinstance(error, TextPatchBatchVerificationError):
            # 네이티브 실행 자체는 끝났고 그 뒤의 판독·검증에서 실패한 경우다.
            # patch_validated_text_batch 가 네이티브 결과의 실행 명령 수를
            # 실패에 실어 보낸다.
            return min(max(error.native_commands_completed, 0), total)
    return None


def _rollback_prepared_text_patch_batch(
    candidate: HwpDocumentCandidate,
    forward: tuple[NativeActionCommand, ...],
    inverse_by_forward: tuple[tuple[NativeActionCommand, ...], ...],
    error: Exception,
    before_signature: str,
    receipt: TextPatchPreparedReceipt,
    *,
    completed_commands: int | None = None,
) -> bool:
    completed = (
        _completed_forward_commands(error, len(inverse_by_forward))
        if completed_commands is None
        else min(max(completed_commands, 0), len(inverse_by_forward))
    )
    if completed is None:
        raise HwpLiveError(
            "prepared text.patch 실패가 몇 번째 명령까지 실행됐는지 말해 주지 않아"
            + " 역패치를 만들지 못했습니다; 전량 실행을 가정하고 되돌리는 대신"
            + " 문서를 네이티브가 멈춘 자리에 그대로 두었습니다"
            + f"; 원인: {error}",
            mutation_started=True,
            safe_to_repeat=False,
        ) from error
    inverse = _completed_prepared_inverse(inverse_by_forward, completed)
    if not inverse:
        return False
    try:
        restored = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(candidate.document_id, candidate.full_name, inverse),
            minimum_version=14,
        )
        restored_signature = (
            read_native_content_signature(candidate.window_handle) or ""
        )
        readback = preflight_native_text_patches(
            candidate.window_handle,
            NativeActionRequest(candidate.document_id, candidate.full_name, forward),
        )
    except Exception as rollback_error:  # noqa: BLE001 - restoration is uncertain
        raise HwpLiveError(
            "prepared text.patch inverse rollback 실행 또는 검증에 실패했습니다"
            + f"; 원인: {error}; rollback: {rollback_error}",
            mutation_started=True,
            safe_to_repeat=False,
        ) from rollback_error
    exact_targets_restored = (
        readback is not None and readback.targets == receipt.targets
    )
    # 지문 대조는 체크포인트가 쓰는 그 규약(checkpoint_signatures_match)을 그대로
    # 쓴다. 원시 문자열 비교는 같은 문서를 다른 세대의 서명 형식으로 읽었을 때
    # 거짓 불일치를 내고, "빈 지문이면 검사 생략"은 대조를 못 한 것을 통과로
    # 바꿔 놓았다. 못 한 것은 못 했다고 말한다.
    signature_confirmed = checkpoint_signatures_match(
        before_signature, restored_signature
    )
    if restored is None or not exact_targets_restored or not signature_confirmed:
        raise HwpLiveError(
            "prepared text.patch inverse rollback 뒤 편집 전 상태를 확인하지 못했습니다"
            + f"; 역패치 실행={'보고됨' if restored is not None else '보고 없음'}"
            + f"; 대상 원문·서식 복원={'확인' if exact_targets_restored else '미확인'}"
            + "; 문서 지문 대조="
            + _signature_verdict(before_signature, restored_signature)
            + f"; 최초 실패: {error}",
            mutation_started=True,
            safe_to_repeat=False,
        ) from error
    return True


def _signature_verdict(before_signature: str, restored_signature: str) -> str:
    if not before_signature or not restored_signature:
        return "지문을 읽지 못해 대조 불가"
    return (
        "일치"
        if checkpoint_signatures_match(before_signature, restored_signature)
        else "불일치"
    )


def _text_length_stable_characters() -> frozenset[str]:
    """The characters a `TEXT` export writes one code unit per UTF-16 unit.

    `REV`'s text length is the length of ``GetTextFile("TEXT")`` in UTF-16
    units, and that export is an EUC-KR round trip: what survives it unchanged
    in length is exactly what EUC-KR can encode in one or two bytes as a single
    character. M1 measured the round trip character by character (20/20 plus
    the follow-up sweep) and only two families came back 1:1 -- printable ASCII
    0x20..0x7E, and the 2,350 KS X 1001 한글 음절, which are the syllables whose
    EUC-KR encoding is two bytes. Everything else grows: the 8,822 extended
    syllables (`힣`, `똠`, `갂`), emoji, combining sequences.

    Control characters are outside the set by construction -- the ASCII half
    starts at 0x20 -- and that is where the newline rule lands too. A newline
    leaves the export as CR LF, two units for the one unit it occupies in the
    request, so an expected_text carrying one would break the equality this set
    exists to guarantee. It cannot: CR and LF are not in here.

    Built once, at import. Measured at 2.5ms for the whole 11,172-syllable
    sweep, which is why it is a frozenset and not a per-call test.
    """
    stable = {chr(code) for code in range(0x20, 0x7F)}
    for code in range(0xAC00, 0xD7A4):
        character = chr(code)
        try:
            encoded = character.encode("euc_kr")
        except UnicodeEncodeError:
            continue
        if len(encoded) == 2:
            stable.add(character)
    return frozenset(stable)


_TEXT_LENGTH_STABLE_CHARACTERS: Final = _text_length_stable_characters()


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def prepared_patch_keeps_refusal(
    requests: tuple[TextPatchRequest, ...],
    before_page_count: int,
    after_page_count: int,
) -> bool:
    """Whether this batch cannot have made a refusing document signable.

    Pure arithmetic over the request list; it reads nothing and calls nothing.
    True is the narrow claim that ``_engine_still_refuses``
    (hwp_live_native_batch.py :761) would answer True after this edit, so the
    revision-token read that produces that answer -- about 6s on a 317MB
    document -- may be skipped. False is "not proven", never "the refusal
    lapsed"; the caller then reads exactly as it did before.

    That function keeps a refusal when the session tag matches and none of page
    count, control count and text length dropped. The session tag cannot move
    inside one call. The other three are answered here, one condition each:

    * text length is ``GetTextFile("TEXT")`` in UTF-16 units, so the change is
      Σ(S(replacement) − S(expected_text)) with S the EUC-KR export length. For
      an expected_text drawn entirely from ``_text_length_stable_characters``
      S(expected) is its UTF-16 length exactly, and no string exports shorter
      than its UTF-16 length, so S(replacement) ≥ len16(replacement). A
      non-negative Σ(len16(replacement) − len16(expected_text)) therefore bounds
      the real delta from below at zero. The expected side must be restricted
      and the replacement side need not be, because the inequality runs the
      safe way only in that direction.
    * control count cannot drop, because every target is a ``find``. M1 proved
      a find target cannot delete a control (4 kinds, 4/4): the anchor's eight
      cells break the text run, so a match crossing one fails as
      ``TEXT_OCCURRENCE_NOT_FOUND``. A ``range`` target can delete one -- a
      snapped endpoint takes the anchor with it -- which is why range, current
      and the cell kinds are refused here rather than reasoned about.
    * page count is observed rather than derived: ``TextPatchResult``'s before
      and after snapshots carry the same number `REV` reports (3/3), so the
      caller hands both in.

    Nothing is remembered or invalidated by answering this. A True that was
    somehow wrong would cost one missing MCP undo entry -- the same price the
    refusal path already announces -- and self-heals on the next read.
    """
    if not requests:
        return False
    if after_page_count < before_page_count:
        return False
    text_length_delta = 0
    for request in requests:
        if request.target.kind != "find":
            return False
        expected = request.expected_text
        if expected is None:
            return False
        if not all(
            character in _TEXT_LENGTH_STABLE_CHARACTERS for character in expected
        ):
            return False
        text_length_delta += _utf16_units(request.replacement) - _utf16_units(expected)
    return text_length_delta >= 0


def _after_signature_repeats_the_refusal(
    candidate: HwpDocumentCandidate,
    requests: tuple[TextPatchRequest, ...],
    result: TextPatchResult,
    before_signature: str,
) -> bool:
    """Whether reading the post-edit signature can only re-derive the empty one.

    Three facts, all cheap. The pre-edit read already came back empty, so this
    document was refused before the edit. A refusal is remembered for this
    window, so the post-edit read would go to ``_engine_still_refuses`` rather
    than to the engine (``read_native_content_signature``,
    hwp_live_native_batch.py :2939, consults the cache and then that memory
    before ever calling ``ContentSignature``). And the batch cannot have
    lowered any measure that function compares. Then the read's whole product
    is the empty string this returns instead.

    ``remembered_content_signature_refusal`` is the I/O-free half of that
    memory on purpose: ``engine_refuses_content_signature`` would answer the
    same question by reading the revision token on a miss, which is the ~6s
    call being avoided.

    Skipping touches nothing. It does not fill the signature cache the way the
    real read does, and M2 measured that fill to be inert anyway -- the global
    clear in ``_call_mutation``'s finally empties it 99-166µs later, before any
    reader gets to it. The refusal memory is untouched on both paths: today's
    True branch of ``_engine_still_refuses`` leaves it exactly as it found it.
    """
    if before_signature:
        return False
    if remembered_content_signature_refusal(candidate.window_handle) is None:
        return False
    return prepared_patch_keeps_refusal(
        requests, result.before.page_count, result.after.page_count
    )


def record_prepared_text_patch_history(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    result: TextPatchResult,
    forward: tuple[NativeActionCommand, ...],
    inverse_by_forward: tuple[tuple[NativeActionCommand, ...], ...],
    before_signature: str,
    requests: tuple[TextPatchRequest, ...],
) -> None:
    after_signature = (
        ""
        if _after_signature_repeats_the_refusal(
            candidate, requests, result, before_signature
        )
        else read_native_content_signature(candidate.window_handle) or ""
    )
    inverse = _completed_prepared_inverse(inverse_by_forward, len(inverse_by_forward))
    history.record(
        LogicalTextPatchBatchHistoryEntry(
            candidate.document_id,
            candidate.full_name,
            "text.patch",
            forward,
            inverse,
            before_signature,
            after_signature,
            result.before.page_count,
            result.after.page_count,
            result.after.current_page,
            (result.after.current_page,),
        )
    )


def _execute_prepared_text_patch_batch(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    requests: tuple[TextPatchRequest, ...],
    unsafe_selectors: set[str],
    guard: Callable[[], None],
    receipt: TextPatchPreparedReceipt,
) -> TextPatchResult:
    # 편집 전 지문 읽기는 이 경로가 가진 before-image 확보 그 자체다 — 체크포인트
    # 경로가 `checkpoint_before` 로 재는 그 자리이고, 되돌린 뒤의 대조도 체크포인트와
    # 같은 규약(checkpoint_signatures_match)을 쓴다. 실사용 저널에서 이 호출은 편집당
    # 수 초를 쓰면서도 어느 phase 에도 귀속되지 않아 응답에서 보이지 않았다.
    signature_before_started = perf_counter_ns()
    before_signature = read_native_content_signature(candidate.window_handle) or ""
    signature_before_elapsed = (perf_counter_ns() - signature_before_started) // 1_000
    try:
        return _patch_prepared_text_batch(
            hwp,
            candidate,
            history,
            requests,
            unsafe_selectors,
            guard,
            receipt,
            before_signature,
            signature_before_elapsed,
        )
    except HwpLiveError as error:
        # 실패도 그 시간을 들고 나간다. 실패 응답에서 빠지면 실패한 편집이 쓴 시간은
        # 다시 보이지 않는다 — `_unrecorded_failure` 가 지키는 규약과 같다.
        error.phase_timings = (
            *error.phase_timings,
            ("checkpoint_before", "python", signature_before_elapsed),
        )
        raise


def _patch_prepared_text_batch(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    requests: tuple[TextPatchRequest, ...],
    unsafe_selectors: set[str],
    guard: Callable[[], None],
    receipt: TextPatchPreparedReceipt,
    before_signature: str,
    signature_before_elapsed: int,
) -> TextPatchResult:
    forward, inverse_by_forward = _prepared_command_inverses(requests, receipt)
    try:
        result = patch_validated_text_batch(
            hwp, candidate, requests, unsafe_selectors, guard, receipt
        )
    except Exception as error:  # noqa: BLE001 - exact inverse transaction boundary
        if not (isinstance(error, HwpLiveError) and error.mutation_started is False):
            restored = _rollback_prepared_text_patch_batch(
                candidate,
                forward,
                inverse_by_forward,
                error,
                before_signature,
                receipt,
            )
            if restored:
                raise _restored_prepared_failure(error) from error
        raise
    # 편집 뒤 지문 읽기는 이 이력 기록 안에 있다. 체크포인트 경로가 같은 이름으로
    # `_record_completed_edit` 한 덩어리를 재듯이, 여기서도 이력 기록 한 덩어리를
    # 잰다 — 그 안에서 시간을 쓰는 것은 사실상 지문 읽기 하나다.
    history_started = perf_counter_ns()
    try:
        record_prepared_text_patch_history(
            candidate,
            history,
            result,
            forward,
            inverse_by_forward,
            before_signature,
            requests,
        )
    except Exception as error:  # noqa: BLE001 - history is transactional here
        # 여기 오는 실패는 편집이 *끝난 뒤* 이력 기록에서 난 것이다. 전량 실행은
        # 가정이 아니라 관측이다 — patch_validated_text_batch 가 result 를
        # 돌려주었고, 그 안의 네이티브 결과가 모든 명령의 실행을 보고했다.
        restored = _rollback_prepared_text_patch_batch(
            candidate,
            forward,
            inverse_by_forward,
            error,
            before_signature,
            receipt,
            completed_commands=len(inverse_by_forward),
        )
        if restored:
            raise _restored_prepared_failure(error) from error
        raise
    history_elapsed = (perf_counter_ns() - history_started) // 1_000
    # 새 항목은 일어난 순서대로 앞뒤에 끼운다 — before-image 확보는 편집보다 앞이고
    # 이력 기록은 판독보다 뒤다. 체크포인트 경로가 같은 이름들로 내는 순서
    # (validation, planning_preflight, checkpoint_before, mutation,
    # checkpoint_after_history, total)와 읽는 방향이 같아야 워터폴이 거짓말을 하지
    # 않는다. 이미 나가고 있던 항목들끼리의 상대순서와 이름·값은 그대로다.
    return replace(
        result,
        phase_timings=(
            TextPatchPhaseTiming(
                "checkpoint_before", "python", signature_before_elapsed
            ),
            *result.phase_timings,
            TextPatchPhaseTiming("checkpoint_after_history", "python", history_elapsed),
        ),
    )


# The checkpointed path answers a mid-run failure with "the copy was restored"
# or "restoring it failed". This path has no copy at all, so neither sentence is
# available and the difference matters to whoever reads the failure: the document
# is left wherever the native run stopped and nothing here will move it back.
_UNRECORDED_FAILURE_CLAUSE: Final = (
    "체크포인트 없이 실행하다 실패했고, 되돌린 것은 없습니다"
)


def _unrecorded_failure(
    error: HwpLiveError,
    notice: str,
    timings: tuple[TextPatchPhaseTiming, ...],
    total_started: int,
) -> None:
    """Carry the missing-checkpoint notice and the phase timings out on the error.

    Without this the failure arrives as a bare HwpLiveError with empty
    `phase_timings`, which is exactly the shape a failed checkpoint rollback
    has - the reader cannot tell the two apart, and the one fact that separates
    them (no checkpoint was ever taken, so no rollback ran) is the one the
    reader needs.
    """
    error.reason = "; ".join(
        part for part in (error.reason, _UNRECORDED_FAILURE_CLAUSE, notice) if part
    )
    error.args = (error.reason,)
    error.phase_timings = (
        *(
            (timing.phase, timing.source, timing.elapsed_microseconds)
            for timing in timings
        ),
        ("total", "python", (perf_counter_ns() - total_started) // 1_000),
    )


def _unrecorded_text_patch_batch(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    requests: tuple[TextPatchRequest, ...],
    unsafe_selectors: set[str],
    guard: Callable[[], None],
    receipt: TextPatchPreparedReceipt | None,
    notice: str,
    timings: tuple[TextPatchPhaseTiming, ...],
    total_started: int,
) -> TextPatchResult:
    """Patch the batch without an MCP undo entry, saying why it has none."""
    try:
        result = patch_validated_text_batch(
            hwp,
            candidate,
            requests,
            unsafe_selectors,
            guard,
            receipt,
        )
    except HwpLiveError as error:
        _unrecorded_failure(error, notice, timings, total_started)
        raise
    return replace(
        result,
        notice=notice,
        phase_timings=(
            *timings,
            *result.phase_timings,
            TextPatchPhaseTiming(
                "total", "python", (perf_counter_ns() - total_started) // 1_000
            ),
        ),
    )


def execute_managed_text_patch_batch(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    requests: tuple[TextPatchRequest, ...],
    unsafe_selectors: set[str],
    guard: Callable[[], None],
    plan_guard: TextPatchPlanGuard | None = None,
) -> TextPatchResult:
    """Run one checkpoint lifecycle around one native text patch batch."""
    total_started = perf_counter_ns()
    validation_started = perf_counter_ns()
    try:
        has_exact_omission = any(
            request.expected_text is None
            and request.target.kind in {"range", "table_cell"}
            for request in requests
        )
        if has_exact_omission and plan_guard is None:
            raise HwpLiveError(
                "expected_text omission requires all text.patch batch plan guards",
                mutation_started=False,
                safe_to_repeat=True,
            )
        if plan_guard is not None:
            validate_text_patch_plan_guard(plan_guard)
        allow_exact_omission = plan_guard is not None
        for request in requests:
            validate_text_patch_request(
                request,
                allow_exact_omission=allow_exact_omission,
                require_current_expected=True,
            )
        # Validate the preparation payload. Inspected literal offsets become
        # executable native coordinates only after the read-only preflight.
        _ = compile_text_patch_batch(requests, preflight=True)
    except HwpLiveError as error:
        validation_elapsed = (perf_counter_ns() - validation_started) // 1_000
        error.phase_timings = (
            ("validation", "python", validation_elapsed),
            ("total", "python", (perf_counter_ns() - total_started) // 1_000),
        )
        raise
    validation_elapsed = (perf_counter_ns() - validation_started) // 1_000
    preflight_started = perf_counter_ns()
    try:
        receipt = preflight_text_patch_batch(candidate, requests, plan_guard)
    except HwpLiveError as error:
        preflight_elapsed = (perf_counter_ns() - preflight_started) // 1_000
        error.phase_timings = (
            ("validation", "python", validation_elapsed),
            ("planning_preflight", "python", preflight_elapsed),
            ("total", "python", (perf_counter_ns() - total_started) // 1_000),
        )
        raise
    preflight_elapsed = (perf_counter_ns() - preflight_started) // 1_000
    if (
        receipt is not None
        and canonical_prepared_text_patch_requests(requests) is not None
    ):
        prepared_result = _execute_prepared_text_patch_batch(
            hwp,
            candidate,
            history,
            requests,
            unsafe_selectors,
            guard,
            receipt,
        )
        return replace(
            prepared_result,
            phase_timings=(
                TextPatchPhaseTiming("validation", "python", validation_elapsed),
                TextPatchPhaseTiming("planning_preflight", "python", preflight_elapsed),
                TextPatchPhaseTiming(
                    "planning_preflight",
                    "native",
                    receipt.native_elapsed_microseconds,
                ),
                *prepared_result.phase_timings,
                TextPatchPhaseTiming(
                    "total", "python", (perf_counter_ns() - total_started) // 1_000
                ),
            ),
        )
    unavailable = document_checkpoint_unavailable_reason(candidate)
    if unavailable:
        # No checkpoint may be taken: either two copies will not fit the
        # session's disk history, or 한/글 has already proved it cannot sign this
        # document. Refusing here would cost the user the whole batch to protect
        # an undo entry they were never going to get, so patch and say the entry
        # is missing -- a later hwp_undo falls through to 한/글's own history.
        return _unrecorded_text_patch_batch(
            hwp,
            candidate,
            requests,
            unsafe_selectors,
            guard,
            receipt,
            unavailable,
            (
                TextPatchPhaseTiming("validation", "python", validation_elapsed),
                TextPatchPhaseTiming("planning_preflight", "python", preflight_elapsed),
                *(
                    ()
                    if receipt is None
                    else (
                        TextPatchPhaseTiming(
                            "planning_preflight",
                            "native",
                            receipt.native_elapsed_microseconds,
                        ),
                    )
                ),
            ),
            total_started,
        )
    checkpoint_started = perf_counter_ns()
    # A broken document identity leaves here as DocumentIdentityError and stops
    # the call before the edit: it is the one failure where going ahead would
    # write the user's work into a file that no longer exists.
    prepared = _prepare_text_patch_checkpoint(candidate, history)
    checkpoint_before_elapsed = (perf_counter_ns() - checkpoint_started) // 1_000
    if isinstance(prepared, str):
        # The document could not be checkpointed - the classic case is a
        # picture-heavy document the bridge could not copy. Patch and report the
        # missing undo entry in the words the preparation step chose: it is the
        # only place that saw the reason.
        return _unrecorded_text_patch_batch(
            hwp,
            candidate,
            requests,
            unsafe_selectors,
            guard,
            receipt,
            prepared,
            (
                TextPatchPhaseTiming("validation", "python", validation_elapsed),
                TextPatchPhaseTiming("planning_preflight", "python", preflight_elapsed),
                *(
                    ()
                    if receipt is None
                    else (
                        TextPatchPhaseTiming(
                            "planning_preflight",
                            "native",
                            receipt.native_elapsed_microseconds,
                        ),
                    )
                ),
                TextPatchPhaseTiming(
                    "checkpoint_before", "python", checkpoint_before_elapsed
                ),
            ),
            total_started,
        )
    mutation_started = perf_counter_ns()
    try:
        result = patch_validated_text_batch(
            hwp,
            candidate,
            requests,
            unsafe_selectors,
            guard,
            receipt,
        )
    except Exception as error:  # noqa: BLE001 - transaction rollback boundary
        mutation_elapsed = (perf_counter_ns() - mutation_started) // 1_000
        if isinstance(error, HwpLiveError) and error.mutation_started is False:
            cleanup_started = perf_counter_ns()
            _remove_path(prepared.before.path)
            _remove_path(prepared.after_path)
            cleanup_elapsed = (perf_counter_ns() - cleanup_started) // 1_000
            error.phase_timings = (
                ("validation", "python", validation_elapsed),
                ("planning_preflight", "python", preflight_elapsed),
                ("checkpoint_before", "python", checkpoint_before_elapsed),
                ("mutation", "python", mutation_elapsed),
                ("checkpoint_after_history", "python", cleanup_elapsed),
                ("total", "python", (perf_counter_ns() - total_started) // 1_000),
            )
            raise
        rollback_started = perf_counter_ns()
        _restore_prepared_before_failure(candidate, prepared, error)
        rollback_elapsed = (perf_counter_ns() - rollback_started) // 1_000
        if isinstance(error, HwpLiveError):
            error.phase_timings = (
                ("validation", "python", validation_elapsed),
                ("planning_preflight", "python", preflight_elapsed),
                ("checkpoint_before", "python", checkpoint_before_elapsed),
                ("mutation", "python", mutation_elapsed),
                ("rollback", "python", rollback_elapsed),
                ("total", "python", (perf_counter_ns() - total_started) // 1_000),
            )
        raise
    checkpoint_after_started = perf_counter_ns()
    try:
        _, _elapsed, after_evidence = _record_completed_edit(
            candidate,
            history,
            prepared,
            require_complete_signatures=True,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        _remove_path(prepared.before.path)
        _remove_path(prepared.after_path)
        checkpoint_after_elapsed = (
            perf_counter_ns() - checkpoint_after_started
        ) // 1_000
        broken = _broken_document_identity(candidate, candidate.full_name, error)
        return replace(
            result,
            notice=(
                _identity_notice_after_applied_edit(broken)
                if broken is not None
                else (_CHECKPOINT_NOT_RECORDED_NOTICE + _capture_failure_clause(error))
            ),
            checkpoint_evidence=prepared.checkpoint_evidence,
            phase_timings=(
                TextPatchPhaseTiming("validation", "python", validation_elapsed),
                TextPatchPhaseTiming("planning_preflight", "python", preflight_elapsed),
                *(
                    ()
                    if receipt is None
                    else (
                        TextPatchPhaseTiming(
                            "planning_preflight",
                            "native",
                            receipt.native_elapsed_microseconds,
                        ),
                    )
                ),
                TextPatchPhaseTiming(
                    "checkpoint_before", "python", checkpoint_before_elapsed
                ),
                *result.phase_timings,
                TextPatchPhaseTiming(
                    "checkpoint_after_history",
                    "python",
                    checkpoint_after_elapsed,
                ),
                TextPatchPhaseTiming(
                    "total",
                    "python",
                    (perf_counter_ns() - total_started) // 1_000,
                ),
            ),
        )
    checkpoint_after_elapsed = (perf_counter_ns() - checkpoint_after_started) // 1_000
    timings = (
        TextPatchPhaseTiming("validation", "python", validation_elapsed),
        TextPatchPhaseTiming(
            "planning_preflight",
            "python",
            preflight_elapsed,
        ),
        *(
            ()
            if receipt is None
            else (
                TextPatchPhaseTiming(
                    "planning_preflight",
                    "native",
                    receipt.native_elapsed_microseconds,
                ),
            )
        ),
        TextPatchPhaseTiming(
            "checkpoint_before",
            "python",
            checkpoint_before_elapsed,
        ),
        *result.phase_timings,
        TextPatchPhaseTiming(
            "checkpoint_after_history",
            "python",
            checkpoint_after_elapsed,
        ),
        TextPatchPhaseTiming(
            "total",
            "python",
            (perf_counter_ns() - total_started) // 1_000,
        ),
    )
    result = replace(result, phase_timings=timings)
    evidence = prepared.checkpoint_evidence.merged(after_evidence)
    if evidence == NO_CHECKPOINT_EVIDENCE:
        return result
    if evidence.unavailable_reason:
        return replace(
            result,
            checkpoint_evidence=evidence,
            notice=result.notice
            or (
                "편집을 적용하고 MCP 되돌리기 기록도 남겼습니다. 다만 문서 사본 방식 "
                + "체크포인트를 쓰지 못해 예전 방식으로 떴습니다."
                + _capture_reason_clause(evidence.unavailable_reason)
            ),
        )
    return replace(result, checkpoint_evidence=evidence)

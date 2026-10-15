from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
import hashlib
import os
from pathlib import Path
import shutil
from typing import Final, Protocol

from hwp_checkpoint_signature import (
    checkpoint_signature_is_complete,
    checkpoint_signatures_match,
)
from hwp_errors import HwpLiveError
from hwp_live_api import LiveHwpApplication
from hwp_live_document_edit_commands import build_delete_control_commands
from hwp_live_edit_history import (
    MAX_NATIVE_HISTORY_STEPS,
    MAX_CHECKPOINT_SOURCE_FILE_BYTES,
    NO_CHECKPOINT_EVIDENCE,
    DocumentCheckpoint,
    DocumentCheckpointEvidence,
    DocumentEditHistoryEntry,
    HistoryDirection,
    HistoryOperation,
    LiveEditHistoryEntry,
    LiveEditHistoryStore,
    LogicalTextPatchBatchHistoryEntry,
    LogicalTextPatchHistoryEntry,
    NativeDocumentStructureSnapshot,
    NativeDocumentEditHistoryEntry,
    PageControlSnapshot,
    build_capture_document_commands,
    checkpoint_meta_path,
    checkpoint_restore_metadata,
    checkpoint_files,
    read_checkpoint_signature,
)
from hwp_live_native_action_contract import NativeActionFailure
from hwp_live_native_action_models import (
    DeleteControlCommand,
    NativeActionCommand,
    NativeActionRequest,
    NativeActionResult,
    NativeIntegerCallResult,
    NativePageControl,
    NativePageInspection,
    NativePosition,
    NativeSelection,
    NativeTextCallResult,
    TextPatchCommand,
    RestoreDocumentFileCommand,
)
from hwp_live_native_batch import (
    engine_refuses_content_signature,
    execute_native_actions,
    forget_cached_content_signatures,
    inspect_native_page,
    native_foreground_guard,
    peek_cached_content_signature,
    read_native_content_signature,
    read_native_snapshot,
    remember_content_signature_refusal,
)
from hwp_live_native_history import (
    NativeHistoryResult,
    execute_native_history,
    native_history_content_precondition,
    read_native_document_structure,
)
from hwp_live_rot import HwpDocumentCandidate
from hwp_live_session_structure_mutation import patch_validated_text
from hwp_live_text_patch_contract import TextPatchRequest, TextPatchResult
from hwp_operation_contract import OperationResult
from hwp_operation_local_precondition import native_failure_left_document_untouched

__all__ = ("_execute_native_history_until_signature",)


_RESTORE_EVIDENCE_METHOD = "DocumentCheckpointRestore"
_RESTORE_EVIDENCE_MATCHES: Final = frozenset(
    (
        "content_signature_match",
        "structural_signature_match_text_empty",
        "document_file_reopen_normalized",
    )
)

# ActionLifecycle.cpp:43-46. The bridge says which of the two capture paths it
# actually used, and separately whether it had to put the editing session back
# on the user's own file after SaveAs adopted the copy.
_CAPTURE_EVIDENCE_METHOD: Final = "DocumentCheckpointCapture"
_CAPTURE_METHODS: Final = frozenset(("document_file", "encoded_block"))
_CAPTURE_IDENTITY_RESTORED: Final = "document_identity_restored"

# ActionLifecycle.cpp, `CaptureDocumentFileCheckpoint`. Every place the on-disk
# capture gives up without an error now says which gate it hit, because the
# fallback that runs next can succeed and hide the fact entirely. An older
# bridge emits none of these and the reason is then simply unknown — which is
# reported as unknown, never guessed.
_CAPTURE_UNAVAILABLE_METHOD: Final = "DocumentCheckpointUnavailable"
_CAPTURE_UNAVAILABLE_REASONS: Final[dict[str, str]] = {
    "document_path_unknown": "한/글이 현재 문서의 파일 경로를 알려주지 않았습니다"
    + "(저장된 적 없는 문서입니다)",
    "save_as_failed": "한/글의 사본 저장(SaveAs) 호출 자체가 실패했습니다",
    "save_as_refused": "한/글이 사본 저장(SaveAs)을 거부했습니다",
    "checkpoint_file_empty": "한/글이 사본을 저장했다고 했지만 파일이 비어 있었습니다",
    "checkpoint_meta_write_failed": "체크포인트 사본은 저장했지만 함께 쓰는 "
    + "메타 파일을 쓰지 못했습니다",
}


def _capture_reason_clause(reason: str) -> str:
    """The bridge's own account of why the on-disk capture did not happen."""
    explained = _CAPTURE_UNAVAILABLE_REASONS.get(reason)
    if explained is None:
        return ""
    return f" 원인: {explained}"


# An undo record is bookkeeping. The edit the caller asked for is the work.
# When only the bookkeeping fails, the work still happens and the answer says
# what the user lost - which is the MCP undo entry, not their edit.
_CHECKPOINT_UNAVAILABLE_NOTICE: Final = (
    "이 문서는 MCP 되돌리기 기록을 남기지 못했습니다"
    "(편집 전 문서 체크포인트를 저장하지 못했습니다). 편집은 그대로 적용됐습니다. "
    "되돌리려면 한/글에서 되돌리기(Ctrl+Z)를 쓰세요"
)


def document_too_large_notice(full_name: str) -> str:
    """What to say when the document is too big to checkpoint at all.

    This is the case the field run hit and nobody was told about: the edit is
    applied and verified, no checkpoint is even attempted, and the old code
    returned the plain success line. `hwp_undo` then found an empty MCP history
    and the user learned about it by pressing undo.

    The sizes are in the sentence on purpose. A limit stated as a rule invites
    the reader to argue with the rule; the two numbers say what was measured and
    what it was measured against.
    """
    megabyte = 1024 * 1024
    try:
        size = Path(full_name).stat().st_size
    except OSError:
        measured = "문서 크기를 읽지 못했지만"
    else:
        measured = f"문서가 {size / megabyte:,.0f}MB 라"
    limit = MAX_CHECKPOINT_SOURCE_FILE_BYTES / megabyte
    return (
        f"편집은 적용하고 검증했습니다. 다만 {measured} 편집 전후 문서 체크포인트를 "
        f"뜨지 않았습니다(한 번의 편집은 복구용 사본까지 최대 3개를 남기고, 세션의 디스크 이력 "
        f"한도상 체크포인트를 뜰 수 있는 문서는 {limit:,.0f}MB 까지입니다). "
        "그래서 이 편집에는 MCP 되돌리기 기록이 없고 hwp_undo 로는 되돌릴 수 "
        "없습니다. 되돌리려면 한/글에서 되돌리기(Ctrl+Z)를 쓰세요"
    )


def unsignable_document_notice(full_name: str) -> str:
    """What to say when 한/글 cannot sign a document this big.

    A checkpoint is only usable if its content signature can be checked
    against the live document, and that signature comes from the engine
    serialising the whole document (`GetTextFile("HWPML2X")`). Past its own
    memory ceiling the engine spends the entire attempt and returns success
    with an empty string, so the checkpoint is written, weighed, and then
    provably worthless.

    The size is in the sentence but no limit is, and that asymmetry is the
    honest part: the ceiling is the engine's allocation failure, not a number
    this code owns. An 82MB document signed fine where a 317MB one did not, and
    nothing here can say where in between it turns over — only that this
    document was measured and refused.
    """
    megabyte = 1024 * 1024
    try:
        size = Path(full_name).stat().st_size
    except OSError:
        measured = "이 문서는"
    else:
        measured = f"이 문서는 {size / megabyte:,.0f}MB 라"
    return (
        f"편집은 적용하고 검증했습니다. 다만 {measured} 한/글이 문서 전체 지문을 "
        "만들지 못해, 확인할 수 없는 체크포인트를 뜨는 대신 뜨지 않았습니다. "
        "그래서 이 편집에는 MCP 되돌리기 기록이 없고 hwp_undo 로는 되돌릴 수 "
        "없습니다. 되돌리려면 한/글에서 되돌리기(Ctrl+Z)를 쓰세요"
    )


_CHECKPOINT_NOT_RECORDED_NOTICE: Final = (
    "편집은 적용하고 검증했지만, 편집 후 문서 체크포인트를 저장하지 못해 "
    "MCP 되돌리기 기록을 남기지 못했습니다. "
    "되돌리려면 한/글에서 되돌리기(Ctrl+Z)를 쓰세요"
)
# 삭제 계열은 체크포인트를 못 떠도 편집을 진행한다. 대신 되돌리기가 어떻게
# 처리되는지 답에 싣는다. 이 문구들은 성공 문장을 대체한다
# (`hwp_live_document_edit_recipe.py` 의 history_notice 규약). 그래서 무슨 일이
# 있었는지까지 이 한 문장이 전부 말해야 한다.
#
# `text.patch` 는 자기 경로(`execute_managed_text_patch`)에서 위의 두 문구를
# 쓰므로 여기로 오지 않는다. 항목이 있는 것은 이 표가 HistoryOperation 전체를
# 덮어 조회에 구멍이 없게 하기 위해서다.
_DELETION_APPLIED: Final[dict[HistoryOperation, str]] = {
    "control.delete": "지정한 기존 개체를 삭제하고 빠른 구조에서 제거를 확인했습니다",
    "document.delete_page": "지정한 쪽을 삭제하고 페이지 수 감소를 확인했습니다",
    "text.patch": "본문을 바꿨습니다",
}
# 편집 전 캡처가 실패한 경우: 되돌리기는 한컴 실행 이력 기반으로 기록된다.
_DELETION_WITHOUT_CHECKPOINT_NOTICES: Final[dict[HistoryOperation, str]] = {
    operation: (
        f"{applied}. 편집 전 문서 체크포인트를 만들지 않았으므로 MCP 되돌리기는 "
        "문서 체크포인트 대신 한컴 실행 이력으로 처리됩니다"
    )
    for operation, applied in _DELETION_APPLIED.items()
}
# 편집 후 기록이 실패한 경우: 이 편집에는 MCP 되돌리기 기록이 아예 없다.
_DELETION_NOT_RECORDED_NOTICES: Final[dict[HistoryOperation, str]] = {
    operation: (
        f"{applied}. 다만 편집 후 문서 체크포인트를 저장하지 못해 MCP 되돌리기 "
        "기록을 남기지 못했습니다. 되돌리려면 한/글에서 되돌리기(Ctrl+Z)를 쓰세요"
    )
    for operation, applied in _DELETION_APPLIED.items()
}

# ActionLifecycle.cpp 의 `CaptureDocumentFileCheckpoint` 가 SaveAs 로 사본을 뜬
# 뒤 편집 세션이 그 사본으로 옮겨간 것을 발견했고, 원래 문서로 되돌리는 것까지
# 실패했을 때 내는 코드다. 이 실패는 "부수 기록을 못 남겼다"가 아니라 문서
# 신원이 깨졌다는 뜻이고, 다른 캡처 실패와 절대 같이 다루면 안 된다.
_DOCUMENT_IDENTITY_FAILURE_CODE: Final = "DOCUMENT_CHECKPOINT_IDENTITY"
# `DOCUMENT_CHECKPOINT_CAPTURE` and the `BLOCK_FILE_*` / `BLOCK_OUTPUT_*`
# writers all describe the checkpoint path itself, so their message is the one
# thing worth repeating to a user who just lost an undo entry. Any other native
# code is about the edit, not the checkpoint, and is left out.
_CHECKPOINT_FAILURE_CODE_PREFIXES: Final = ("DOCUMENT_CHECKPOINT", "BLOCK_")


class DocumentIdentityError(HwpLiveError):
    """편집 세션이 사용자의 문서가 아니라 삭제된 체크포인트 사본에 붙어 있다.

    이 상태에서 편집을 계속하면 그 편집이 어디에 저장될지 아무도 모른다.
    한/글이 들고 있는 문서 경로는 이 코드가 방금 지운 임시 파일이므로 이후
    `hwp_save` 는 그 경로로 간다. 그래서 여기서만은 편집을 막는다 — 다른
    캡처 실패는 전부 편집을 진행한다.
    """

    def __init__(self, document_path: str, observed_path: str) -> None:
        self.document_path: str = document_path
        self.observed_path: str = observed_path
        observed = observed_path or "알 수 없는 경로"
        # 이 예외가 나가는 자리는 편집을 시작하기 전뿐이다. 문서가 바뀌지
        # 않았다는 것을 아는 채로 "바뀌었을 수도 있다"고 답하지 않는다.
        super().__init__(
            "문서 체크포인트를 뜨는 동안 한/글의 편집 세션이 체크포인트 임시 "
            + f"사본({observed})으로 옮겨졌고, 원래 문서로 되돌리지 못했습니다. "
            + "그 임시 파일은 이미 지워졌으므로 지금 저장하면 편집 내용이 어디에도 "
            + "남지 않습니다. 그래서 편집을 진행하지 않았습니다(문서 내용은 이 "
            + "호출로 바뀌지 않았습니다). 한/글에서 [파일] - [다른 이름으로 저장하기]로 "
            + f"원래 경로 {document_path} 에 저장해 편집 세션을 원래 문서로 되돌린 "
            + "뒤 다시 요청하세요",
            mutation_started=False,
        )


class CheckpointDriftError(HwpLiveError):
    """The document changed after the MCP edit that captured this checkpoint.

    Restoring a document checkpoint deletes the whole document and pours the
    saved copy back in. If anything changed since the checkpoint was taken —
    typically text typed straight into 한/글 between the MCP edit and this
    undo — that change is inside what gets deleted, and no redo brings it back.
    This is raised instead whenever the stored endpoints do not uniquely
    authorize a checkpoint restore. Walking through 한/글's own history is not
    safe either when the live state may include somebody else's later edit.
    """

    def __init__(self, expected: str, actual: str) -> None:
        self.expected: str = expected
        self.actual: str = actual
        reason = (
            "현재 문서 지문이 기록된 편집 전후 상태와 일치하지 않아"
            if actual
            else "현재 문서 지문을 읽지 못해"
        )
        super().__init__(
            f"{reason} 문서 체크포인트를 덮어쓰지 않았습니다. "
            + "문서는 바뀌지 않았습니다. 후속 편집을 저장하거나 되돌린 뒤 다시 "
            + "시도하거나 한/글에서 직접 되돌리세요"
        )


class WindowHandleCandidate(Protocol):
    @property
    def window_handle(self) -> int: ...


def read_checkpoint_evidence(
    native: NativeActionResult | None,
) -> DocumentCheckpointEvidence:
    """Read the capture evidence out of one native batch result.

    An unknown value is dropped rather than passed through: the field is a
    closed set on the public contract, and inventing a member of it because a
    future bridge said something new would be a worse answer than saying
    nothing.
    """
    if native is None:
        return NO_CHECKPOINT_EVIDENCE
    method = ""
    identity_restored = False
    unavailable_reason = ""
    for entry in native.call_results:
        if not isinstance(entry, NativeTextCallResult):
            continue
        if entry.method == _CAPTURE_UNAVAILABLE_METHOD:
            if entry.value in _CAPTURE_UNAVAILABLE_REASONS:
                unavailable_reason = entry.value
            continue
        if entry.method != _CAPTURE_EVIDENCE_METHOD:
            continue
        if entry.value == _CAPTURE_IDENTITY_RESTORED:
            identity_restored = True
        elif entry.value in _CAPTURE_METHODS:
            method = entry.value
    return DocumentCheckpointEvidence(
        capture_method=method,
        identity_restored=identity_restored,
        unavailable_reason=unavailable_reason,
    )


def _capture_failure_clause(error: BaseException) -> str:
    """Why the capture failed, in the bridge's own words, or "".

    A capture that fails outright raises before any evidence can be read, so the
    reason has to come off the failure. The bridge's checkpoint failures already
    carry one — `DOCUMENT_CHECKPOINT_CAPTURE` with the legacy path's empty
    `GetTextFile` result is the picture-heavy case — and repeating it is what
    turns "no undo entry" into something the reader can act on.

    Anything else is left unexplained rather than explained wrongly: a stat
    failure or a missing bridge is not a fact about the checkpoint path.
    """
    if isinstance(error, NativeActionFailure) and error.code.startswith(
        _CHECKPOINT_FAILURE_CODE_PREFIXES
    ):
        return f" 원인: {error.message}"
    return ""


@dataclass(frozen=True, slots=True)
class PreparedDocumentEdit:
    document_id: int
    full_name: str
    operation: HistoryOperation
    before: DocumentCheckpoint
    after_path: Path
    page: int
    before_controls: tuple[PageControlSnapshot, ...]
    target_control_ids: tuple[str, ...]
    capture_elapsed_microseconds: int
    checkpoint_evidence: DocumentCheckpointEvidence = NO_CHECKPOINT_EVIDENCE
    p1_single_text_undo: bool = False
    changed_pages: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class LiveEditHistoryExecution:
    commands_executed: int
    elapsed_microseconds: int
    custom_history: bool
    # What the caller has to tell the user instead of the stock success line.
    # Empty means "nothing unusual happened"; anything else is the whole story
    # and replaces the message, never appends to it.
    notice: str = ""
    # Whether the document's final state was proven. None means "could not be
    # confirmed" — not "wrong". It rides out to OperationResult.verified, which
    # is a tri-state for exactly this reason.
    state_confirmed: bool | None = True
    # Every checkpoint this execution wrote, folded into one answer.
    checkpoint_evidence: DocumentCheckpointEvidence = NO_CHECKPOINT_EVIDENCE
    # Pages the managed MCP edit recorded as affected. This survives a
    # checkpoint reopen, whose caret normally returns to page 1.
    changed_pages: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class NativeHistoryWalk:
    """Outcome of walking 한/글's own history toward a checkpoint signature.

    This never means "the call failed". `reached` says whether the target state
    was observed; everything else describes exactly where the document ended up
    so the answer can say it out loud.
    """

    reached: bool
    steps_walked: int
    steps_restored: int
    elapsed_microseconds: int
    # Only meaningful when `reached` is False. True: the document is provably
    # back where this call found it. False: it provably is not. None: the
    # content signature probe could not answer, so this is unknown.
    restore_confirmed: bool | None
    notice: str

    @property
    def net_applied(self) -> int:
        """한/글 history steps still applied to the document when this returned."""
        return self.steps_walked - self.steps_restored


def deletion_without_checkpoint_notice(operation: HistoryOperation) -> str:
    """편집 전 체크포인트를 뜨지 못한 삭제가 사용자에게 해야 할 말.

    삭제는 그대로 진행됐고 되돌리기도 있다. 다만 그 되돌리기가 문서
    체크포인트가 아니라 한컴 실행 이력이라는 사실이 달라진다.
    """
    return _DELETION_WITHOUT_CHECKPOINT_NOTICES[operation]


def should_use_document_checkpoint(full_name: str) -> bool:
    """Whether this document is small enough to checkpoint at all.

    The bridge now captures a checkpoint by asking 한/글 to save a copy of the
    document (ActionLifecycle.cpp, `CaptureDocumentFileCheckpoint`), so the old
    reason for this limit — a whole document serialised into one BSTR and
    written as ASCII, which the native writer refuses past 256 MiB — no longer
    applies to the capture itself.

    The limit stays because of what a checkpoint costs after it is written. One
    edit records before and after, and a failed restore deliberately keeps one
    rollback copy. Past this threshold those three document-sized files cannot
    fit `MAX_HISTORY_DISK_BYTES`, so capturing them would exceed the request's
    disk bound. The threshold is derived from that budget rather than written
    down twice: see `MAX_CHECKPOINT_SOURCE_FILE_BYTES`.

    **Answering False costs the caller their MCP undo entry, so every caller has
    to say so.** This is the gate the 331 MB field document hit, and the answer
    it produced said nothing at all: the edit reported `succeeded`, `verified`,
    `document_checkpoint_capture: null`, and the user found out by pressing
    undo. `document_too_large_notice` is what that costs now, and
    `execute_managed_text_patch` attaches it.

    An unsaved or unreadable path answers True: it has no size on disk to be
    over the limit, and the store still refuses files it cannot hold.

    **This is half of the question.** It says what the session can hold, not
    what 한/글 can sign, and a checkpoint that cannot be signed is not a
    checkpoint. `document_checkpoint_unavailable_reason` is the whole question
    and is what the edit paths ask.
    """
    try:
        return Path(full_name).stat().st_size <= MAX_CHECKPOINT_SOURCE_FILE_BYTES
    except OSError:
        return True


def document_checkpoint_unavailable_reason(candidate: HwpDocumentCandidate) -> str:
    """Why no checkpoint may be attempted here, or "" when one may be.

    Two independent things stop a checkpoint and neither can stand in for the
    other, which is exactly how the field failure happened: the size gate said
    yes to a 313MiB document — it is under the 341MiB the disk budget allows —
    and the engine, never asked, said no. The edit paid two whole-document
    saves and two refused serialisations for a signature that could not exist,
    and then refused to touch the document at all.

    * `should_use_document_checkpoint` bounds what the session's disk history
      can *hold*. It is derived from `MAX_HISTORY_DISK_BYTES` and knows nothing
      about 한/글.
    * `engine_refuses_content_signature` answers what 한/글 can *sign*, from
      what a previous attempt already measured, without starting another one.
      A checkpoint that cannot be signed is thrown away by
      `checkpoint_signature_is_complete` further down whatever it cost.

    Answering non-empty costs the caller their MCP undo entry, so the answer is
    the notice they must attach rather than a bare False. It is never a reason
    to refuse the edit: the edit is the work, the undo entry is bookkeeping.
    """
    if not should_use_document_checkpoint(candidate.full_name):
        return document_too_large_notice(candidate.full_name)
    if engine_refuses_content_signature(candidate.window_handle):
        return unsignable_document_notice(candidate.full_name)
    return ""


def full_document_checkpoint_unavailable_reason(candidate: HwpDocumentCandidate) -> str:
    if os.environ.get("HWP_FULL_DOCUMENT_CHECKPOINTS") != "1":
        return (
            "전체 문서 체크포인트 자동 저장을 생략했습니다. "
            "이 편집은 한/글 되돌리기(Ctrl+Z)를 사용하세요."
        )
    return document_checkpoint_unavailable_reason(candidate)


def _control_state(
    controls: tuple[NativePageControl, ...],
) -> tuple[PageControlSnapshot, ...]:
    return tuple(
        PageControlSnapshot(
            instance_id=control.instance_id,
            control_type=control.control_type,
            rows=control.rows,
            columns=control.columns,
        )
        for control in controls
    )


def _remove_path(path: Path) -> None:
    for owned in checkpoint_files(path):
        try:
            owned.unlink(missing_ok=True)
        except OSError:
            pass


def _capture_checkpoint(
    candidate: HwpDocumentCandidate,
    document_id: int,
    full_name: str,
    path: Path,
    page_count: int,
) -> tuple[DocumentCheckpoint, int, DocumentCheckpointEvidence]:
    native = execute_native_actions(
        candidate.window_handle,
        NativeActionRequest(
            document_id,
            full_name,
            build_capture_document_commands(path),
        ),
        minimum_version=9,
    )
    if native is None:
        raise HwpLiveError(
            "한컴 프로토콜 9 문서 체크포인트 저장기를 사용할 수 없습니다"
        )
    try:
        size = path.stat().st_size
    except OSError as error:
        raise HwpLiveError("문서 체크포인트 파일을 확인하지 못했습니다") from error
    if size < 1:
        raise HwpLiveError("문서 체크포인트 파일이 비어 있습니다")
    signature = read_checkpoint_signature(path)
    try:
        checkpoint_meta_path(path).unlink(missing_ok=True)
    except OSError:
        pass
    return (
        DocumentCheckpoint(path, size, page_count, signature),
        native.elapsed_microseconds,
        read_checkpoint_evidence(native),
    )


def _unsignable_capture_discarded(
    candidate: HwpDocumentCandidate,
    checkpoint: DocumentCheckpoint,
    *owned: Path,
) -> bool:
    """Throw away a checkpoint 한/글 wrote but could not sign, and remember why.

    The capture succeeded — the copy is on disk — and it is still worthless.
    Every restore begins by refusing a checkpoint whose content signature is
    incomplete (`_restore_checkpoint`), because a copy that cannot be compared
    against the live document cannot be shown to be the right copy to write
    over it. So keeping this one buys nothing and promises something: an undo
    entry that would fail at the moment the user reached for it.

    The refusal is handed to the signature memory here because this is where it
    was observed. The capture bought the verdict inside the native call, where
    `read_native_content_signature` never sees it; without this the next edit
    on the same document would buy it again, at the price of another
    whole-document save first. That memory is re-checked against the cheap
    revision token, so a document that shrinks back under the engine's ceiling
    is retried rather than written off.
    """
    if checkpoint_signature_is_complete(checkpoint.signature):
        return False
    for path in owned:
        _remove_path(path)
    remember_content_signature_refusal(candidate.window_handle)
    return True


def _reported_identity_failure_path(error: BaseException) -> str:
    """브리지가 신원 실패로 지목한 원래 문서 경로, 아니면 "".

    판정은 C++ 이 한다. 여기서는 그 판정 결과를 코드로 읽기만 한다.
    """
    if (
        isinstance(error, NativeActionFailure)
        and error.code == _DOCUMENT_IDENTITY_FAILURE_CODE
    ):
        return error.location or ""
    return ""


def _live_document_path(candidate: WindowHandleCandidate) -> str | None:
    """한/글이 지금 들고 있는 문서 경로. 읽지 못하면 None."""
    snapshot = read_native_snapshot(candidate.window_handle)
    if snapshot is None:
        return None
    return snapshot.full_name


def _broken_document_identity(
    candidate: HwpDocumentCandidate,
    full_name: str,
    error: BaseException,
) -> DocumentIdentityError | None:
    """신원이 정말로 깨졌는지 대조한 결과. 멀쩡하면 None.

    브리지 보고만 믿지 않는다. 브리지가 `DOCUMENT_CHECKPOINT_IDENTITY` 를
    냈더라도 한/글이 여전히 원래 문서를 들고 있으면 그것은 문서 신원이 아니라
    체크포인트 하나를 잃은 것이고, 그때는 다른 캡처 실패와 똑같이 다룬다.

    경로를 읽지 못하면(스냅샷 실패) 깨진 쪽으로 답한다. 브리지는 이미 신원이
    깨졌다고 말했고, 확인이 안 되는 상태에서 편집을 진행하는 것이 바로 이
    결함이 만드는 손실이기 때문이다.
    """
    document_path = _reported_identity_failure_path(error)
    if not document_path:
        return None
    observed = _live_document_path(candidate)
    if observed is not None and os.path.normcase(
        os.path.abspath(observed)
    ) == os.path.normcase(os.path.abspath(full_name)):
        return None
    return DocumentIdentityError(document_path, "" if observed is None else observed)


def _raise_when_document_identity_broke(
    candidate: HwpDocumentCandidate,
    full_name: str,
    error: BaseException,
) -> None:
    """편집 전이다. 신원이 깨졌으면 편집을 시작하지 않는다."""
    broken = _broken_document_identity(candidate, full_name, error)
    if broken is not None:
        raise broken from error


def _identity_notice_after_applied_edit(broken: DocumentIdentityError) -> str:
    """편집이 이미 적용된 뒤에 신원이 깨진 것을 알았을 때 할 말.

    되돌릴 수도, 못 한 척할 수도 없다. 편집은 남기고 사용자가 지금 무엇을
    해야 하는지 말한다.
    """
    observed = broken.observed_path or "알 수 없는 경로"
    return (
        "편집은 적용했습니다. 그런데 편집 후 문서 체크포인트를 뜨는 동안 한/글의 "
        f"편집 세션이 이미 지워진 임시 사본({observed})으로 옮겨졌고 원래 문서로 "
        "되돌리지 못했습니다. 이 상태로 저장하면 편집 내용이 원래 파일에 남지 "
        "않습니다. 한/글에서 [파일] - [다른 이름으로 저장하기]로 원래 경로 "
        f"{broken.document_path} 에 지금 저장하세요. MCP 되돌리기 기록도 남기지 "
        "못했습니다"
    )


def _probe_content_signature(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    document_id: int,
    full_name: str,
) -> str:
    """Content signature of the document as it stands right now, or ""."""
    _ = history, document_id, full_name
    try:
        return read_native_content_signature(candidate.window_handle) or ""
    except (HwpLiveError, OSError, ValueError):
        return ""


def _restored_content_was_verified(native: NativeActionResult) -> bool:
    return any(
        isinstance(entry, NativeTextCallResult)
        and entry.method == _RESTORE_EVIDENCE_METHOD
        and entry.value in _RESTORE_EVIDENCE_MATCHES
        for entry in native.call_results
    )


_RESTORE_PAGE_COUNTS: Final = (
    ("DocumentRestorePagesAfterEmptying", "비운 뒤"),
    ("DocumentRestorePagesAfterInsert", "삽입 뒤"),
    ("DocumentRestorePagesAfterTrim", "정리 뒤"),
)
_RESTORE_ATTEMPT_METHOD: Final = "DocumentRestoreAttempt"
_RESTORE_BODY_AFTER_EMPTYING_METHOD: Final = "DocumentRestoreBodyAfterEmptying"
_RESTORE_INSERT_METHOD: Final = "DocumentCheckpointRestoreInsert"


def _emptying_body_description(value: str) -> str:
    prefix = "empty at "
    if value.startswith(prefix):
        return f"비운 뒤 본문 없음 ({value[len(prefix) :]})"
    return f"비운 뒤 본문 상태: {value}"


def read_restore_page_counts(native: NativeActionResult) -> str:
    """복원이 잰 세 쪽 수를 한 줄로. 없으면 빈 문자열.

    실패한 복원은 이 숫자를 오류 문구에 싣지만, 성공한 복원은 아무 말도 하지
    않았다. 그래서 정상 동작하는 문서 셋의 숫자를 한 번도 못 봤고, 훼손되는
    문서 하나와 비교할 것이 없었다. 성공했을 때도 나와야 하는 이유다.

    네이티브가 내보내도 여기서 읽지 않으면 응답까지 오지 않는다. 이 작업에서
    같은 이유로 계측을 세 번 잃었다 — 실패 응답이 CALL 결과를 안 실어서 한 번,
    base64 안의 base64 를 평문으로 찾아서 한 번, 그리고 파이썬이 읽지 않아서
    한 번.
    """
    attempts: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    labels = dict(_RESTORE_PAGE_COUNTS)
    for entry in native.call_results:
        if isinstance(entry, NativeTextCallResult):
            if entry.method == _RESTORE_ATTEMPT_METHOD:
                current = []
                attempts.append((entry.value, current))
            elif entry.method == _RESTORE_BODY_AFTER_EMPTYING_METHOD:
                if current is None:
                    current = []
                    attempts.append(("", current))
                current.append(_emptying_body_description(entry.value))
            elif entry.method == _RESTORE_INSERT_METHOD:
                if current is None:
                    current = []
                    attempts.append(("", current))
                current.append(entry.value)
        elif (
            isinstance(entry, NativeIntegerCallResult)
            and entry.method in labels
            and current is not None
        ):
            current.append(f"{labels[entry.method]} {entry.value}쪽")
    if attempts:
        described: list[str] = []
        for attempt, evidence in attempts:
            detail = ", ".join(evidence)
            described.append(f"{attempt}: {detail}" if attempt else detail)
        return "복원 계측: " + "; ".join(filter(None, described))

    measured: list[str] = []
    for method, label in _RESTORE_PAGE_COUNTS:
        for entry in native.call_results:
            if isinstance(entry, NativeIntegerCallResult) and entry.method == method:
                measured.append(f"{label} {entry.value}쪽")
                break
    return "복원 계측: " + ", ".join(measured) if measured else ""


def _restore_checkpoint(
    candidate: HwpDocumentCandidate,
    document_id: int,
    full_name: str,
    checkpoint: DocumentCheckpoint,
    *,
    history: LiveEditHistoryStore | None = None,
    expected_signature: str = "",
    engine_undo_origin: DocumentCheckpoint | None = None,
) -> tuple[int, int, str]:
    try:
        actual_size = checkpoint.path.stat().st_size
    except OSError as error:
        raise HwpLiveError(
            "복구할 문서 체크포인트 파일을 확인하지 못했습니다"
        ) from error
    if actual_size != checkpoint.bytes:
        raise HwpLiveError("복구할 문서 체크포인트 파일 크기가 변경되었습니다")
    if not checkpoint_signature_is_complete(checkpoint.signature):
        raise HwpLiveError(
            "복구할 문서 체크포인트 내용 지문이 완전하지 않아 문서를 바꾸지 "
            + "않았습니다",
            mutation_started=False,
        )
    authorization_signature = expected_signature or checkpoint.signature
    if not checkpoint_signature_is_complete(authorization_signature):
        raise HwpLiveError(
            "현재 문서 변경을 허가할 내용 지문이 완전하지 않아 체크포인트를 "
            + "적용하지 않았습니다",
            mutation_started=False,
        )
    if expected_signature and history is not None:
        # `expected_signature` describes the state the MCP edit left behind. The
        # restore below deletes the entire document first, so anything that
        # changed since then is inside what gets deleted. Refuse to be the thing
        # that deletes it; the caller falls back to 한/글's reversible history.
        live_signature = _probe_content_signature(
            candidate,
            history,
            document_id,
            full_name,
        )
        if not _complete_content_matches(expected_signature, live_signature):
            raise CheckpointDriftError(expected_signature, live_signature)
    try:
        with checkpoint_restore_metadata(
            checkpoint,
            full_name,
            engine_undo_origin=engine_undo_origin,
        ):
            native = execute_native_actions(
                candidate.window_handle,
                NativeActionRequest(
                    document_id,
                    full_name,
                    (
                        RestoreDocumentFileCommand(
                            checkpoint.path,
                            checkpoint.page_count,
                        ),
                    ),
                ),
                minimum_version=13,
                expected_content_signature=authorization_signature,
            )
    finally:
        if history is not None:
            history.enforce_limits()
    if native is None:
        raise HwpLiveError("한컴 네이티브 문서 체크포인트 복원기를 사용할 수 없습니다")
    snapshot = read_native_snapshot(candidate.window_handle)
    if snapshot is None:
        raise HwpLiveError("체크포인트 적용 후 네이티브 문서 상태를 읽지 못했습니다")
    expected_key = document_id, os.path.normcase(os.path.abspath(full_name))
    actual_key = (
        snapshot.document_id,
        os.path.normcase(os.path.abspath(snapshot.full_name)),
    )
    if actual_key != expected_key or snapshot.page_count != checkpoint.page_count:
        raise HwpLiveError(
            "체크포인트 적용 후 문서 식별값 또는 페이지 수가 바뀌었습니다"
        )
    # A page count proves almost nothing about a restore: a page whose pictures
    # all vanished still counts as a page. When the checkpoint carries a content
    # signature, the restored document has to reproduce it. The bridge normally
    # checks this itself while the blocks are still in hand; this covers the
    # case where it could not.
    if (
        checkpoint.signature
        and not _restored_content_was_verified(native)
        and history is not None
        and not _complete_content_matches(
            checkpoint.signature,
            _probe_content_signature(candidate, history, document_id, full_name),
        )
    ):
        raise HwpLiveError(
            "체크포인트를 적용했지만 복구된 문서 내용이 체크포인트와 다릅니다"
        )
    return (
        native.commands_executed,
        native.elapsed_microseconds,
        read_restore_page_counts(native),
    )


def prepare_control_deletion(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    document_id: int,
    full_name: str,
    page: int,
    page_count: int,
    target_controls: tuple[NativePageControl, ...],
) -> PreparedDocumentEdit | None:
    """개체 삭제 전 문서를 체크포인트한다. 뜨지 못하면 None.

    None 은 오류가 아니다. 이 삭제에 문서 체크포인트 되돌리기 기록이 붙지
    않는다는 뜻이고, 호출자는 대용량 문서와 똑같이 한컴 이력 기반 경로로
    삭제를 진행한다. 부수 기록을 못 남겼다고 사용자가 요청한 삭제를 통째로
    막는 것이 더 큰 손실이다.

    예외는 하나뿐이다: 문서 신원이 깨졌을 때(`DocumentIdentityError`). 그때는
    편집이 어디에 저장될지 모르므로 진행하지 않는다.
    """
    detailed_page = inspect_native_page(
        candidate.window_handle,
        page,
        include_cells=True,
    )
    if detailed_page is None:
        raise HwpLiveError("개체 삭제 전 대상 쪽의 상세 구조를 읽지 못했습니다")
    before_path = history.new_checkpoint_path()
    after_path = history.new_checkpoint_path()
    try:
        before, elapsed, evidence = _capture_checkpoint(
            candidate,
            document_id,
            full_name,
            before_path,
            page_count,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        _remove_path(before_path)
        _remove_path(after_path)
        _raise_when_document_identity_broke(candidate, full_name, error)
        return None
    if _unsignable_capture_discarded(candidate, before, before_path, after_path):
        return None
    return PreparedDocumentEdit(
        document_id=document_id,
        full_name=full_name,
        operation="control.delete",
        before=before,
        after_path=after_path,
        page=page,
        before_controls=_control_state(detailed_page.controls),
        target_control_ids=tuple(control.instance_id for control in target_controls),
        capture_elapsed_microseconds=elapsed,
        checkpoint_evidence=evidence,
    )


def prepare_page_deletion(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    document_id: int,
    full_name: str,
    page: int,
    page_count: int,
) -> PreparedDocumentEdit | None:
    """쪽 삭제 전 문서를 체크포인트한다. 뜨지 못하면 None.

    `prepare_control_deletion` 과 같은 규약이다: None 은 되돌리기 기록만
    없다는 뜻이고, 문서 신원이 깨졌을 때만 예외가 나간다.
    """
    before_path = history.new_checkpoint_path()
    after_path = history.new_checkpoint_path()
    try:
        before, elapsed, evidence = _capture_checkpoint(
            candidate,
            document_id,
            full_name,
            before_path,
            page_count,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        _remove_path(before_path)
        _remove_path(after_path)
        _raise_when_document_identity_broke(candidate, full_name, error)
        return None
    if _unsignable_capture_discarded(candidate, before, before_path, after_path):
        return None
    return PreparedDocumentEdit(
        document_id=document_id,
        full_name=full_name,
        operation="document.delete_page",
        before=before,
        after_path=after_path,
        page=page,
        before_controls=(),
        target_control_ids=(),
        capture_elapsed_microseconds=elapsed,
        checkpoint_evidence=evidence,
    )


def _record_completed_edit(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    prepared: PreparedDocumentEdit,
    *,
    require_complete_signatures: bool = False,
) -> tuple[DocumentEditHistoryEntry, int, DocumentCheckpointEvidence]:
    snapshot = read_native_snapshot(candidate.window_handle)
    if snapshot is None:
        raise HwpLiveError("편집 후 문서 상태를 읽지 못했습니다")
    after, elapsed, after_evidence = _capture_checkpoint(
        candidate,
        prepared.document_id,
        prepared.full_name,
        prepared.after_path,
        snapshot.page_count,
    )
    if require_complete_signatures and (
        not checkpoint_signature_is_complete(prepared.before.signature)
        or not checkpoint_signature_is_complete(after.signature)
    ):
        # The before-capture is signed — an unsigned one is discarded before
        # the edit — so this is the document crossing 한/글's serialisation
        # ceiling partway through. Say so to the signature memory, or the next
        # edit repeats the whole before-capture to learn it again.
        remember_content_signature_refusal(candidate.window_handle)
        raise HwpLiveError(
            "편집 전후 문서 내용 지문이 완전하지 않아 MCP 되돌리기 기록을 "
            + "남기지 않았습니다"
        )
    after_controls: tuple[PageControlSnapshot, ...] = ()
    if prepared.operation == "control.delete":
        inspected = inspect_native_page(
            candidate.window_handle,
            prepared.page,
            include_cells=True,
        )
        if inspected is None:
            raise HwpLiveError("개체 삭제 후 쪽 구조를 읽지 못했습니다")
        after_controls = _control_state(inspected.controls)
    entry = DocumentEditHistoryEntry(
        document_id=prepared.document_id,
        full_name=prepared.full_name,
        operation=prepared.operation,
        before=prepared.before,
        after=after,
        page=prepared.page,
        before_controls=prepared.before_controls,
        after_controls=after_controls,
        p1_single_text_undo=prepared.p1_single_text_undo,
        changed_pages=prepared.changed_pages,
    )
    history.record(entry)
    return entry, elapsed, after_evidence


def _rollback_recovery_path(error: NativeActionFailure) -> Path | None:
    marker = "a copy of the document as this call found it was kept at "
    _prefix, found, raw_path = error.message.rpartition(marker)
    if not found:
        return None
    path = Path(raw_path.strip())
    if path.suffix.lower() != ".rollback":
        return None
    return path


def _open_rollback_recovery_in_new_tab(
    candidate: HwpDocumentCandidate,
    rollback_error: NativeActionFailure,
) -> Path:
    rollback_path = _rollback_recovery_path(rollback_error)
    if rollback_path is None or not rollback_path.is_file():
        raise HwpLiveError("네이티브 롤백 복구 사본 경로를 확인하지 못했습니다")
    recovery_path = rollback_path.with_suffix(".hwp")
    shutil.copy2(rollback_path, recovery_path)
    with native_foreground_guard(candidate.window_handle):
        _ = candidate.application.XHwpDocuments.Add(True)
        if not candidate.application.Open(str(recovery_path), None, None):
            raise HwpLiveError("복구 사본을 한컴 새 탭에서 열지 못했습니다")
    return recovery_path


def _append_recovery_notice(error: BaseException, notice: str) -> None:
    reason = f"{error}; {notice}"
    error.args = (reason,)
    if isinstance(error, HwpLiveError):
        error.reason = reason


def _restore_prepared_before_failure(
    candidate: HwpDocumentCandidate,
    prepared: PreparedDocumentEdit,
    error: BaseException | None = None,
) -> None:
    """실패한 편집을 편집 전 체크포인트로 되돌린다. 되돌릴 것이 있을 때만.

    되돌리기는 공짜가 아니다. `_restore_checkpoint` 는 열려 있는 문서를 지우고
    체크포인트 사본을 다시 여는 것이고, 이 저장소의 현장 문서(313MiB)에서
    그 한 번이 네이티브 안에서만 19.6초였다(운영 저널 2026-08-18T05:06:16
    document.undo, `native_elapsed_microseconds=19_636_135`, 복원 방식
    document_file_reopen). 되는 동안 사용자의 한/글 실행 이력과 창 상태도 함께
    사라진다.

    그런데 본문 패치·개체 삭제 실패의 큰 몫은 편집 *전* 검사에서 멈춘 것이다
    (ActionTextPatch.cpp 의 `PatchSelectedText` 는 선택만 한 상태에서
    SetError 로 끝난다). 그때 브리지는 완료 명령 0건·부분 변경 없음·재시도
    안전을 함께 실어 보내고, 그 세 증거는 "문서는 체크포인트를 뜬 그 상태
    그대로"라는 뜻이다. 그 위에 사본을 덮어쓰는 것은 아무것도 되돌리지 않으면서
    19.6초와 사용자의 이력을 쓰는 일이다.

    브리지 증거가 없더라도 현재 문서와 편집 전 체크포인트의 완전한 내용 지문이
    정확히 같으면 같은 결론이다. 어느 한쪽이라도 없거나 불완전하면 추정하지
    않고 기존 복원을 그대로 실행한다. 되돌리지 않을 때도 체크포인트 쌍은 지운다.
    편집이 없었으니 되돌리기 기록도 없고, 남기면 디스크 예산만 먹는다.

    두 번째 이유는 진단이다. 이 함수는 `except` 안에서 불리므로 여기서 나는
    예외가 원래 실패를 덮어쓴다. 서명 불가 문서에서는 `_restore_checkpoint` 가
    "복구할 문서 체크포인트 내용 지문이 완전하지 않아..." 로 멈추는데, 그러면
    호출자는 캡션이 왜 안 바뀌었는지 대신 체크포인트 이야기를 듣는다 -- 오늘
    운영 저널에만 그렇게 원인이 지워진 text.replace 가 6건이다.
    """
    if native_failure_left_document_untouched(error):
        _remove_path(prepared.before.path)
        _remove_path(prepared.after_path)
        return
    current_signature: str | None = None
    try:
        forget_cached_content_signatures(candidate.window_handle)
        current_signature = read_native_content_signature(candidate.window_handle)
    except Exception:
        # 읽지 못한 것은 같다는 증거가 아니다. 기존의 보수적인 복원으로 간다.
        pass
    if (
        current_signature is not None
        and checkpoint_signature_is_complete(current_signature)
        and checkpoint_signature_is_complete(prepared.before.signature)
        and current_signature == prepared.before.signature
    ):
        _remove_path(prepared.before.path)
        _remove_path(prepared.after_path)
        return
    try:
        _ = _restore_checkpoint(
            candidate,
            prepared.document_id,
            prepared.full_name,
            prepared.before,
            expected_signature=current_signature or "",
        )
    except NativeActionFailure as rollback_error:
        if rollback_error.code != "DOCUMENT_CHECKPOINT_ROLLBACK":
            raise
        reported_error = error if error is not None else rollback_error
        try:
            recovery_path = _open_rollback_recovery_in_new_tab(
                candidate, rollback_error
            )
        except Exception as recovery_error:
            _append_recovery_notice(
                reported_error,
                "롤백 복구 사본을 새 탭에서 여는 작업도 실패했습니다"
                + f" ({recovery_error})",
            )
        else:
            _append_recovery_notice(
                reported_error,
                f"롤백 복구 사본을 새 탭에서 열었습니다: {recovery_path}",
            )
        if error is None:
            raise
        return
    _remove_path(prepared.before.path)
    _remove_path(prepared.after_path)


def _record_prepared_deletion(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    prepared: PreparedDocumentEdit,
    native: NativeActionResult,
) -> LiveEditHistoryExecution:
    """삭제는 끝났다. 남은 것은 되돌리기 기록뿐이다.

    기록이 실패해도 삭제는 되돌리지 않는다. 그것은 사용자가 요청한 작업이고,
    실패한 것은 부수 기록이다. 체크포인트 쌍은 버려서 반쪽짜리 이력이 남지
    않게 하고, 무엇을 잃었는지 답에 싣는다.
    """
    try:
        _, after_capture_elapsed, after_evidence = _record_completed_edit(
            candidate,
            history,
            prepared,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        _remove_path(prepared.before.path)
        _remove_path(prepared.after_path)
        broken = _broken_document_identity(candidate, prepared.full_name, error)
        return LiveEditHistoryExecution(
            commands_executed=native.commands_executed,
            elapsed_microseconds=(
                prepared.capture_elapsed_microseconds + native.elapsed_microseconds
            ),
            # 체크포인트 쌍을 버렸으니 이 편집에는 문서 체크포인트 이력이 없다.
            custom_history=False,
            notice=(
                _identity_notice_after_applied_edit(broken)
                if broken is not None
                else _DELETION_NOT_RECORDED_NOTICES[prepared.operation]
            ),
            checkpoint_evidence=prepared.checkpoint_evidence,
        )
    return LiveEditHistoryExecution(
        commands_executed=native.commands_executed,
        elapsed_microseconds=(
            prepared.capture_elapsed_microseconds
            + native.elapsed_microseconds
            + after_capture_elapsed
        ),
        custom_history=True,
        checkpoint_evidence=prepared.checkpoint_evidence.merged(after_evidence),
    )


def execute_prepared_control_deletion(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    prepared: PreparedDocumentEdit,
) -> LiveEditHistoryExecution:
    commands = tuple(
        DeleteControlCommand(instance_id) for instance_id in prepared.target_control_ids
    )
    try:
        native = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(prepared.document_id, prepared.full_name, commands),
            minimum_version=9,
        )
        if native is None:
            raise HwpLiveError("한컴 프로토콜 9 기존 개체 삭제기를 사용할 수 없습니다")
    except (HwpLiveError, OSError, ValueError) as error:
        # 삭제 자체가 실패했다. 체크포인트는 바로 이럴 때 쓰라고 뜬 것이다.
        _restore_prepared_before_failure(candidate, prepared, error)
        raise
    return _record_prepared_deletion(candidate, history, prepared, native)


def execute_prepared_page_deletion(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    prepared: PreparedDocumentEdit,
    commands: tuple[NativeActionCommand, ...],
) -> LiveEditHistoryExecution:
    try:
        native = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(prepared.document_id, prepared.full_name, commands),
            minimum_version=9,
        )
        if native is None:
            raise HwpLiveError("한컴 프로토콜 9 쪽 삭제기를 사용할 수 없습니다")
    except (HwpLiveError, OSError, ValueError) as error:
        _restore_prepared_before_failure(candidate, prepared, error)
        raise
    return _record_prepared_deletion(candidate, history, prepared, native)


def _prepare_document_edit_checkpoint(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    operation: HistoryOperation,
) -> PreparedDocumentEdit | str:
    """Checkpoint the document before an edit, or say why it could not be.

    A `str` answer is not an error. It is the notice the caller must attach:
    this edit will have no MCP undo entry, which is a smaller loss than not
    making the edit at all — but only if the answer says so. It carries the
    bridge's own reason when the bridge gave one, because "no undo entry" and
    "no undo entry because 한/글 refused to save a copy" are different facts and
    only the second one can be acted on.

    There is exactly one failure this does not swallow: the bridge reporting
    that the editing session moved onto the checkpoint copy and could not be
    moved back. That is not a missing undo entry, it is a document whose next
    save has nowhere to go, so it leaves as `DocumentIdentityError`.
    """
    snapshot = read_native_snapshot(candidate.window_handle)
    if snapshot is None:
        # No page count, so there is nothing to label a checkpoint with. The
        # capture never runs, so the bridge has no reason to report.
        return _CHECKPOINT_UNAVAILABLE_NOTICE
    before_path = history.new_checkpoint_path()
    after_path = history.new_checkpoint_path()
    try:
        before, elapsed, evidence = _capture_checkpoint(
            candidate,
            candidate.document_id,
            candidate.full_name,
            before_path,
            snapshot.page_count,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        _remove_path(before_path)
        _remove_path(after_path)
        _raise_when_document_identity_broke(candidate, candidate.full_name, error)
        return _CHECKPOINT_UNAVAILABLE_NOTICE + _capture_failure_clause(error)
    if _unsignable_capture_discarded(candidate, before, before_path, after_path):
        # 한/글 saved the copy and then could not sign it. Both are thrown away
        # here rather than after the edit, so this document costs one refused
        # serialisation instead of two, and the next edit costs none.
        return unsignable_document_notice(candidate.full_name)
    return PreparedDocumentEdit(
        document_id=candidate.document_id,
        full_name=candidate.full_name,
        operation=operation,
        before=before,
        after_path=after_path,
        page=snapshot.current_page,
        before_controls=(),
        target_control_ids=(),
        capture_elapsed_microseconds=elapsed,
        checkpoint_evidence=evidence,
    )


def _prepare_text_patch_checkpoint(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
) -> PreparedDocumentEdit | str:
    return _prepare_document_edit_checkpoint(
        candidate,
        history,
        "text.patch",
    )


def _is_p1_single_text_undo(
    request: object,
    result: TextPatchResult,
) -> bool:
    """Whether one engine Undo can be tried without crossing a structural edit."""

    if not isinstance(request, TextPatchRequest):
        return False
    expected = request.expected_text
    selection = result.after.selection
    return (
        request.target.kind == "find"
        and expected is not None
        and bool(expected)
        and bool(request.replacement)
        and not any(mark in expected for mark in ("\r", "\n", "\f"))
        and not any(mark in request.replacement for mark in ("\r", "\n", "\f"))
        and request.formatting is None
        and request.target.table_instance_id is None
        and request.target.cell_address is None
        and result.native.commands_executed == 1
        and selection.selected
        and not selection.cell_addresses
        and selection.start.list_id == 0
        and selection.end.list_id == 0
    )


def _logical_patch_candidate(
    request: TextPatchRequest,
    before: object,
) -> tuple[bool, str]:
    if not isinstance(request, TextPatchRequest):
        return False, ""
    original = request.expected_text
    if original is None:
        selection = getattr(before, "selection", None)
        if not isinstance(selection, NativeSelection) or not selection.selected:
            return False, ""
        original = getattr(before, "selected_text", "")
        if not isinstance(original, str):
            return False, ""
    if request.formatting is not None or original == request.replacement:
        return False, original
    if any(
        mark in original or mark in request.replacement for mark in ("\r", "\n", "\f")
    ):
        return False, original
    return True, original


def _single_paragraph_selection(selection: NativeSelection) -> bool:
    return (
        selection.selected
        and selection.start.list_id == selection.end.list_id
        and selection.start.paragraph == selection.end.paragraph
    )


def _text_position_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _selection_for_text(start: NativePosition, text: str) -> NativeSelection:
    return NativeSelection(
        True,
        start,
        NativePosition(
            start.list_id,
            start.paragraph,
            start.character + _text_position_units(text),
        ),
    )


def _redo_selection_for_patch(
    request: TextPatchRequest,
    before: object,
    start: NativePosition,
    original_text: str,
) -> NativeSelection | None:
    before_selection = getattr(before, "selection", None)
    if (
        request.target.kind == "current"
        and isinstance(before_selection, NativeSelection)
        and _single_paragraph_selection(before_selection)
    ):
        return before_selection
    if (
        request.target.kind == "range"
        and request.target.start is not None
        and request.target.end is not None
    ):
        selection = NativeSelection(True, request.target.start, request.target.end)
        return selection if _single_paragraph_selection(selection) else None
    if len(original_text) != _text_position_units(original_text):
        return None
    return _selection_for_text(start, original_text)


_UNKNOWN_STATE_AFTER_FAILED_PATCH: Final = (
    "본문 패치가 실패했고, 실패 뒤 문서 전체 지문을 읽지 못해 문서가 바뀌었는지 "
    "확인할 수 없었습니다. 확인하지 못한 상태에 되돌리기를 걸면 이 호출과 무관한 "
    "편집까지 지울 수 있으므로 MCP 는 아무것도 되돌리지 않았습니다. 한/글에서 문서를 "
    "확인한 뒤 필요하면 되돌리기(Ctrl+Z)를 쓰세요"
)


def _observed_content_signature(candidate: HwpDocumentCandidate) -> str:
    """이 순간 문서의 전체 내용 지문. 읽지 못하면 "" — 모른다는 뜻이다.

    캐시를 먼저 버린다. 방금 실패한 편집이 문서를 건드렸는지 묻는 자리이고,
    직전 호출이 남긴 값을 되받으면 "안 바뀌었다"는 답만 나온다.
    """
    try:
        forget_cached_content_signatures(candidate.window_handle)
        return read_native_content_signature(candidate.window_handle) or ""
    except (HwpLiveError, OSError, ValueError):
        return ""


def _rollback_failed_logical_patch(
    candidate: HwpDocumentCandidate,
    before_signature: str,
    error: BaseException,
) -> None:
    """실패한 논리 패치를 되돌린다 — 무엇을 되돌리는지 아는 경우에만.

    예전에는 여기서 지문 없이(`after_signature=""`) 곧장 한컴 Undo 를 걸었다.
    그 Undo 는 전제조건이 없어서 무엇을 취소하는지 모른 채 한 단계를 소비한다.
    실패한 패치가 문서를 건드리지 않았다면 그 한 단계는 이 호출과 무관한
    사용자의 직전 편집이고, 되돌리기가 아니라 파괴다.

    그래서 지문을 먼저 읽는다. 세 갈래다.

    * 편집 전 지문과 같다 — 되돌릴 것이 없다. 아무것도 하지 않는다.
    * 다르다 — 무엇을 되돌리는지 알고 거는 것이므로, 그 지문을 전제조건으로
      실은 승인된 Undo 를 건다.
    * 읽지 못했다 — "안 바뀌었다"는 증거도 "바뀌었다"는 증거도 아니다. 모르는
      상태에는 걸지 않고, 무엇을 하지 않았는지 원래 실패에 실어 보낸다
      (1.3.1 계약: 상태 불명이면 편집을 유지하고 정직하게 말한다).
    """
    current_signature = _observed_content_signature(candidate)
    if not checkpoint_signature_is_complete(current_signature):
        _append_recovery_notice(error, _UNKNOWN_STATE_AFTER_FAILED_PATCH)
        return
    if _complete_content_matches(before_signature, current_signature):
        return
    _rollback_logical_patch(candidate, before_signature, current_signature)


def _rollback_logical_patch(
    candidate: HwpDocumentCandidate,
    before_signature: str,
    after_signature: str = "",
) -> None:
    try:
        result = (
            _execute_authorized_native_history(
                candidate.window_handle,
                "undo",
                1,
                after_signature,
            )
            if checkpoint_signature_is_complete(after_signature)
            else execute_native_history(candidate.window_handle, "undo", 1)
        )
        restored_signature = (
            read_native_content_signature(candidate.window_handle) or ""
        )
    except (HwpLiveError, OSError, ValueError) as error:
        raise HwpLiveError(
            "text.patch 변경 뒤 논리 이력 기록이 완성되지 않아 한컴 Undo를 "
            + "시도했지만 편집 전 상태를 확인하지 못했습니다. 문서가 일부 변경된 "
            + "상태일 수 있습니다",
            mutation_started=True,
            safe_to_repeat=False,
        ) from error
    if result.applied != 1 or not _complete_content_matches(
        before_signature,
        restored_signature,
    ):
        raise HwpLiveError(
            "text.patch 변경 뒤 논리 이력 기록이 완성되지 않아 한컴 Undo를 "
            + "시도했지만 편집 전 전체 내용 지문으로 돌아오지 않았습니다. 문서가 "
            + "일부 변경된 상태일 수 있습니다",
            mutation_started=True,
            safe_to_repeat=False,
        )


def execute_managed_text_patch(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    request: TextPatchRequest,
    unsafe_selectors: set[str],
    guard: Callable[[], None],
) -> TextPatchResult:
    unavailable = document_checkpoint_unavailable_reason(candidate)
    if unavailable:
        return replace(
            patch_validated_text(
                hwp,
                candidate,
                request,
                unsafe_selectors,
                guard,
            ),
            notice=unavailable,
        )

    if not isinstance(request, TextPatchRequest) or request.formatting is not None:
        return _execute_checkpointed_text_patch(
            hwp, candidate, history, request, unsafe_selectors, guard
        )
    original = request.expected_text
    if original is not None and (
        original == request.replacement
        or any(
            mark in original or mark in request.replacement
            for mark in ("\r", "\n", "\f")
        )
    ):
        return _execute_checkpointed_text_patch(
            hwp, candidate, history, request, unsafe_selectors, guard
        )
    before_signature = peek_cached_content_signature(candidate.window_handle) or ""
    if not checkpoint_signature_is_complete(before_signature):
        return _execute_checkpointed_text_patch(
            hwp, candidate, history, request, unsafe_selectors, guard
        )
    before_snapshot = read_native_snapshot(candidate.window_handle)
    if before_snapshot is None:
        return _execute_checkpointed_text_patch(
            hwp, candidate, history, request, unsafe_selectors, guard
        )
    candidate_is_logical, original_text = _logical_patch_candidate(
        request,
        before_snapshot,
    )
    if not candidate_is_logical:
        return _execute_checkpointed_text_patch(
            hwp, candidate, history, request, unsafe_selectors, guard
        )
    try:
        before_structure = read_native_document_structure(candidate.window_handle)
    except HwpLiveError:
        return _execute_checkpointed_text_patch(
            hwp, candidate, history, request, unsafe_selectors, guard
        )
    if before_structure is None:
        # A soft read failure answers None, not an exception. Without a
        # readable "before" there is nothing to compare the after-structure
        # against, and `None == None` further down would read as "the structure
        # did not change" when in truth neither end was ever read. Nothing has
        # been written yet, so the checkpointed path is still free.
        return _execute_checkpointed_text_patch(
            hwp, candidate, history, request, unsafe_selectors, guard
        )

    try:
        result = patch_validated_text(
            hwp,
            candidate,
            request,
            unsafe_selectors,
            guard,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        if not native_failure_left_document_untouched(error):
            _rollback_failed_logical_patch(candidate, before_signature, error)
        raise

    after_signature = read_native_content_signature(candidate.window_handle) or ""
    try:
        after_structure = read_native_document_structure(candidate.window_handle)
    except HwpLiveError:
        after_structure = None
    selection = result.after.selection
    redo_selection = _redo_selection_for_patch(
        request,
        result.before,
        selection.start,
        original_text,
    )
    # None is "could not read", never "did not change". Folding an unreadable
    # after-structure into the equality below made a perfectly good edit look
    # structural, and the recovery for that is Undo followed by a full
    # re-apply -- a document write and a whole checkpoint pair spent on a
    # reading failure.
    structure_readable = after_structure is not None
    structure_unchanged = structure_readable and before_structure == after_structure
    logical_complete = (
        checkpoint_signature_is_complete(after_signature)
        and result.before.page_count == result.after.page_count
        and structure_unchanged
        and _single_paragraph_selection(selection)
        and redo_selection is not None
        and selection.start
        == _selection_for_text(selection.start, request.replacement).start
        and selection.end
        == _selection_for_text(selection.start, request.replacement).end
    )
    if not logical_complete:
        if not structure_readable:
            # The only thing missing is proof that this edit was purely
            # logical. Keep the edit, skip the undo entry it cannot justify,
            # and say which of the two is missing.
            return replace(
                result,
                notice=(
                    "편집은 적용하고 검증했습니다. 다만 편집 후 문서 구조를 읽지 못해 "
                    + "이 편집이 본문 안에서만 일어났는지 확인하지 못했고, 확인하지 "
                    + "못한 채로는 MCP 되돌리기 기록을 남기지 않았습니다. 되돌리려면 "
                    + "한/글에서 되돌리기(Ctrl+Z)를 쓰세요"
                ),
            )
        if checkpoint_signature_is_complete(after_signature):
            _rollback_logical_patch(candidate, before_signature, after_signature)
            return _execute_checkpointed_text_patch(
                hwp, candidate, history, request, unsafe_selectors, guard
            )
        return replace(
            result,
            notice=(
                "편집은 적용하고 검증했습니다. 다만 편집 후 문서 전체 지문을 읽지 "
                + "못해 MCP 되돌리기 기록을 남기지 않았습니다. 되돌리려면 한/글에서 "
                + "되돌리기(Ctrl+Z)를 쓰세요"
            ),
        )

    assert redo_selection is not None
    history.record(
        LogicalTextPatchHistoryEntry(
            document_id=candidate.document_id,
            full_name=candidate.full_name,
            operation="text.patch",
            original_text=original_text,
            replacement_text=request.replacement,
            undo_selection=selection,
            redo_selection=redo_selection,
            before_content_signature=before_signature,
            after_content_signature=after_signature,
            before_page_count=result.before.page_count,
            after_page_count=result.after.page_count,
            page=result.after.current_page,
            changed_pages=(result.after.current_page,),
        )
    )
    return result


def _execute_checkpointed_text_patch(
    hwp: LiveHwpApplication,
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    request: TextPatchRequest,
    unsafe_selectors: set[str],
    guard: Callable[[], None],
) -> TextPatchResult:
    unavailable = full_document_checkpoint_unavailable_reason(candidate)
    if unavailable:
        # No checkpoint may be taken: either two copies will not fit the
        # session's disk history, or 한/글 has already proved it cannot sign
        # this document. Patch without recording an MCP undo entry, so a later
        # hwp_undo falls through to 한/글's own history instead of to a
        # checkpoint that never existed.
        #
        # The notice is the whole point of this branch existing separately. This
        # used to return the patch result untouched, which meant a 331 MB
        # document reported a plain success and the missing undo entry was
        # discoverable only by trying to use it.
        return replace(
            patch_validated_text(
                hwp,
                candidate,
                request,
                unsafe_selectors,
                guard,
            ),
            notice=unavailable,
        )
    # A broken document identity leaves here as DocumentIdentityError and stops
    # the call before the edit: it is the one failure where going ahead would
    # write the user's work into a file that no longer exists.
    prepared = _prepare_text_patch_checkpoint(candidate, history)
    if isinstance(prepared, str):
        # The document could not be checkpointed - the classic case is a
        # picture-heavy document the bridge could not copy. Failing here would
        # cost the user their edit to protect an undo entry they never had.
        # Make the edit and say the undo entry is missing, in the words the
        # preparation step chose: it is the only place that saw the reason.
        return replace(
            patch_validated_text(
                hwp,
                candidate,
                request,
                unsafe_selectors,
                guard,
            ),
            notice=prepared,
        )
    try:
        result = patch_validated_text(
            hwp,
            candidate,
            request,
            unsafe_selectors,
            guard,
        )
        prepared = replace(
            prepared,
            p1_single_text_undo=_is_p1_single_text_undo(request, result),
        )
    except (HwpLiveError, OSError, ValueError) as error:
        # The edit itself failed, so the document goes back to the checkpoint
        # that was taken for exactly this -- unless the bridge proved there was
        # nothing to go back from.
        _restore_prepared_before_failure(candidate, prepared, error)
        raise
    try:
        _, _elapsed, after_evidence = _record_completed_edit(
            candidate,
            history,
            prepared,
            require_complete_signatures=True,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        # The edit is applied and verified; only the record of it failed. Undo
        # the record, not the edit: the checkpoint pair is dropped so no later
        # hwp_undo can restore half a history entry.
        _remove_path(prepared.before.path)
        _remove_path(prepared.after_path)
        # The after-capture can break the document's identity the same way the
        # before-capture can. Blocking is no longer available — the edit is
        # already in the document — so the answer says which file the session is
        # on now and what to do about it, instead of the stock missing-undo line.
        broken = _broken_document_identity(candidate, candidate.full_name, error)
        # The before-capture still happened, and if it moved the editing
        # session onto the copy that is true whether or not the record
        # survived. Report what was observed, not what was kept.
        return replace(
            result,
            notice=(
                _identity_notice_after_applied_edit(broken)
                if broken is not None
                else _CHECKPOINT_NOT_RECORDED_NOTICE + _capture_failure_clause(error)
            ),
            checkpoint_evidence=prepared.checkpoint_evidence,
        )
    evidence = prepared.checkpoint_evidence.merged(after_evidence)
    # A bridge that reports nothing leaves the result exactly as the patch
    # produced it. Silence is "could not tell", never a claim about the copy.
    if evidence == NO_CHECKPOINT_EVIDENCE:
        return result
    # The undo entry exists, but the bridge had to fall back to the legacy
    # in-memory capture to make it. That is a degradation the reader would
    # otherwise never see, so it rides out with the success.
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


def _successful_layout_write(result: OperationResult) -> bool:
    return result.status == "executed" and result.changed


def _layout_checkpoint_result(
    result: OperationResult,
    evidence: DocumentCheckpointEvidence = NO_CHECKPOINT_EVIDENCE,
    *,
    notice: str = "",
) -> OperationResult:
    if not notice and evidence.unavailable_reason:
        notice = (
            "편집을 적용하고 MCP 되돌리기 기록도 남겼습니다. 다만 문서 사본 방식 "
            + "체크포인트를 쓰지 못해 예전 방식으로 떴습니다."
            + _capture_reason_clause(evidence.unavailable_reason)
        )
    update: dict[str, object] = {}
    if notice:
        separator = " " if result.message.endswith((".", "!", "?")) else ". "
        suffix = separator + notice
        if len(suffix) >= 4_000:
            update["message"] = suffix[:4_000]
        else:
            update["message"] = result.message[: 4_000 - len(suffix)] + suffix
    if evidence.capture_method in _CAPTURE_METHODS:
        update["document_checkpoint_capture"] = evidence.capture_method
        update["document_identity_restored"] = evidence.identity_restored
    if not update:
        return result
    return result.model_copy(update=update)


@dataclass(slots=True)
class LayoutEditRecovery:
    """What the layout write could and did do about recovery.

    `execute_managed_layout_edit` leaves by two doors — a result and an
    exception — and the caller needs the same three facts on both. On the
    exception door there is no result to carry them, so the caller hands in one
    of these and reads it back in its `except` block.

    `restore_attempted` is the one that matters to a caller that reports
    rollback: False means no checkpoint existed, so no restore ever ran and
    nothing may claim the document was put back. It is never inferred from the
    document's state afterwards, because "the document looks unchanged" and "a
    rollback returned it" are different facts.
    """

    checkpoint_taken: bool = False
    restore_attempted: bool = False
    unavailable_reason: str = ""


def _unprotected_layout_write(
    edit: Callable[[], OperationResult],
    notice: str,
) -> OperationResult:
    """Run the write no checkpoint could cover, and say so in the answer.

    Refusing here is what b733c58 removed and must not come back: the edit is
    the work the caller asked for, and the undo entry is bookkeeping. What the
    caller loses is stated instead of hidden.
    """
    result = edit()
    if not _successful_layout_write(result):
        return result
    return _layout_checkpoint_result(result, notice=notice)


def execute_managed_layout_edit(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    operation: HistoryOperation,
    edit: Callable[[], OperationResult],
    *,
    require_undo_entry: bool = False,
    restore_on_exception: bool = False,
    recovery: LayoutEditRecovery | None = None,
) -> OperationResult:
    """Run one layout write under a pre-edit document checkpoint when it can.

    This is the `text.patch` contract (`_execute_checkpointed_text_patch`) with
    the same gate in front of it, and the two commits that emptied it are the
    reason it is written this way:

    * b733c58 removed the prepaid checkpoint because `require_undo_entry` had
      turned it into a *refusal* — a document that could not be checkpointed
      could not be edited at all. The checkpoint comes back; the refusal does
      not. `document_checkpoint_unavailable_reason` is asked first, and a
      non-empty answer means the write runs unprotected with that answer
      attached, never that the write is denied.
    * 5dd92ac then removed what was left, correctly: a copy captured *after*
      the write is the post-edit document, and recording it as the undo
      "before" cannot undo anything. That objection is answered by capturing
      before the write, which is what this does.

    `restore_on_exception` is live again: with a checkpoint in hand a failed
    write is put back before the exception escapes. `require_undo_entry` stays
    inert on purpose — its only 1.3.1 behaviour was the pre-write refusal above
    — and callers that need to know whether recovery was possible read it from
    `recovery` instead, which is a fact rather than a request.
    """
    _ = require_undo_entry
    unavailable = full_document_checkpoint_unavailable_reason(candidate)
    if unavailable:
        if recovery is not None:
            recovery.unavailable_reason = unavailable
        return _unprotected_layout_write(edit, unavailable)

    # A broken document identity leaves here as DocumentIdentityError and stops
    # the call before the write, exactly as the text patch path does.
    prepared = _prepare_document_edit_checkpoint(candidate, history, operation)
    if isinstance(prepared, str):
        if recovery is not None:
            recovery.unavailable_reason = prepared
        return _unprotected_layout_write(edit, prepared)
    if recovery is not None:
        recovery.checkpoint_taken = True

    try:
        result = edit()
    except Exception as error:
        if restore_on_exception:
            if recovery is not None:
                recovery.restore_attempted = True
            _restore_prepared_before_failure(candidate, prepared, error)
        else:
            _remove_path(prepared.before.path)
            _remove_path(prepared.after_path)
        raise
    if not _successful_layout_write(result):
        _remove_path(prepared.before.path)
        _remove_path(prepared.after_path)
        return result

    prepared = replace(prepared, changed_pages=result.changed_pages)
    try:
        _, _elapsed, after_evidence = _record_completed_edit(
            candidate,
            history,
            prepared,
            require_complete_signatures=True,
        )
    except (HwpLiveError, OSError, ValueError) as error:
        # The write is applied and verified; only the record of it failed. Drop
        # the pair so no later undo can restore half an entry, and keep the
        # edit: it is the work, the entry is the ledger.
        _remove_path(prepared.before.path)
        _remove_path(prepared.after_path)
        broken = _broken_document_identity(candidate, candidate.full_name, error)
        notice = (
            _identity_notice_after_applied_edit(broken)
            if broken is not None
            else _CHECKPOINT_NOT_RECORDED_NOTICE + _capture_failure_clause(error)
        )
        return _layout_checkpoint_result(
            result,
            prepared.checkpoint_evidence,
            notice=notice,
        )
    return _layout_checkpoint_result(
        result,
        prepared.checkpoint_evidence.merged(after_evidence),
    )


def page_text_signature(text: str) -> str:
    """Digest of one page's body text, for identifying body-only changes.

    The native document fingerprint is (page_count, control_count,
    control_hash). None of those move when body text alone changes, so a
    document that someone typed into after an MCP edit fingerprints identically
    to the state that edit left behind. This is the missing half of that
    identity, taken from a page inspection the delete path already performs.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_page_text_signature(
    candidate: WindowHandleCandidate,
    page: int | None,
) -> str:
    """Current body digest of `page`, or "" when it could not be read.

    "" is "unknown", never "unchanged". Callers that hold a recorded signature
    treat unknown as a mismatch, because an unreadable page cannot show that
    nobody else's work is about to be undone.
    """
    if page is None:
        return ""
    inspected = inspect_native_page(candidate.window_handle, page, include_cells=False)
    if inspected is None:
        return ""
    return page_text_signature(inspected.text)


def _body_matches(observed: str, recorded: str) -> bool:
    """Whether a current page body satisfies the claim an entry recorded."""
    return not recorded or observed == recorded


def _complete_content_matches(recorded: str, observed: str) -> bool:
    return (
        checkpoint_signature_is_complete(recorded)
        and checkpoint_signature_is_complete(observed)
        and checkpoint_signatures_match(recorded, observed)
    )


def _execute_authorized_native_history(
    window_handle: int,
    direction: HistoryDirection,
    steps: int,
    expected_content_signature: str,
) -> NativeHistoryResult:
    with native_history_content_precondition(expected_content_signature):
        return execute_native_history(window_handle, direction, steps)


def _result_content_signature(result: object) -> str:
    value = getattr(result, "after_content_signature", "")
    return value if isinstance(value, str) else ""


def history_state_matches(
    candidate: WindowHandleCandidate,
    *,
    page_count: int,
    page: int | None = None,
    controls: tuple[PageControlSnapshot, ...] | None = None,
    document_structure: NativeDocumentStructureSnapshot | None = None,
    observed_document_structure: NativeDocumentStructureSnapshot | None = None,
    text_signature: str = "",
    observed_text_signature: str = "",
    content_signature: str = "",
    observed_content_signature: str = "",
) -> bool:
    snapshot = read_native_snapshot(candidate.window_handle)
    if snapshot is None or snapshot.page_count != page_count:
        return False
    if content_signature:
        actual_content = (
            observed_content_signature
            or read_native_content_signature(candidate.window_handle)
            or ""
        )
        if not _complete_content_matches(content_signature, actual_content):
            return False
    if document_structure is not None:
        actual_structure = (
            observed_document_structure
            if observed_document_structure is not None
            else read_native_document_structure(candidate.window_handle)
        )
        if actual_structure != document_structure:
            return False
        if not text_signature:
            # Nothing was recorded for this entry, so there is no body claim to
            # check. Entries written before the body digest existed land here
            # and keep their previous structure-only behaviour.
            return True
        actual_text = observed_text_signature or _read_page_text_signature(
            candidate,
            page,
        )
        return actual_text == text_signature
    if page is None or controls is None:
        return True
    inspected = inspect_native_page(candidate.window_handle, page, include_cells=True)
    if inspected is None:
        return False
    actual = _control_state(inspected.controls)
    return Counter(item.signature for item in actual) == Counter(
        item.signature for item in controls
    )


def execute_native_history_until_state(
    candidate: WindowHandleCandidate,
    direction: HistoryDirection,
    maximum_steps: int,
    *,
    page_count: int,
    page: int | None = None,
    controls: tuple[PageControlSnapshot, ...] | None = None,
    document_structure: NativeDocumentStructureSnapshot | None = None,
    minimum_steps: int = 0,
    origin_page_count: int | None = None,
    origin_controls: tuple[PageControlSnapshot, ...] | None = None,
    origin_document_structure: NativeDocumentStructureSnapshot | None = None,
    text_signature: str = "",
    origin_text_signature: str = "",
    content_signature: str = "",
    origin_content_signature: str = "",
) -> tuple[int, int]:
    if any(
        signature and not checkpoint_signature_is_complete(signature)
        for signature in (content_signature, origin_content_signature)
    ):
        raise HwpLiveError(
            "문서 전체 본문 지문이 완전하지 않아 한컴 실행 이력을 실행하지 "
            + "않았습니다",
            mutation_started=False,
        )
    current_content_signature = origin_content_signature
    if content_signature and not current_content_signature:
        current_content_signature = (
            read_native_content_signature(candidate.window_handle) or ""
        )
    if content_signature and not checkpoint_signature_is_complete(
        current_content_signature
    ):
        raise HwpLiveError(
            "현재 문서 전체 본문 지문이 완전하지 않아 한컴 실행 이력을 "
            + "실행하지 않았습니다",
            mutation_started=False,
        )
    if minimum_steps == 0 and history_state_matches(
        candidate,
        page_count=page_count,
        page=page,
        controls=controls,
        document_structure=document_structure,
        text_signature=text_signature,
        content_signature=content_signature,
    ):
        return 0, 0
    elapsed = 0
    applied = 0
    for step in range(1, maximum_steps + 1):
        try:
            result = (
                _execute_authorized_native_history(
                    candidate.window_handle,
                    direction,
                    1,
                    current_content_signature,
                )
                if current_content_signature
                else execute_native_history(
                    candidate.window_handle,
                    direction,
                    1,
                )
            )
        except HwpLiveError as error:
            raise _native_history_walk_failure(
                candidate,
                direction,
                applied,
                f"한컴 {direction} 호출이 {applied}단계 뒤 실패했습니다",
                origin_page_count=origin_page_count,
                page=page,
                origin_controls=origin_controls,
                origin_document_structure=origin_document_structure,
                origin_text_signature=origin_text_signature,
                origin_content_signature=origin_content_signature,
                current_content_signature=current_content_signature,
            ) from error
        elapsed += result.elapsed_microseconds
        if result.applied == 0:
            raise _native_history_walk_failure(
                candidate,
                direction,
                applied,
                f"한컴 {direction} 이력이 {applied}단계에서 끝나 "
                + "MCP 작업 경계 상태에 도달하지 못했습니다",
                origin_page_count=origin_page_count,
                page=page,
                origin_controls=origin_controls,
                origin_document_structure=origin_document_structure,
                origin_text_signature=origin_text_signature,
                origin_content_signature=origin_content_signature,
                current_content_signature=current_content_signature,
            )
        applied += 1
        try:
            observed_content_signature = ""
            if current_content_signature:
                observed_content_signature = (
                    _result_content_signature(result)
                    or read_native_content_signature(candidate.window_handle)
                    or ""
                )
                if not checkpoint_signature_is_complete(observed_content_signature):
                    raise HwpLiveError(
                        "한컴 실행 이력 직후 문서 지문을 완전하게 읽지 못했습니다"
                    )
                current_content_signature = observed_content_signature
            observed_structure = getattr(result, "after_structure", None)
            if document_structure is not None and observed_structure is None:
                observed_structure = read_native_document_structure(
                    candidate.window_handle
                )
                if observed_structure is None:
                    raise HwpLiveError("MCP 작업 경계 상태 지문을 읽지 못했습니다")
            matches = history_state_matches(
                candidate,
                page_count=page_count,
                page=page,
                controls=controls,
                document_structure=document_structure,
                observed_document_structure=observed_structure,
                text_signature=text_signature,
                content_signature=content_signature,
                observed_content_signature=observed_content_signature,
            )
        except HwpLiveError as error:
            raise _native_history_walk_failure(
                candidate,
                direction,
                applied,
                "MCP 작업 경계 상태 지문을 읽지 못했습니다",
                origin_page_count=origin_page_count,
                page=page,
                origin_controls=origin_controls,
                origin_document_structure=origin_document_structure,
                origin_text_signature=origin_text_signature,
                origin_content_signature=origin_content_signature,
                current_content_signature=current_content_signature,
            ) from error
        if step >= minimum_steps and matches:
            return step, elapsed
    raise _native_history_walk_failure(
        candidate,
        direction,
        applied,
        f"한컴 {direction}를 안전 예산 {maximum_steps}단계까지 실행했지만 "
        + "MCP 작업 경계 상태에 도달하지 못했습니다",
        origin_page_count=origin_page_count,
        page=page,
        origin_controls=origin_controls,
        origin_document_structure=origin_document_structure,
        origin_text_signature=origin_text_signature,
        origin_content_signature=origin_content_signature,
        current_content_signature=current_content_signature,
    )


def _native_history_walk_failure(
    candidate: WindowHandleCandidate,
    direction: HistoryDirection,
    walked: int,
    reason: str,
    *,
    origin_page_count: int | None,
    page: int | None,
    origin_controls: tuple[PageControlSnapshot, ...] | None,
    origin_document_structure: NativeDocumentStructureSnapshot | None,
    origin_text_signature: str = "",
    origin_content_signature: str = "",
    current_content_signature: str = "",
) -> HwpLiveError:
    if walked == 0:
        # Nothing was walked, so nothing was applied to the document. The
        # sentence already says so; `mutation_started=False` is the same fact
        # in the field the envelope reads, which would otherwise promote this
        # unchanged document to a partial change.
        return HwpLiveError(
            f"{reason}. 이 호출로 문서는 바뀌지 않았습니다",
            mutation_started=False,
        )
    reverse: HistoryDirection = "redo" if direction == "undo" else "undo"
    restored = 0
    observed_structure = None
    for _ in range(walked):
        try:
            result = (
                _execute_authorized_native_history(
                    candidate.window_handle,
                    reverse,
                    1,
                    current_content_signature,
                )
                if current_content_signature
                else execute_native_history(
                    candidate.window_handle,
                    reverse,
                    1,
                )
            )
        except HwpLiveError:
            break
        if result.applied == 0:
            break
        restored += 1
        observed_structure = getattr(result, "after_structure", None)
        if current_content_signature:
            next_signature = (
                _result_content_signature(result)
                or read_native_content_signature(candidate.window_handle)
                or ""
            )
            if not checkpoint_signature_is_complete(next_signature):
                break
            current_content_signature = next_signature
    outstanding = walked - restored
    if outstanding:
        return HwpLiveError(
            f"{reason}. 되감은 {walked}단계 중 {restored}단계만 {reverse}로 "
            + f"복구되어 한컴 {direction} {outstanding}단계가 문서에 남아 있습니다. "
            + f"한/글에서 {reverse} {outstanding}단계를 실행하면 호출 전 상태가 됩니다"
        )
    if origin_page_count is None:
        return HwpLiveError(
            f"{reason}. 되감은 {walked}단계를 모두 {reverse}로 복구했지만 "
            + "호출 전 상태 지문이 없어 문서가 같은지는 확인하지 못했습니다"
        )
    try:
        confirmed = history_state_matches(
            candidate,
            page_count=origin_page_count,
            page=page,
            controls=origin_controls,
            document_structure=origin_document_structure,
            observed_document_structure=observed_structure,
            text_signature=origin_text_signature,
            content_signature=origin_content_signature,
        )
    except HwpLiveError:
        confirmed = False
    if confirmed:
        return HwpLiveError(
            f"{reason}. 되감은 {walked}단계를 모두 {reverse}로 복구하고 "
            + "문서가 호출 전 상태로 돌아온 것을 확인했습니다"
        )
    return HwpLiveError(
        f"{reason}. 되감은 {walked}단계를 모두 {reverse}로 복구했지만 "
        + "문서가 호출 전 상태와 같은지는 확인하지 못했습니다"
    )


def _execute_native_history_until_signature(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    document_id: int,
    full_name: str,
    direction: HistoryDirection,
    target_signature: str,
    maximum_steps: int,
    *,
    origin_signature: str = "",
) -> NativeHistoryWalk:
    """Legacy low-level history walk retained for reversal diagnostics.

    The public checkpoint path deliberately does not call this helper. A live
    state different from both recorded endpoints may contain another editor's
    work, so walking from it is not authorized even when the opposite direction
    could usually put the steps back. Direct tests keep the helper's partial
    reversal accounting honest for any already-started internal walk.

    `origin_signature` is the content signature the caller already probed to
    establish the initial state. It is what the restore below is checked
    against, so verifying costs no extra capture on entry.
    """
    elapsed = 0
    applied = 0
    exhausted = False
    authorization_signature = (
        origin_signature if checkpoint_signature_is_complete(origin_signature) else ""
    )
    for _ in range(maximum_steps):
        result = (
            _execute_authorized_native_history(
                candidate.window_handle,
                direction,
                1,
                authorization_signature,
            )
            if authorization_signature
            else execute_native_history(
                candidate.window_handle,
                direction,
                1,
            )
        )
        elapsed += result.elapsed_microseconds
        if result.applied == 0:
            exhausted = True
            break
        applied += 1
        observed_signature = _result_content_signature(
            result
        ) or _probe_content_signature(
            candidate,
            history,
            document_id,
            full_name,
        )
        if checkpoint_signature_is_complete(observed_signature):
            authorization_signature = observed_signature
        if checkpoint_signatures_match(
            target_signature,
            observed_signature,
        ):
            return NativeHistoryWalk(
                reached=True,
                steps_walked=applied,
                steps_restored=0,
                elapsed_microseconds=elapsed,
                restore_confirmed=None,
                notice="",
            )
    restored = 0
    confirmed: bool | None = True
    if applied:
        # Put the document back where this call found it. Every step was one of
        # the engine's own, so this is exact rather than best effort — but
        # "exact in principle" is not evidence. Read how many steps actually
        # went back, then check the document itself.
        reverse: HistoryDirection = "redo" if direction == "undo" else "undo"
        reversal = (
            _execute_authorized_native_history(
                candidate.window_handle,
                reverse,
                applied,
                authorization_signature,
            )
            if authorization_signature
            else execute_native_history(
                candidate.window_handle,
                reverse,
                applied,
            )
        )
        elapsed += reversal.elapsed_microseconds
        restored = min(applied, reversal.applied)
        if restored != applied:
            # The engine put back fewer steps than it took. Whatever a signature
            # would say, history steps are still applied and the document is not
            # back — so do not spend a whole-document capture asking.
            confirmed = False
        elif not origin_signature:
            # Nothing to compare against. The step counts balanced, which is
            # weaker evidence than content — "not confirmed", never "restored".
            confirmed = None
        else:
            final = _probe_content_signature(
                candidate,
                history,
                document_id,
                full_name,
            )
            confirmed = (
                checkpoint_signatures_match(origin_signature, final) if final else None
            )
    return NativeHistoryWalk(
        reached=False,
        steps_walked=applied,
        steps_restored=restored,
        elapsed_microseconds=elapsed,
        restore_confirmed=confirmed,
        notice=_drift_walk_notice(
            direction=direction,
            walked=applied,
            restored=restored,
            exhausted=exhausted,
            confirmed=confirmed,
        ),
    )


def _drift_walk_notice(
    *,
    direction: HistoryDirection,
    walked: int,
    restored: int,
    exhausted: bool,
    confirmed: bool | None,
) -> str:
    """What to tell the user when the checkpoint boundary was not reached.

    Never says "되돌렸습니다" unless the document was actually observed back at
    its starting content. When it was not, this says how many 한/글 history
    steps are still applied so the state can be undone by hand.
    """
    reverse = "다시 실행" if direction == "undo" else "되돌리기"
    head = (
        "MCP 편집 이후 문서가 바뀌어 문서 체크포인트를 덮어쓰지 않았습니다"
        "(사용자가 직접 입력한 내용을 지우지 않으려는 것입니다). "
    )
    if walked == 0:
        return head + (
            f"한컴 {direction} 이력이 비어 있어 편집 직전 내용으로 되감을 수 "
            "없었습니다. 이 호출로 문서는 바뀌지 않았습니다"
        )
    reason = (
        f"한컴 {direction} 이력이 {walked}단계에서 끝나"
        if exhausted
        else f"한컴 {direction} 이력을 {walked}단계 실행했지만"
    )
    outstanding = walked - restored
    if outstanding:
        return head + (
            f"{reason} 편집 직전 내용에 도달하지 못했습니다. "
            f"되감은 {walked}단계 중 {restored}단계만 복구되어 한컴 {direction} "
            f"{outstanding}단계가 문서에 그대로 남아 있습니다. 한/글에서 "
            f"{reverse} {outstanding}단계를 실행하면 호출 전 상태가 됩니다"
        )
    if confirmed is True:
        return head + (
            f"{reason} 편집 직전 내용에 도달하지 못했습니다. 되감은 {walked}단계를 "
            "모두 복구해 문서를 호출 전 상태로 되돌려 두었고, 문서 내용 서명으로 "
            "확인했습니다. 이 호출로 문서 내용은 바뀌지 않았습니다"
        )
    if confirmed is None:
        return head + (
            f"{reason} 편집 직전 내용에 도달하지 못했습니다. 되감은 {walked}단계를 "
            "모두 복구했지만 문서 내용 서명을 읽지 못해 호출 전 상태와 같은지는 "
            "확인하지 못했습니다. 문서를 눈으로 확인하세요"
        )
    return head + (
        f"{reason} 편집 직전 내용에 도달하지 못했습니다. 되감은 {walked}단계를 "
        "모두 복구했는데도 문서 내용 서명이 호출 전과 다릅니다. 문서가 중간 "
        "상태일 수 있으니 한/글에서 직접 확인하세요"
    )


_DELETION_HISTORY_NOTICE = (
    "삭제는 완료했지만 안전한 전후 문서 지문을 모두 읽지 못해 "
    "MCP 되돌리기 기록은 남기지 않았습니다"
)


def _inspect_page_for_control_deletion(
    window_handle: int,
    page: int,
) -> NativePageInspection | None:
    try:
        return inspect_native_page(window_handle, page, include_cells=False)
    except HwpLiveError:
        return None


def execute_grouped_native_control_deletion(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    document_id: int,
    full_name: str,
    page: int,
    page_count: int,
    target_controls: tuple[NativePageControl, ...],
) -> LiveEditHistoryExecution:
    commands = build_delete_control_commands(target_controls)
    inspected_before = _inspect_page_for_control_deletion(
        candidate.window_handle,
        page,
    )
    if inspected_before is None:
        native = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(document_id, full_name, commands),
            minimum_version=9,
        )
        if native is None:
            raise HwpLiveError("한컴 프로토콜 9 기존 개체 삭제기를 사용할 수 없습니다")
        return LiveEditHistoryExecution(
            native.commands_executed,
            native.elapsed_microseconds,
            False,
            notice=_DELETION_HISTORY_NOTICE,
        )
    before_controls = _control_state(inspected_before.controls)
    # Free: this inspection already happened for the control state. The native
    # document fingerprint carries no body text, so without this a later
    # body-only edit on this page is indistinguishable from the state the
    # deletion left behind.
    before_page_text = page_text_signature(inspected_before.text)
    before_content_signature = (
        peek_cached_content_signature(candidate.window_handle) or ""
    )
    try:
        before_document_structure = read_native_document_structure(
            candidate.window_handle
        )
    except HwpLiveError:
        before_document_structure = None
    native_elapsed = 0
    commands_executed = 0
    history_safe = False
    after_document_structure = None
    try:
        native = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(document_id, full_name, commands),
            minimum_version=9,
        )
        if native is None:
            raise HwpLiveError("한컴 프로토콜 9 기존 개체 삭제기를 사용할 수 없습니다")
        commands_executed = native.commands_executed
        native_elapsed = native.elapsed_microseconds
        snapshot_after = read_native_snapshot(candidate.window_handle)
        inspected_after = _inspect_page_for_control_deletion(
            candidate.window_handle,
            page,
        )
        if snapshot_after is None:
            raise HwpLiveError("개체 삭제 후 문서 구조를 읽지 못했습니다")
        if inspected_after is None:
            return LiveEditHistoryExecution(
                commands_executed,
                native_elapsed,
                False,
                notice=_DELETION_HISTORY_NOTICE,
            )
        try:
            after_document_structure = read_native_document_structure(
                candidate.window_handle
            )
        except HwpLiveError:
            after_document_structure = None
        # Without a complete before-signature the entry below is never
        # recorded, so the after-signature answers nothing. On a document the
        # engine refuses to serialise, that unread answer is the whole
        # serialisation attempt (~17s measured on a 317MB document) paid twice
        # per deletion for a verdict already known to be "not recordable".
        after_content_signature = (
            (read_native_content_signature(candidate.window_handle) or "")
            if checkpoint_signature_is_complete(before_content_signature)
            else ""
        )
        entry = NativeDocumentEditHistoryEntry(
            document_id=document_id,
            full_name=full_name,
            operation="control.delete",
            # This is a safety/cost ceiling, not a claim that one DeleteCtrl is
            # one 한/글 history step. The undo path stops at the measured
            # document boundary and reports how many steps it actually took.
            maximum_native_steps=MAX_NATIVE_HISTORY_STEPS,
            before_page_count=page_count,
            after_page_count=snapshot_after.page_count,
            page=page,
            before_controls=before_controls,
            after_controls=_control_state(inspected_after.controls),
            before_document_structure=before_document_structure,
            after_document_structure=after_document_structure,
            before_page_text_signature=before_page_text,
            after_page_text_signature=page_text_signature(inspected_after.text),
            before_content_signature=before_content_signature,
            after_content_signature=after_content_signature,
        )
        history_safe = (
            before_document_structure is not None
            and after_document_structure is not None
            and checkpoint_signature_is_complete(before_content_signature)
            and checkpoint_signature_is_complete(after_content_signature)
        )
        if history_safe:
            history.record(entry)
    except (HwpLiveError, OSError, ValueError):
        if commands_executed:
            try:
                _ = execute_native_history_until_state(
                    candidate,
                    "undo",
                    MAX_NATIVE_HISTORY_STEPS,
                    page_count=page_count,
                    page=page,
                    controls=before_controls,
                    document_structure=before_document_structure,
                    minimum_steps=1,
                    text_signature=before_page_text,
                    content_signature=before_content_signature,
                )
            except HwpLiveError as restore_error:
                raise HwpLiveError(
                    "대용량 문서 개체 삭제 실패 후 한컴 이력 복구도 검증하지 못했습니다"
                ) from restore_error
        raise
    notice = "" if history_safe else _DELETION_HISTORY_NOTICE
    return LiveEditHistoryExecution(
        commands_executed,
        native_elapsed,
        False,
        notice=notice,
    )


def execute_grouped_native_page_deletion(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    document_id: int,
    full_name: str,
    page: int,
    page_count: int,
    commands: tuple[NativeActionCommand, ...],
) -> LiveEditHistoryExecution:
    native_elapsed = 0
    commands_executed = 0
    before_content_signature = (
        peek_cached_content_signature(candidate.window_handle) or ""
    )
    try:
        native = execute_native_actions(
            candidate.window_handle,
            NativeActionRequest(document_id, full_name, commands),
            minimum_version=9,
        )
        if native is None:
            raise HwpLiveError("한컴 프로토콜 9 쪽 삭제기를 사용할 수 없습니다")
        commands_executed = native.commands_executed
        native_elapsed = native.elapsed_microseconds
        snapshot_after = read_native_snapshot(candidate.window_handle)
        if snapshot_after is None or snapshot_after.page_count != page_count - 1:
            raise HwpLiveError("쪽 삭제 후 페이지 수가 정확히 하나 줄지 않았습니다")
        # Same shape as the control-deletion path above: an incomplete
        # before-signature already decides history_safe, so the expensive
        # after-read is skipped rather than paid for an unread answer.
        after_content_signature = (
            (read_native_content_signature(candidate.window_handle) or "")
            if checkpoint_signature_is_complete(before_content_signature)
            else ""
        )
        history_safe = checkpoint_signature_is_complete(
            before_content_signature
        ) and checkpoint_signature_is_complete(after_content_signature)
        if history_safe:
            history.record(
                NativeDocumentEditHistoryEntry(
                    document_id=document_id,
                    full_name=full_name,
                    operation="document.delete_page",
                    maximum_native_steps=len(commands),
                    before_page_count=page_count,
                    after_page_count=snapshot_after.page_count,
                    page=page,
                    before_content_signature=before_content_signature,
                    after_content_signature=after_content_signature,
                )
            )
    except (HwpLiveError, OSError, ValueError):
        if commands_executed:
            try:
                _ = execute_native_history_until_state(
                    candidate,
                    "undo",
                    commands_executed,
                    page_count=page_count,
                    content_signature=before_content_signature,
                )
            except HwpLiveError as restore_error:
                raise HwpLiveError(
                    "대용량 문서 쪽 삭제 실패 후 한컴 이력 복구도 검증하지 못했습니다"
                ) from restore_error
        raise
    notice = (
        ""
        if history_safe
        else "쪽 삭제는 완료했지만 안전한 전후 문서 지문을 모두 읽지 못해 "
        + "MCP 되돌리기 기록은 남기지 않았습니다"
    )
    return LiveEditHistoryExecution(
        commands_executed,
        native_elapsed,
        False,
        notice=notice,
    )


def verify_history_entry_state(
    candidate: HwpDocumentCandidate,
    entry: LiveEditHistoryEntry,
    direction: HistoryDirection,
) -> None:
    if isinstance(
        entry,
        (
            NativeDocumentEditHistoryEntry,
            LogicalTextPatchHistoryEntry,
            LogicalTextPatchBatchHistoryEntry,
        ),
    ):
        page_count = (
            entry.before_page_count if direction == "undo" else entry.after_page_count
        )
    else:
        checkpoint = entry.before if direction == "undo" else entry.after
        page_count = checkpoint.page_count
    controls = None
    if entry.operation == "control.delete":
        controls = (
            entry.before_controls if direction == "undo" else entry.after_controls
        )
    if not history_state_matches(
        candidate,
        page_count=page_count,
        page=entry.page,
        controls=controls,
        document_structure=(
            entry.before_document_structure
            if direction == "undo"
            else entry.after_document_structure
        )
        if isinstance(entry, NativeDocumentEditHistoryEntry)
        else None,
        text_signature=(
            entry.before_page_text_signature
            if direction == "undo"
            else entry.after_page_text_signature
        )
        if isinstance(entry, NativeDocumentEditHistoryEntry)
        else "",
    ):
        raise HwpLiveError(
            "복구한 문서의 페이지 수 또는 대상 개체 구조가 이력과 다릅니다"
        )


def _execute_logical_text_history(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    entry: LogicalTextPatchHistoryEntry,
    direction: HistoryDirection,
) -> tuple[int, int, str]:
    target_signature = (
        entry.before_content_signature
        if direction == "undo"
        else entry.after_content_signature
    )
    origin_signature = (
        entry.after_content_signature
        if direction == "undo"
        else entry.before_content_signature
    )
    selection = entry.undo_selection if direction == "undo" else entry.redo_selection
    expected_text = (
        entry.replacement_text if direction == "undo" else entry.original_text
    )
    replacement = entry.original_text if direction == "undo" else entry.replacement_text
    target_page_count = (
        entry.before_page_count if direction == "undo" else entry.after_page_count
    )
    current_signature = _probe_content_signature(
        candidate,
        history,
        entry.document_id,
        entry.full_name,
    )
    if not checkpoint_signature_is_complete(current_signature):
        raise HwpLiveError(
            "현재 문서 전체 내용 지문을 읽지 못해 논리 text.patch 이력을 "
            + "적용하지 않았습니다",
            mutation_started=False,
        )
    if _complete_content_matches(target_signature, current_signature):
        return (
            0,
            0,
            "문서가 이미 MCP 작업 경계 상태여서 논리 역패치를 적용하지 않았습니다",
        )
    if not _complete_content_matches(origin_signature, current_signature):
        raise HwpLiveError(
            "현재 문서가 text.patch 편집 전과 후 어느 전체 내용 지문과도 일치하지 "
            + "않아 후속 편집을 관통하지 않도록 문서를 바꾸지 않았습니다",
            mutation_started=False,
        )
    native = execute_native_actions(
        candidate.window_handle,
        NativeActionRequest(
            entry.document_id,
            entry.full_name,
            (
                TextPatchCommand(
                    target="range",
                    expected_text=expected_text,
                    replacement=replacement,
                    start=selection.start,
                    end=selection.end,
                ),
            ),
        ),
        minimum_version=13,
    )
    if native is None:
        raise HwpLiveError("논리 text.patch 이력 적용기를 사용할 수 없습니다")
    snapshot = read_native_snapshot(candidate.window_handle)
    restored_signature = read_native_content_signature(candidate.window_handle) or ""
    expected_key = entry.document_id, os.path.normcase(os.path.abspath(entry.full_name))
    actual_key = (
        (snapshot.document_id, os.path.normcase(os.path.abspath(snapshot.full_name)))
        if snapshot is not None
        else None
    )
    if (
        snapshot is None
        or actual_key != expected_key
        or snapshot.page_count != target_page_count
        or not _complete_content_matches(target_signature, restored_signature)
    ):
        raise HwpLiveError(
            "논리 text.patch 이력을 적용했지만 목표 전체 내용 지문 또는 문서 "
            + "식별값을 확인하지 못했습니다",
            mutation_started=True,
            safe_to_repeat=False,
        )
    if replacement:
        expected_selection = _selection_for_text(selection.start, replacement)
        if (
            not snapshot.selection.selected
            or snapshot.selection.start != expected_selection.start
            or snapshot.selection.end != expected_selection.end
            or snapshot.selected_text.replace("\r\n", "\n").replace("\r", "\n")
            != replacement.replace("\r\n", "\n").replace("\r", "\n")
        ):
            raise HwpLiveError(
                "논리 text.patch 이력 적용 뒤 복원한 범위를 확인하지 못했습니다",
                mutation_started=True,
                safe_to_repeat=False,
            )
    elif snapshot.selection.selected:
        raise HwpLiveError(
            "논리 text.patch 삭제 뒤 선택 영역이 예상대로 접히지 않았습니다",
            mutation_started=True,
            safe_to_repeat=False,
        )
    return (
        native.commands_executed,
        native.elapsed_microseconds,
        "전체 내용 지문을 확인하고 논리 역패치로 MCP 작업 경계 상태를 복원했습니다",
    )


def _execute_logical_text_batch_history(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    entry: LogicalTextPatchBatchHistoryEntry,
    direction: HistoryDirection,
) -> tuple[int, int, str]:
    target_signature = (
        entry.before_content_signature
        if direction == "undo"
        else entry.after_content_signature
    )
    origin_signature = (
        entry.after_content_signature
        if direction == "undo"
        else entry.before_content_signature
    )
    current_signature = _probe_content_signature(
        candidate, history, entry.document_id, entry.full_name
    )
    if not checkpoint_signature_is_complete(current_signature):
        raise HwpLiveError(
            "현재 문서 전체 내용 지문을 읽지 못해 논리 text.patch batch 이력을 적용하지 않았습니다",
            mutation_started=False,
        )
    if _complete_content_matches(target_signature, current_signature):
        return 0, 0, "문서가 이미 MCP batch 작업 경계 상태입니다"
    if not _complete_content_matches(origin_signature, current_signature):
        raise HwpLiveError(
            "현재 문서가 text.patch batch 편집 전후 지문과 일치하지 않습니다",
            mutation_started=False,
        )
    commands = entry.inverse_commands if direction == "undo" else entry.forward_commands
    native = execute_native_actions(
        candidate.window_handle,
        NativeActionRequest(entry.document_id, entry.full_name, commands),
        minimum_version=14,
    )
    snapshot = read_native_snapshot(candidate.window_handle)
    restored_signature = read_native_content_signature(candidate.window_handle) or ""
    expected_pages = (
        entry.before_page_count if direction == "undo" else entry.after_page_count
    )
    if (
        native is None
        or snapshot is None
        or snapshot.document_id != entry.document_id
        or os.path.normcase(os.path.abspath(snapshot.full_name))
        != os.path.normcase(os.path.abspath(entry.full_name))
        or snapshot.page_count != expected_pages
        or not _complete_content_matches(target_signature, restored_signature)
    ):
        raise HwpLiveError(
            "논리 text.patch batch 이력을 적용했지만 목표 상태를 확인하지 못했습니다",
            mutation_started=True,
            safe_to_repeat=False,
        )
    return (
        native.commands_executed,
        native.elapsed_microseconds,
        "논리 역패치 batch로 MCP 작업 경계 상태를 복원했습니다",
    )


def execute_document_edit_history(
    candidate: HwpDocumentCandidate,
    history: LiveEditHistoryStore,
    document_id: int,
    full_name: str,
    direction: HistoryDirection,
    steps: int,
) -> LiveEditHistoryExecution | None:
    available = history.available(direction, document_id, full_name)
    if available == 0:
        return None
    if steps > available:
        raise HwpLiveError(
            f"현재 문서의 MCP 편집 {direction} 이력은 {available}단계입니다; "
            + "기본 한컴 이력과 한 호출에서 혼합 실행할 수 없습니다",
            mutation_started=False,
        )
    commands = 0
    elapsed = 0
    checkpoint_history = True
    measurements: list[str] = []
    changed_pages: set[int] = set()
    for _ in range(steps):
        entry = history.peek(direction, document_id, full_name)
        if entry is None:
            raise HwpLiveError("문서 편집 이력이 실행 중 소진되었습니다")
        changed_pages.update(getattr(entry, "changed_pages", ()))
        entry_used_engine_history = False
        if isinstance(entry, LogicalTextPatchHistoryEntry):
            checkpoint_history = False
            executed, duration, measured = _execute_logical_text_history(
                candidate,
                history,
                entry,
                direction,
            )
        elif isinstance(entry, LogicalTextPatchBatchHistoryEntry):
            checkpoint_history = False
            executed, duration, measured = _execute_logical_text_batch_history(
                candidate,
                history,
                entry,
                direction,
            )
        elif isinstance(entry, NativeDocumentEditHistoryEntry):
            checkpoint_history = False
            target_page_count = (
                entry.before_page_count
                if direction == "undo"
                else entry.after_page_count
            )
            target_controls = None
            origin_controls = None
            if entry.operation == "control.delete":
                target_controls = (
                    entry.before_controls
                    if direction == "undo"
                    else entry.after_controls
                )
                origin_controls = (
                    entry.after_controls
                    if direction == "undo"
                    else entry.before_controls
                )
            target_document_structure = (
                entry.before_document_structure
                if direction == "undo"
                else entry.after_document_structure
            )
            origin_document_structure = (
                entry.after_document_structure
                if direction == "undo"
                else entry.before_document_structure
            )
            target_page_text_signature = (
                entry.before_page_text_signature
                if direction == "undo"
                else entry.after_page_text_signature
            )
            origin_page_text_signature = (
                entry.after_page_text_signature
                if direction == "undo"
                else entry.before_page_text_signature
            )
            target_content_signature = (
                entry.before_content_signature
                if direction == "undo"
                else entry.after_content_signature
            )
            origin_content_signature = (
                entry.after_content_signature
                if direction == "undo"
                else entry.before_content_signature
            )
            if not checkpoint_signature_is_complete(
                entry.before_content_signature
            ) or not checkpoint_signature_is_complete(entry.after_content_signature):
                raise HwpLiveError(
                    "삭제 전후 문서 전체 본문 지문이 완전하지 않아 한컴 실행 이력 "
                    + "경계를 안전하게 확인할 수 없습니다. 남의 편집을 지우지 않도록 "
                    + "문서를 바꾸지 않았습니다",
                    mutation_started=False,
                )
            origin_page_count = (
                entry.after_page_count
                if direction == "undo"
                else entry.before_page_count
            )
            minimum_steps = 0
            already_at_target = False
            if entry.operation == "control.delete":
                try:
                    current_structure = read_native_document_structure(
                        candidate.window_handle
                    )
                except HwpLiveError:
                    current_structure = None
                if entry.before_document_structure is None:
                    raise HwpLiveError(
                        "삭제 전 문서 전체 개체 지문을 읽지 못해 이 삭제의 한컴 실행 "
                        + "이력 경계를 안전하게 확인할 수 없습니다. 남의 편집을 지우지 "
                        + "않도록 문서를 바꾸지 않았습니다. 한/글에서 삭제 뒤 후속 편집이 "
                        + "없는지 직접 확인한 후 실행 취소를 사용하세요",
                        mutation_started=False,
                    )
                if entry.after_document_structure is None:
                    raise HwpLiveError(
                        "삭제 후 문서 전체 개체 지문을 읽지 못해 이 삭제의 한컴 실행 "
                        + "이력 경계를 안전하게 확인할 수 없습니다. 남의 편집을 지우지 "
                        + "않도록 문서를 바꾸지 않았습니다. 한/글에서 삭제 뒤 후속 편집이 "
                        + "없는지 직접 확인한 후 실행 취소를 사용하세요",
                        mutation_started=False,
                    )
                if current_structure is None:
                    raise HwpLiveError(
                        "현재 문서 전체 개체 지문을 읽지 못해 삭제 전후 상태를 확인할 "
                        + "수 없습니다. 남의 편집을 지우지 않도록 문서를 바꾸지 "
                        + "않았습니다. 문서 상태를 직접 확인한 뒤 다시 요청하세요",
                        mutation_started=False,
                    )
                # The native fingerprint is (page_count, control_count,
                # control_hash) and none of those move when body text alone
                # changes. Without the recorded page digest, a document someone
                # typed into after the deletion fingerprints exactly like the
                # state the deletion left behind, and the walk below would run
                # straight through that edit. Read it before deciding anything.
                recorded_page_text = (
                    entry.before_page_text_signature or entry.after_page_text_signature
                )
                current_page_text = (
                    _read_page_text_signature(candidate, entry.page)
                    if recorded_page_text
                    else ""
                )
                recorded_content = (
                    entry.before_content_signature or entry.after_content_signature
                )
                current_content = (
                    read_native_content_signature(candidate.window_handle) or ""
                    if recorded_content
                    else ""
                )
                if recorded_page_text and not current_page_text:
                    raise HwpLiveError(
                        "현재 대상 쪽 본문 지문을 읽지 못해 삭제 뒤 후속 본문 편집이 "
                        + "있었는지 확인할 수 없습니다. 남의 편집을 지우지 않도록 "
                        + "문서를 바꾸지 않았습니다. 문서 상태를 직접 확인한 뒤 다시 "
                        + "요청하세요",
                        mutation_started=False,
                    )
                # An unreadable current signature is not a mismatch. Falling
                # through would blame a follow-up edit that may not exist --
                # the engine simply refuses to serialise a document past its
                # memory ceiling, and the recorded fingerprints cannot be
                # checked against a document that cannot be fingerprinted.
                if recorded_content and not checkpoint_signature_is_complete(
                    current_content
                ):
                    raise HwpLiveError(
                        "현재 문서 전체 본문 지문을 읽지 못해 삭제 전과 후 어느 "
                        + "상태인지 확인할 수 없습니다. 본문 직렬화가 실패할 만큼 "
                        + "큰 문서에서는 지문 자체가 만들어지지 않습니다. 다른 "
                        + "편집을 실행 취소하지 않도록 문서를 바꾸지 않았습니다. "
                        + "한/글에서 이력 순서를 직접 확인해 실행 취소하세요",
                        mutation_started=False,
                    )
                matches_before = (
                    current_structure == entry.before_document_structure
                    and _body_matches(
                        current_page_text,
                        entry.before_page_text_signature,
                    )
                    and _complete_content_matches(
                        entry.before_content_signature,
                        current_content,
                    )
                )
                matches_after = (
                    current_structure == entry.after_document_structure
                    and _body_matches(
                        current_page_text,
                        entry.after_page_text_signature,
                    )
                    and _complete_content_matches(
                        entry.after_content_signature,
                        current_content,
                    )
                )
                if matches_before and matches_after:
                    raise HwpLiveError(
                        "삭제 전과 후를 구별할 수 없어 문서 전체 개체 지문이 같은 "
                        + "이력은 실행하지 않습니다. 다른 편집을 실행 취소하지 않도록 "
                        + "문서를 바꾸지 않았습니다. 한/글에서 이력 순서를 직접 확인해 "
                        + "실행 취소하세요",
                        mutation_started=False,
                    )
                matches_target = (
                    matches_before if direction == "undo" else matches_after
                )
                matches_origin = (
                    matches_after if direction == "undo" else matches_before
                )
                already_at_target = matches_target
                if not already_at_target and not matches_origin:
                    raise HwpLiveError(
                        "현재 문서가 삭제 전과 후 어느 지문과도 일치하지 않아 삭제 뒤 "
                        + "후속 편집이 있는 것으로 판단했습니다. 그 편집을 관통하지 "
                        + "않도록 문서를 바꾸지 않았습니다. 후속 편집을 저장하거나 "
                        + "되돌린 뒤 다시 요청하세요",
                        mutation_started=False,
                    )
                minimum_steps = 0 if already_at_target else 1
            else:
                current_snapshot = read_native_snapshot(candidate.window_handle)
                if current_snapshot is None:
                    raise HwpLiveError(
                        "현재 문서의 쪽 수를 읽지 못해 삭제 전후 상태를 확인할 수 "
                        + "없습니다. 문서를 바꾸지 않았습니다. 문서 상태를 직접 확인한 "
                        + "뒤 다시 요청하세요",
                        mutation_started=False,
                    )
                recorded_content = (
                    entry.before_content_signature or entry.after_content_signature
                )
                current_content = (
                    read_native_content_signature(candidate.window_handle) or ""
                    if recorded_content
                    else ""
                )
                # Same reasoning as the control-deletion branch above: an
                # unreadable current signature must not read as "matches
                # neither state".
                if recorded_content and not checkpoint_signature_is_complete(
                    current_content
                ):
                    raise HwpLiveError(
                        "현재 문서 전체 본문 지문을 읽지 못해 쪽 삭제 전과 후 어느 "
                        + "상태인지 확인할 수 없습니다. 본문 직렬화가 실패할 만큼 "
                        + "큰 문서에서는 지문 자체가 만들어지지 않습니다. 다른 "
                        + "편집을 실행 취소하지 않도록 문서를 바꾸지 않았습니다. "
                        + "한/글에서 이력 순서를 직접 확인해 실행 취소하세요",
                        mutation_started=False,
                    )
                matches_before = (
                    current_snapshot.page_count == entry.before_page_count
                    and _complete_content_matches(
                        entry.before_content_signature,
                        current_content,
                    )
                )
                matches_after = (
                    current_snapshot.page_count == entry.after_page_count
                    and _complete_content_matches(
                        entry.after_content_signature,
                        current_content,
                    )
                )
                if matches_before and matches_after:
                    raise HwpLiveError(
                        "쪽 삭제 전과 후의 쪽 수가 같아 두 상태를 구별할 수 없습니다. "
                        + "문서를 바꾸지 않았습니다. 한/글에서 이력 순서를 직접 확인해 "
                        + "실행 취소하세요",
                        mutation_started=False,
                    )
                matches_target = (
                    matches_before if direction == "undo" else matches_after
                )
                matches_origin = (
                    matches_after if direction == "undo" else matches_before
                )
                already_at_target = matches_target
                if not already_at_target and not matches_origin:
                    raise HwpLiveError(
                        "현재 문서가 쪽 삭제 전과 후 어느 상태와도 일치하지 않아 삭제 "
                        + "뒤 후속 편집이 있는 것으로 판단했습니다. 그 편집을 관통하지 "
                        + "않도록 문서를 바꾸지 않았습니다. 후속 편집을 저장하거나 "
                        + "되돌린 뒤 다시 요청하세요",
                        mutation_started=False,
                    )
            if already_at_target:
                executed, duration = 0, 0
            else:
                executed, duration = execute_native_history_until_state(
                    candidate,
                    direction,
                    entry.maximum_native_steps,
                    page_count=target_page_count,
                    page=entry.page,
                    controls=target_controls,
                    document_structure=target_document_structure,
                    minimum_steps=minimum_steps,
                    origin_page_count=origin_page_count,
                    origin_controls=origin_controls,
                    origin_document_structure=origin_document_structure,
                    text_signature=target_page_text_signature,
                    origin_text_signature=origin_page_text_signature,
                    content_signature=target_content_signature,
                    origin_content_signature=origin_content_signature,
                )
            measured = (
                "문서가 이미 MCP 작업 경계 상태여서 한컴 실행 이력을 실행하지 "
                + "않았습니다"
                if already_at_target
                else f"한컴 실행 이력 {executed}단계로 MCP 작업 경계 상태를 확인했습니다"
            )
        else:
            checkpoint = entry.before if direction == "undo" else entry.after
            # The state the MCP edit left behind: what the document must still
            # look like for the checkpoint restore to be destroying nothing.
            settled = entry.after if direction == "undo" else entry.before
            settled_label = "편집 후" if direction == "undo" else "편집 전"
            target_label = "편집 전" if direction == "undo" else "편집 후"
            if not checkpoint_signature_is_complete(settled.signature):
                raise HwpLiveError(
                    f"{settled_label} 문서 내용 지문을 읽지 못해 체크포인트 복원이 "
                    + "다른 편집을 지우지 않는지 확인할 수 없습니다. 문서를 바꾸지 "
                    + "않았습니다. 문서 상태를 직접 확인한 뒤 다시 요청하세요",
                    mutation_started=False,
                )
            if not checkpoint_signature_is_complete(checkpoint.signature):
                raise HwpLiveError(
                    f"{target_label} 문서 내용 지문을 읽지 못해 체크포인트 복원 "
                    + "목표를 확인할 수 없습니다. 문서를 바꾸지 않았습니다. 문서 "
                    + "상태를 직접 확인한 뒤 다시 요청하세요",
                    mutation_started=False,
                )
            current_signature = _probe_content_signature(
                candidate,
                history,
                entry.document_id,
                entry.full_name,
            )
            if not checkpoint_signature_is_complete(current_signature):
                raise HwpLiveError(
                    "현재 문서 내용 지문을 읽지 못해 편집 전후 상태를 확인할 수 "
                    + "없습니다. 남의 편집을 지우지 않도록 문서를 바꾸지 않았습니다. "
                    + "문서 상태를 직접 확인한 뒤 다시 요청하세요",
                    mutation_started=False,
                )
            matches_target = _complete_content_matches(
                checkpoint.signature,
                current_signature,
            )
            matches_settled = _complete_content_matches(
                settled.signature,
                current_signature,
            )
            if matches_target and matches_settled:
                raise HwpLiveError(
                    "현재 문서 내용 지문으로 편집 전과 후를 구별할 수 없어 어느 "
                    + "체크포인트도 적용하지 않았습니다. 문서는 바뀌지 않았습니다. "
                    + "한/글에서 이력 순서를 직접 확인해 실행 취소하세요",
                    mutation_started=False,
                )
            if matches_target:
                executed, duration, measured = (
                    0,
                    0,
                    "문서가 이미 MCP 작업 경계 상태여서 체크포인트를 적용하지 "
                    + "않았습니다",
                )
            elif matches_settled:
                engine_undo_origin = (
                    settled
                    if direction == "undo"
                    and steps == 1
                    and entry.operation == "text.patch"
                    and entry.p1_single_text_undo
                    else None
                )
                executed, duration, measured = _restore_checkpoint(
                    candidate,
                    entry.document_id,
                    entry.full_name,
                    checkpoint,
                    history=history,
                    expected_signature=settled.signature,
                    engine_undo_origin=engine_undo_origin,
                )
                entry_used_engine_history = "engine_undo_verified" in measured
                if entry_used_engine_history:
                    checkpoint_history = False
            else:
                raise HwpLiveError(
                    "현재 문서가 편집 전과 후 어느 지문과도 일치하지 않아 후속 "
                    + "편집이 있는 것으로 판단했습니다. 그 편집을 관통하지 않도록 "
                    + "문서를 바꾸지 않았습니다. 후속 편집을 저장하거나 되돌린 뒤 "
                    + "다시 요청하세요",
                    mutation_started=False,
                )
        if isinstance(entry, DocumentEditHistoryEntry):
            verify_history_entry_state(candidate, entry, direction)
        if entry_used_engine_history:
            history.drop(direction, entry)
        else:
            history.commit(direction, entry)
        if isinstance(entry, DocumentEditHistoryEntry):
            verify_history_entry_state(candidate, entry, direction)
        commands += executed
        elapsed += duration
        if measured:
            measurements.append(measured)
    return LiveEditHistoryExecution(
        commands,
        elapsed,
        checkpoint_history,
        notice="; ".join(measurements),
        changed_pages=tuple(sorted(changed_pages)),
    )

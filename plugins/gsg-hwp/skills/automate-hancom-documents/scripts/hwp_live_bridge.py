from __future__ import annotations

import ntpath
import time
from concurrent.futures import (
    CancelledError,
    Future,
    TimeoutError as FutureTimeoutError,
)
from collections.abc import Callable, Collection, Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, local
from typing import Final, TypeVar, cast, final, override

from pywintypes import com_error

from hwp_errors import (
    HwpLiveError,
    HwpTargetProcessLostError,
    is_hwp_target_process_lost_error,
)
from hwp_live_bridge_contract import (
    BridgeSnapshot,
    BridgeState,
    HancomDialogActionResult,
    HancomDialogDismissResult,
    HancomPopupStructure,
    HancomWindowState,
    HancomWindowStateList,
)
from hwp_live_bridge_diagnostic import popup_diagnostic
from hwp_live_bridge_mutation import HancomBridgeDocumentMutationMixin
from hwp_live_bridge_operation import HancomBridgeOperationMixin
from hwp_live_bridge_session import HancomBridgeSessionMixin
from hwp_live_contract import (
    ConnectedDocument,
    LiveContext,
    MutationResult,
    OpenDocument,
    OpenDocumentList,
    PreviewResult,
    StyleReadOutcome,
)
from hwp_live_grounding import HwpGroundingReport, HwpGroundingRequest
from hwp_live_events import ChangeObservable, ChangeSignal, HwpEventSignal
from hwp_live_native_events import HybridChangeSignal
from hwp_live_native_action_contract import NativeActionFailure
from hwp_live_native_batch import (
    forget_cached_content_signatures,
    forget_content_signature_refusals_on_rebind,
    load_persisted_refusals_once,
    native_dispatch_operation_scope,
    note_native_window_content_change,
)
from hwp_live_native_dispatch_scope import current_public_native_dispatch_scope
from hwp_live_process_lane import (
    HwpLaneOperationContext,
    HwpMutationWatchdog,
    HwpProcessLane,
    MutationWatchdogObservation,
    Win32MutationWatchdogProbe,
    adaptive_watchdog_poll_seconds,
)
from hwp_live_progress import (
    NativeProgressClock,
    NativeProgressMark,
    NativeWorkDeclaration,
    native_progress_sink,
)
from hwp_live_session import LiveHwpController
from hwp_live_session_candidate import select_operation_document
from hwp_live_session_lifecycle import (
    SaveStateMachine,
    SaveStateSnapshot,
    save_state_uncertainty_is_native_readback_only,
)
from hwp_live_state_cache import HancomStateCache
from hwp_live_structure_contract import (
    DocumentStructure,
    FastPageInspection,
)
from hwp_live_windows import (
    ProcessExitWatch,
    Win32WindowStateReader,
    WindowStateReader,
    open_process_exit_watch,
    stalled_hwp_call_diagnostic,
)


T = TypeVar("T")
_OPERATION_RECOVERY_ID: ContextVar[str | None] = ContextVar(
    "hwp_operation_recovery_id",
    default=None,
)
_FAST_INSPECTION_CACHE_LIMIT: Final = 32
_DEFAULT_CALL_TIMEOUT_SECONDS: Final = 180.0
_DEFAULT_PROCESS_QUEUE_LIMIT: Final = 8
_RECOVERY_READ_TIMEOUT_SECONDS: Final = 1.0
_PROCESS_EXIT_POLL_SECONDS: Final = 0.05
_MODAL_DIAGNOSTIC_DELAY_SECONDS: Final = 0.5
_MODAL_DIAGNOSTIC_INTERVAL_SECONDS: Final = 0.25
# 네이티브 호출 한 번은 그 안에서 진행을 알릴 수 없다(ExecuteActions 1회). 그
# 구간이 기본 마감보다 길어질 수 있으면, 들어가기 전에 선언한 작업량을 초로
# 환산해 그만큼만 더 기다린다. 예측이 아니라 "이 시간까지는 죽었다고 부르지
# 않는다"는 상한선이다.
#
# 단가는 실측에서 나왔다(artifacts/live-defects/batch-cost-*). 같은 61x23 시트를
# 세 분할 예산으로 돌렸을 때 배치별 최악은 위상 단위당 1.570ms(100k 배치 6),
# 명령당 72.0ms(50k 배치 7)였다. 한 단가만 놓고 보면 여유가 2배에 못 미치지만,
# 허용치는 두 모형의 max() 이고 기본 마감이 하한이라, 실제로 물리는 여유는
# 단가 하나로 보는 것보다 크다. 실측 32개 배치 전부에 대해 허용치/실제시간을
# 계산했을 때 가장 얇은 것이 3.35배(50k 배치 7), 그다음이 3.43배(100k 배치 4)
# 였다. 즉 이 값들은 여유가 가장 얇은 배치를 기준으로 잡혀 있다.
_NATIVE_TOPOLOGY_UNIT_SECONDS: Final = 0.003
_NATIVE_COMMAND_UNIT_SECONDS: Final = 0.10
# MCP 작업자가 한 번의 도구 호출을 끊는 시각. 권위는
# hwp_mcp_worker_deadline.DEFAULT_CALL_TIMEOUT_SECONDS 이고, 여기서 import 하지
# 않는 것은 그 모듈이 anyio·mcp 를 끌고 오기 때문이다(브리지는 MCP 를 모른다).
# 두 값이 어긋나지 않는다는 것은 시험으로 묶어 둔다.
_WORKER_CALL_DEADLINE_SECONDS: Final = 240.0
# 선언이 틀려도 무한정 기다리지 않게 하는 천장. 네이티브 파서가 받는 명령 수
# 상한(kMaximumCommands 20,000)에 명령 단가 모형을 그대로 적용하면 2,000초가
# 되는데, 그건 행을 잡는 장치이기를 포기하는 값이다.
#
# 진짜 상한은 계산 모형이 아니라 작업자 마감이다. 천장이 마감(240초)보다 크면
# 그 사이의 선언은 아무 일도 하지 않는다 — 브리지가 "이 구간은 죽었다"는
# 진단(phase·progress_beats·commands_completed·work_allowance_seconds 가 실린
# _deadline_error)을 내기 전에 작업자가 먼저 무딘 타임아웃으로 호출을 끊는다.
# 옛 900초는 그 구간이 660초나 되는 값이었다.
#
# 여유 30초는 한 호출에서 이 실행 구간 바깥에 남는 시간이다: 레인 큐 대기,
# COM 아파트 진입, 마감이 걸린 뒤의 모달 진단(_MODAL_DIAGNOSTIC_DELAY_SECONDS
# 와 폴링), 프로세스 격리·정리, 결과 봉투 직렬화. 마감의 12.5% 이고, 기본
# 마감 180초보다는 크므로 선언 없는 호출의 예산은 건드리지 않는다.
_NATIVE_WORK_ALLOWANCE_CEILING_SECONDS: Final = _WORKER_CALL_DEADLINE_SECONDS - 30.0
_RPC_E_CALL_REJECTED: Final = -2_147_418_111
_RPC_E_SERVERCALL_RETRYLATER: Final = -2_147_417_846
_RPC_E_SERVERCALL_REJECTED: Final = -2_147_417_845
_COM_BUSY_HRESULTS: Final = frozenset(
    {
        _RPC_E_CALL_REJECTED,
        _RPC_E_SERVERCALL_RETRYLATER,
        _RPC_E_SERVERCALL_REJECTED,
    }
)
_COM_BUSY_DELAYS: Final = (0.025, 0.05, 0.1)


@contextmanager
def operation_recovery_scope(operation_id: str | None) -> Generator[None, None, None]:
    token = _OPERATION_RECOVERY_ID.set(operation_id)
    try:
        yield
    finally:
        _OPERATION_RECOVERY_ID.reset(token)


@dataclass(frozen=True, slots=True)
class _FastInspectionCacheEntry:
    sequence: int
    inspection: FastPageInspection


@dataclass(frozen=True, slots=True)
class _DocumentWatchTarget:
    process_id: int
    window_handle: int
    document_path: Path
    document_id: int
    document: OpenDocument


@dataclass(frozen=True, slots=True)
class _SaveCloseBlocker:
    session_id: str
    process_id: int | None
    state: SaveStateSnapshot


@dataclass(frozen=True, slots=True)
class _PoisonedLaneRecovery:
    futures: tuple[Future[object], ...]
    contexts: tuple[HwpLaneOperationContext, ...]
    reconcile_required: bool
    operation_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _PoisonRecoveryObservation:
    process_alive: bool | None
    responding: bool | None
    modal_present: bool | None
    progress_visible: bool | None
    target_known: bool
    target_identity_valid: bool | None


class _BridgeThreadState(local):
    process_id: int

    def __init__(self) -> None:
        self.process_id = 0


def _exception_chain(error: BaseException) -> tuple[BaseException, ...]:
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return tuple(chain)


def _is_com_busy_error(error: BaseException) -> bool:
    signed = {str(value) for value in _COM_BUSY_HRESULTS}
    unsigned = {str(value & 0xFFFFFFFF) for value in _COM_BUSY_HRESULTS}
    hexadecimal = {f"0x{value & 0xFFFFFFFF:08x}" for value in _COM_BUSY_HRESULTS}
    for item in _exception_chain(error):
        hresult = getattr(item, "hresult", None)
        if isinstance(hresult, int) and (
            hresult in _COM_BUSY_HRESULTS
            or hresult - 0x1_0000_0000 in _COM_BUSY_HRESULTS
        ):
            return True
        message = str(item).lower()
        if any(token in message for token in signed | unsigned | hexadecimal):
            return True
        if "call was rejected by callee" in message or "server busy" in message:
            return True
    return False


def _native_work_allowance_seconds(
    base_seconds: float,
    declaration: NativeWorkDeclaration | None,
) -> float:
    """이 구간을 죽었다고 부르기 전에 기다릴 시간.

    선언이 없으면 기본 마감 그대로다. 선언이 있으면 두 모형(위상 단위·명령 수)
    중 큰 쪽을 쓴다 — 어느 쪽이 지배적인지는 문서마다 다르고, 실측에서 같은
    작업의 배치별 실제 시간이 위상 단위 기준 37배까지 벌어졌기 때문에 둘 중
    하나만 믿을 수 없다. 기본 마감보다 짧아지는 일은 없고, 천장을 넘지 않는다.
    """

    if declaration is None:
        return base_seconds
    predicted = max(
        declaration.topology_units * _NATIVE_TOPOLOGY_UNIT_SECONDS,
        declaration.commands * _NATIVE_COMMAND_UNIT_SECONDS,
    )
    return max(base_seconds, min(predicted, _NATIVE_WORK_ALLOWANCE_CEILING_SECONDS))


def _deadline_error(
    *,
    mutation: bool,
    phase: str,
    process_lane_isolation: bool,
    save_state: SaveStateSnapshot | None = None,
    watchdog: MutationWatchdogObservation | None = None,
    progress: NativeProgressMark | None = None,
    allowance_seconds: float | None = None,
) -> HwpLiveError:
    reconcile = mutation and phase == "running"
    mutation_started = mutation and phase == "running"
    return HwpLiveError(
        "".join(
            (
                "한컴 COM 전체 제한시간을 초과했습니다",
                f"; phase={phase}",
                # 마감은 "마지막으로 확인된 진행" 이후로 재기 때문에, 실패
                # 보고에 그 진행이 무엇이었는지 같이 싣지 않으면 왜 이 시각에
                # 끊겼는지 읽을 수 없다.
                (
                    ""
                    if progress is None or progress.count == 0
                    else (
                        f"; progress_beats={progress.count}"
                        f"; last_progress={progress.label}"
                    )
                ),
                # 끝난 명령 수는 자유문이 아니라 토큰으로 싣는다. 결과 봉투가
                # 이 값을 그대로 commands_executed 로 옮기므로, 마감으로 끊긴
                # 작업도 "0개 실행"이라고 거짓말하지 않는다.
                (
                    ""
                    if progress is None or progress.completed_units == 0
                    else f"; commands_completed={progress.completed_units}"
                ),
                (
                    ""
                    if allowance_seconds is None
                    else f"; work_allowance_seconds={allowance_seconds:.3f}"
                ),
                (
                    ""
                    if progress is None or progress.declaration is None
                    else (
                        f"; declared_segment={progress.declaration.label}"
                        f"; declared_commands={progress.declaration.commands}"
                    )
                ),
                f"; queue_busy={'true' if phase == 'queue_wait' else 'false'}",
                "; worker_isolation_required=false",
                (
                    "; process_lane_isolation_required=true"
                    if process_lane_isolation
                    else "; process_lane_isolation_required=false"
                ),
                f"; reconcile_required={'true' if reconcile else 'false'}",
                (
                    "; mutation_started=true"
                    if mutation_started
                    else "; mutation_started=false"
                ),
                f"; retry_safe={'false' if reconcile else 'true'}",
                (
                    ""
                    if save_state is None
                    else (
                        f"; save_state={save_state.phase}"
                        f"; save_terminal={'true' if save_state.terminal else 'false'}"
                        f"; save_close_blocked="
                        f"{'true' if save_state.close_blocked else 'false'}"
                    )
                ),
                (
                    ""
                    if watchdog is None
                    else (
                        f"; watchdog_elapsed_seconds={watchdog.elapsed_seconds:.3f}"
                        f"; watchdog_poll_seconds={watchdog.poll_seconds:.3f}"
                        f"; target_process_alive={watchdog.process_alive}"
                        f"; target_responding={watchdog.responding}"
                        f"; progress_control_present="
                        f"{watchdog.progress_control_present}"
                        f"; progress_control_visible="
                        f"{watchdog.progress_control_visible}"
                        f"; target_modal_present={watchdog.modal_present}"
                        f"; file_metadata_changed={watchdog.metadata_changed}"
                    )
                ),
            )
        ),
        mutation_started=mutation_started,
    )


def _poison_recovery_error(
    *,
    process_id: int,
    recovery: _PoisonedLaneRecovery | None,
    observation: _PoisonRecoveryObservation | None,
    pending: bool,
) -> HwpLiveError:
    reconcile_required = False if recovery is None else recovery.reconcile_required
    process_alive = None if observation is None else observation.process_alive
    responding = None if observation is None else observation.responding
    modal_present = None if observation is None else observation.modal_present
    progress_visible = None if observation is None else observation.progress_visible
    target_identity_valid = (
        None if observation is None else observation.target_identity_valid
    )
    target_known = False if observation is None else observation.target_known
    file_metadata_changed = (
        False
        if recovery is None
        else any(context.watchdog_metadata_changed for context in recovery.contexts)
    )
    if pending:
        user_action = (
            "대상 selector의 상태 조회 또는 hwp_get_operation_status로 현재 "
            "문서 상태를 판정하세요. 판정 전에는 편집만 보류됩니다"
        )
    elif modal_present is True:
        user_action = (
            "한글 화면의 대화상자를 직접 확인해 완료한 뒤 상태 조회를 다시 호출하세요"
        )
    elif responding is False or progress_visible is True:
        user_action = "한글 작업이 끝나 창이 다시 응답할 때까지 기다린 뒤 상태 조회를 다시 호출하세요"
    else:
        user_action = (
            "대상 selector로 문서 상태를 다시 읽으세요. 저장 작업이면 "
            "hwp_get_operation_status의 파일 지문 결과도 함께 확인하고, "
            "판정할 수 없을 때만 한글 화면에서 직접 확인하세요"
        )
    return HwpLiveError(
        "".join(
            (
                "한컴 COM 전체 제한시간을 초과했습니다",
                f"; process_id={process_id if process_id > 0 else 'unknown'}",
                "; phase=recovery",
                f"; queue_busy={'true' if pending else 'false'}",
                "; worker_isolation_required=false",
                "; process_lane_isolation_required=true",
                "; lane_poisoned=true",
                "; mutation_gate=true",
                "; reads_allowed=true",
                f"; lane_recovery_pending={'true' if pending else 'false'}",
                f"; target_process_alive={process_alive}",
                f"; target_responding={responding}",
                f"; target_modal_present={modal_present}",
                f"; progress_control_visible={progress_visible}",
                f"; target_known={'true' if target_known else 'false'}",
                f"; target_identity_valid={target_identity_valid}",
                f"; file_metadata_changed={'true' if file_metadata_changed else 'false'}",
                f"; reconcile_required={'true' if reconcile_required else 'false'}",
                "; mutation_started=false",
                f"; retry_safe={'false' if reconcile_required else 'true'}",
                f"; user_action={user_action}",
            )
        )
    )


def _exception_outcome_resolved(error: BaseException) -> bool:
    if isinstance(error, NativeActionFailure):
        return error.partial_mutation is False and error.retry_safe is True
    return False


def _process_lost_error(
    *,
    process_id: int,
    mutation: bool,
    phase: str,
    diagnostic: str | None,
) -> HwpTargetProcessLostError:
    mutation_started = mutation and phase == "running"
    reason = "".join(
        (
            "대상 한컴 프로세스가 종료되어 연결이 무효화되었습니다",
            f"; process_id={process_id if process_id > 0 else 'unknown'}",
            "; target_process_lost=true",
            "; reconnect_required=true",
            "; worker_isolation_required=true",
            "; process_lane_isolation_required=true",
            f"; phase={phase}",
            ("; queue_busy=true" if phase == "queue_wait" else "; queue_busy=false"),
            (
                "; reconcile_required=true"
                if mutation_started
                else "; reconcile_required=false"
            ),
            (
                "; mutation_started=true"
                if mutation_started
                else "; mutation_started=false"
            ),
            "; retry_safe=false" if mutation_started else "; retry_safe=true",
            "; 한컴 문서에 재연결해야 합니다",
        )
    )
    if diagnostic is not None:
        reason = f"{reason}; {diagnostic}"
    return HwpTargetProcessLostError(
        reason,
        mutation_started=mutation_started,
    )


def _modal_dialog_error(
    *,
    mutation: bool,
    phase: str,
    diagnostic: str,
    save_state: SaveStateSnapshot | None = None,
    watchdog: MutationWatchdogObservation | None = None,
) -> HwpLiveError:
    mutation_started = mutation and phase == "running"
    return HwpLiveError(
        "".join(
            (
                diagnostic,
                "; target_modal_dialog=true",
                "; reconnect_required=true",
                "; worker_isolation_required=true",
                "; process_lane_isolation_required=true",
                f"; phase={phase}",
                (
                    "; reconcile_required=true"
                    if mutation_started
                    else "; reconcile_required=false"
                ),
                (
                    "; mutation_started=true"
                    if mutation_started
                    else "; mutation_started=false"
                ),
                "; retry_safe=false" if mutation_started else "; retry_safe=true",
                (
                    ""
                    if save_state is None
                    else (
                        f"; save_state={save_state.phase}"
                        f"; save_terminal={'true' if save_state.terminal else 'false'}"
                        f"; save_close_blocked="
                        f"{'true' if save_state.close_blocked else 'false'}"
                    )
                ),
                (
                    ""
                    if watchdog is None
                    else (
                        f"; watchdog_elapsed_seconds={watchdog.elapsed_seconds:.3f}"
                        f"; target_responding={watchdog.responding}"
                        f"; progress_control_visible="
                        f"{watchdog.progress_control_visible}"
                        f"; file_metadata_changed={watchdog.metadata_changed}"
                    )
                ),
                "; 대화상자를 직접 확인한 뒤 한컴 문서에 재연결해야 합니다",
            )
        ),
        mutation_started=mutation_started,
    )


STYLE_READ_UNVERIFIED_REASON: Final = (
    "스타일을 읽는 동안 한컴 프로세스가 계속 이벤트를 냈습니다. "
    "목록은 실제로 읽어 온 값이지만 그 사이 문서가 바뀌었다면 최신이 아닐 수 "
    "있으니 정확도가 중요하면 다시 호출하세요"
)


def _unverified_style_read(outcome: StyleReadOutcome) -> StyleReadOutcome:
    return StyleReadOutcome(
        styles=outcome.styles,
        unverified_reason=STYLE_READ_UNVERIFIED_REASON,
    )


@final
class HancomBridge(
    HancomBridgeSessionMixin,
    HancomBridgeOperationMixin,
    HancomBridgeDocumentMutationMixin,
):
    __slots__: tuple[str, ...] = (
        "_cache",
        "_active_mutation_contexts",
        "_closed",
        "_controller",
        "_controller_factory",
        "_controllers",
        "_worked_lanes",
        "_call_timeout_seconds",
        "_deferred_transient_releases",
        "_events",
        "_fast_inspections",
        "_initial_event_signal",
        "_lifecycle_lock",
        "_lost_sessions",
        "_poison_recoveries",
        "_recovery_identity_confirmed",
        "_recovery_outcome_confirmed",
        "_mutation_futures",
        "_process_futures",
        "_process_sessions",
        "_process_lanes",
        "_process_queue_limit",
        "_poisoned_processes",
        "_session_processes",
        "_session_watch_targets",
        "_style_revisions",
        "_thread_state",
        "_transient_sessions",
        "_windows",
        "_uncertain_save_states",
    )

    _cache: HancomStateCache
    _active_mutation_contexts: dict[int, HwpLaneOperationContext]
    _closed: bool
    _controller: LiveHwpController
    _controller_factory: Callable[[], LiveHwpController] | None
    _controllers: dict[int, LiveHwpController]
    _worked_lanes: set[int]
    _call_timeout_seconds: float
    _deferred_transient_releases: set[str]
    _events: dict[int, ChangeSignal]
    _fast_inspections: dict[tuple[str, int, bool], _FastInspectionCacheEntry]
    _initial_event_signal: ChangeSignal | None
    _lifecycle_lock: Lock
    _lost_sessions: dict[str, int]
    _poison_recoveries: dict[int, _PoisonedLaneRecovery]
    _recovery_identity_confirmed: set[int]
    _recovery_outcome_confirmed: set[int]
    _mutation_futures: dict[int, set[Future[object]]]
    _process_futures: dict[int, set[Future[object]]]
    _process_sessions: dict[int, set[str]]
    _process_lanes: dict[int, HwpProcessLane]
    _process_queue_limit: int
    _poisoned_processes: set[int]
    _session_processes: dict[str, int]
    _session_watch_targets: dict[str, _DocumentWatchTarget]
    _style_revisions: dict[str, int]
    _thread_state: _BridgeThreadState
    _transient_sessions: set[str]
    _windows: WindowStateReader
    _uncertain_save_states: dict[str, SaveStateMachine]

    def __init__(
        self,
        controller: LiveHwpController,
        window_reader: WindowStateReader | None = None,
        change_signal: ChangeSignal | None = None,
        *,
        controller_factory: Callable[[], LiveHwpController] | None = None,
        call_timeout_seconds: float = _DEFAULT_CALL_TIMEOUT_SECONDS,
        process_queue_limit: int = _DEFAULT_PROCESS_QUEUE_LIMIT,
    ) -> None:
        if call_timeout_seconds <= 0:
            raise ValueError("한컴 COM 제한시간은 양수여야 합니다")
        if process_queue_limit < 1:
            raise ValueError("한컴 프로세스 큐 한도는 1 이상이어야 합니다")
        self._controller = controller
        self._controller_factory = controller_factory
        self._controllers = {0: controller}
        # 시드 컨트롤러는 pid 0 자리에 "등록"되었을 뿐 아직 아무 작업도 하지
        # 않았다. 컨트롤러가 COM 아파트를 여는 것은 오직 레인 스레드에서
        # 작업이 돌 때뿐이므로, 표시는 "컨트롤러를 가져갔다"가 아니라
        # "레인에 작업을 제출했다"로 남긴다. _call 의 제출 지점 한 곳에서만
        # 채운다.
        self._worked_lanes = set()
        self._call_timeout_seconds = call_timeout_seconds
        self._closed = False
        self._deferred_transient_releases = set()
        self._lifecycle_lock = Lock()
        self._cache = HancomStateCache()
        self._active_mutation_contexts = {}
        self._fast_inspections = {}
        self._events = {}
        self._initial_event_signal = change_signal
        self._lost_sessions = {}
        self._poison_recoveries = {}
        self._recovery_identity_confirmed = set()
        self._recovery_outcome_confirmed = set()
        self._mutation_futures = {}
        self._process_futures = {}
        self._process_sessions = {}
        self._process_lanes = {}
        self._process_queue_limit = process_queue_limit
        self._poisoned_processes = set()
        self._session_processes = {}
        self._session_watch_targets = {}
        self._style_revisions = {}
        self._thread_state = _BridgeThreadState()
        self._transient_sessions = set()
        self._windows = (
            Win32WindowStateReader() if window_reader is None else window_reader
        )
        self._uncertain_save_states = {}

    def _mutation_context(
        self,
        session_id: str | None,
        *,
        mutation: bool,
        save_operation: bool,
    ) -> HwpLaneOperationContext | None:
        if not mutation or session_id is None:
            return None
        target = self._session_watch_targets.get(session_id)
        if target is None:
            return None
        context = HwpLaneOperationContext(
            session_id=session_id,
            process_id=target.process_id,
            window_handle=target.window_handle,
            document_path=target.document_path,
            document_id=target.document_id,
        )
        if save_operation:
            _ = context.attach_save_state(SaveStateMachine())
        self._active_mutation_contexts[id(context)] = context
        return context

    def _mutation_watchdog(
        self,
        context: HwpLaneOperationContext | None,
        *,
        started_at: float,
    ) -> HwpMutationWatchdog | None:
        if context is None:
            return None
        signal = self._events.get(context.process_id)
        event_reader = (
            signal.events_after if isinstance(signal, HwpEventSignal) else None
        )
        after_event_sequence = signal.sequence() if signal is not None else 0
        terminal_seconds = min(180.0, self._call_timeout_seconds)
        warning_seconds = min(30.0, terminal_seconds / 2)
        poll_seconds = adaptive_watchdog_poll_seconds(
            process_id=context.process_id,
            document_path=context.document_path,
        )
        watchdog = HwpMutationWatchdog(
            process_id=context.process_id,
            window_handle=context.window_handle,
            document_path=context.document_path,
            document_id=context.document_id,
            state=context,
            probe=Win32MutationWatchdogProbe(
                window_handle=context.window_handle,
                document_path=context.document_path,
                window_reader=self._windows,
                exit_watch=open_process_exit_watch(context.process_id),
            ),
            event_reader=event_reader,
            warning_seconds=warning_seconds,
            terminal_seconds=terminal_seconds,
            poll_seconds=poll_seconds,
            started_at=started_at,
            after_event_sequence=after_event_sequence,
            armed=False,
        )
        watchdog.start()
        return watchdog

    @staticmethod
    def _context_save_state(
        context: HwpLaneOperationContext | None,
    ) -> SaveStateMachine | None:
        if context is None:
            return None
        state = context.save_state()
        return state if isinstance(state, SaveStateMachine) else None

    def _mark_save_uncertain(
        self,
        context: HwpLaneOperationContext | None,
        *,
        source: str,
        watchdog: HwpMutationWatchdog | None,
    ) -> SaveStateSnapshot | None:
        if context is None:
            return None
        state = self._context_save_state(context)
        if state is None:
            return None
        observation = None if watchdog is None else watchdog.snapshot()
        detail = (
            None
            if observation is None
            else (
                f"elapsed_seconds={observation.elapsed_seconds:.3f};"
                f"responding={observation.responding};"
                f"progress_visible={observation.progress_control_visible};"
                f"modal_present={observation.modal_present};"
                f"metadata_changed={observation.metadata_changed}"
            )
        )
        state.uncertain(source=source, detail=detail)
        with self._lifecycle_lock:
            self._uncertain_save_states[context.session_id] = state
        return state.snapshot()

    def _release_mutation_context(
        self,
        context: HwpLaneOperationContext | None,
    ) -> None:
        if context is None:
            return
        state = self._context_save_state(context)
        release_session_id: str | None = None
        with self._lifecycle_lock:
            _ = self._active_mutation_contexts.pop(id(context), None)
            if state is not None and state.snapshot().phase == "verified":
                _ = self._uncertain_save_states.pop(context.session_id, None)
            elif state is not None and state.snapshot().phase == "uncertain":
                self._uncertain_save_states[context.session_id] = state
            if (
                context.session_id in self._deferred_transient_releases
                and self._save_close_blocker_locked(context.session_id) is None
            ):
                self._deferred_transient_releases.discard(context.session_id)
                release_session_id = context.session_id
        if release_session_id is not None:
            _ = self._release_transient_connection_now(release_session_id)

    def release_confirmed_save_close_block(self, session_id: str) -> bool:
        """디스크 지문으로 확정된 저장이 남긴 닫기 차단을 걷어낸다.

        ``SaveStateMachine`` 이 "verified"에 이르려면 네이티브 readback 이
        저장 전후 문서 지문을 대조해야 한다. 그 지문은 엔진이 문서를
        직렬화해야 생기고, 자기 메모리 한계 위의 문서에서는 만들어지지
        않는다. 그래서 그 크기에서는 ``hwp_save`` 의 native.verified 가 늘
        거짓이고, 저장할 때마다 ``_uncertain_save_states`` 에 차단이 새로
        찍힌다. 해제 경로는 같은 세션에서 phase 가 "verified"에 닿는 저장을
        한 번 더 요구하는데 그 저장은 도달할 수 없다 — 스스로 못 푸는
        차단이다.

        그런데 그 저장의 판정은 이미 더 강한 증거로 끝나 있다:
        ``reconcile_save_fingerprint`` 의 6조건(네이티브 Save 완료값 + 대상
        경로 + 파일 크기 + mtime + 저장 전후 SHA-256, hwp_save_fingerprint.py
        :145-187)은 라이브 readback 이 아니라 디스크를 본다. 그 판정이
        "confirmed"를 냈다면 이 차단이 지키려던 사실은 이미 확인됐다.

        걷어내는 것은 미확정의 출처가 네이티브 readback 하나뿐일 때로
        한정한다. 감시견 미확정·대화상자·프로세스 소실·마감으로 찍힌
        차단은 저장이 어디까지 갔는지 모른다는 뜻이라 그대로 남는다.
        """
        release_session_id: str | None = None
        with self._lifecycle_lock:
            state = self._uncertain_save_states.get(session_id)
            if state is None or not save_state_uncertainty_is_native_readback_only(
                state.snapshot()
            ):
                return False
            _ = self._uncertain_save_states.pop(session_id, None)
            if (
                session_id in self._deferred_transient_releases
                and self._save_close_blocker_locked(session_id) is None
            ):
                self._deferred_transient_releases.discard(session_id)
                release_session_id = session_id
        if release_session_id is not None:
            _ = self._release_transient_connection_now(release_session_id)
        return True

    def _save_close_blocker_locked(
        self,
        session_id: str | None,
    ) -> _SaveCloseBlocker | None:
        states: list[tuple[str, int | None, SaveStateMachine]] = []
        for context in self._active_mutation_contexts.values():
            if session_id is not None and context.session_id != session_id:
                continue
            state = context.request_close_guard()
            if isinstance(state, SaveStateMachine):
                states.append((context.session_id, context.process_id, state))
        for uncertain_session_id, state in self._uncertain_save_states.items():
            if session_id is not None and uncertain_session_id != session_id:
                continue
            target = self._session_watch_targets.get(uncertain_session_id)
            states.append(
                (
                    uncertain_session_id,
                    (
                        target.process_id
                        if target is not None
                        else self._session_processes.get(uncertain_session_id)
                    ),
                    state,
                )
            )
        seen: set[int] = set()
        for candidate_session_id, process_id, state in states:
            if id(state) in seen:
                continue
            seen.add(id(state))
            snapshot = state.snapshot()
            if snapshot.close_blocked:
                return _SaveCloseBlocker(
                    session_id=candidate_session_id,
                    process_id=process_id,
                    state=snapshot,
                )
        return None

    @staticmethod
    def _save_close_error(blocker: _SaveCloseBlocker) -> HwpLiveError:
        process_id = (
            "unknown" if blocker.process_id is None else str(blocker.process_id)
        )
        return HwpLiveError(
            "".join(
                (
                    "MCP 자동 연결 해제만 보류했습니다 — 저장 상태 확인 필요",
                    f"; session_id={blocker.session_id}; process_id={process_id}",
                    f"; save_state={blocker.state.phase}; save_terminal=",
                    f"{'true' if blocker.state.terminal else 'false'}",
                    "; save_close_blocked=true; reconcile_required=true",
                    "; retry_safe=false",
                    "; 한글 UI의 저장·닫기 조작은 차단하지 않습니다",
                    "; 현재 저장 호출이 끝난 뒤 hwp_disconnect를 다시 호출하세요",
                )
            )
        )

    def _poison_recovery_observation(
        self,
        process_id: int,
        session_id: str | None,
    ) -> _PoisonRecoveryObservation:
        with self._lifecycle_lock:
            target = (
                self._session_watch_targets.get(session_id)
                if session_id is not None
                else None
            )
            if target is None:
                target = next(
                    (
                        candidate
                        for candidate in self._session_watch_targets.values()
                        if candidate.process_id == process_id
                    ),
                    None,
                )
        watch: ProcessExitWatch | None = None
        if process_id > 0:
            try:
                watch = open_process_exit_watch(process_id)
            except (HwpLiveError, OSError, RuntimeError):
                watch = None
        if target is None:
            try:
                process_alive = None if watch is None else not watch.exited()
            except (HwpLiveError, OSError, RuntimeError):
                process_alive = None
            finally:
                if watch is not None:
                    watch.close()
            return _PoisonRecoveryObservation(
                process_alive=process_alive,
                responding=True,
                modal_present=None,
                progress_visible=None,
                target_known=False,
                target_identity_valid=None,
            )

        probe = Win32MutationWatchdogProbe(
            window_handle=target.window_handle,
            document_path=target.document_path,
            window_reader=self._windows,
            exit_watch=watch,
        )
        try:
            sample = probe.sample()
        except (HwpLiveError, OSError, RuntimeError, TypeError, ValueError):
            return _PoisonRecoveryObservation(
                process_alive=None,
                responding=None,
                modal_present=None,
                progress_visible=None,
                target_known=True,
                target_identity_valid=None,
            )
        finally:
            probe.close()
        try:
            window = self._windows.read(target.window_handle)
        except (HwpLiveError, OSError, RuntimeError, TypeError, ValueError):
            identity_valid = None
        else:
            identity_valid = window.exists and window.process_id == target.process_id
        process_alive = sample.process_alive
        if process_alive is not False and identity_valid is True:
            process_alive = True
        return _PoisonRecoveryObservation(
            process_alive=process_alive,
            responding=sample.responding if identity_valid is True else None,
            modal_present=sample.modal_present,
            progress_visible=sample.progress_control_visible,
            target_known=True,
            target_identity_valid=identity_valid,
        )

    def _recover_poisoned_process(
        self,
        process_id: int,
        session_id: str | None,
        *,
        allow_unresolved_read: bool,
        readback_succeeded: bool = False,
        outcome_confirmed: bool = False,
    ) -> bool:
        with self._lifecycle_lock:
            if process_id not in self._poisoned_processes:
                return True
            recovery = self._poison_recoveries.get(process_id)
            identity_confirmed = process_id in self._recovery_identity_confirmed
            outcome_confirmed = (
                outcome_confirmed or process_id in self._recovery_outcome_confirmed
            )
        observation = self._poison_recovery_observation(
            process_id,
            session_id,
        )
        if observation.process_alive is False:
            self._invalidate_process(process_id)
            raise _process_lost_error(
                process_id=process_id,
                mutation=(False if recovery is None else recovery.reconcile_required),
                phase=(
                    "running"
                    if recovery is not None and recovery.reconcile_required
                    else "queued"
                ),
                diagnostic=None,
            )
        futures = () if recovery is None else recovery.futures
        reconcile_required = False if recovery is None else recovery.reconcile_required
        pending = any(not future.done() for future in futures)
        unresolved_exception = False
        for future in futures:
            if not future.done() or future.cancelled():
                continue
            error = future.exception()
            if error is None:
                continue
            if is_hwp_target_process_lost_error(error):
                self._invalidate_process(process_id)
                raise _process_lost_error(
                    process_id=process_id,
                    mutation=reconcile_required,
                    phase=("running" if reconcile_required else "queued"),
                    diagnostic=None,
                ) from error
            if not _exception_outcome_resolved(error):
                unresolved_exception = True
        process_alive_confirmed = (
            readback_succeeded or process_id == 0 or observation.process_alive is True
        )
        target_ready = (
            readback_succeeded or identity_confirmed or not observation.target_known
        )
        outcome_resolved = (
            readback_succeeded
            if not futures
            else (
                not pending
                and (
                    not reconcile_required
                    or not unresolved_exception
                    or outcome_confirmed
                )
            )
        )
        if (
            recovery is None
            or not process_alive_confirmed
            or not target_ready
            or not outcome_resolved
        ):
            if allow_unresolved_read:
                return False
            raise _poison_recovery_error(
                process_id=process_id,
                recovery=recovery,
                observation=observation,
                pending=pending,
            )
        with self._lifecycle_lock:
            current = self._poison_recoveries.get(process_id)
            if process_id not in self._poisoned_processes or current is not recovery:
                return process_id not in self._poisoned_processes
            self._poisoned_processes.discard(process_id)
            _ = self._poison_recoveries.pop(process_id, None)
            self._recovery_identity_confirmed.discard(process_id)
            self._recovery_outcome_confirmed.discard(process_id)
        self._cache.clear()
        self._fast_inspections.clear()
        forget_cached_content_signatures()
        return True

    def _recover_after_document_read(self, session_id: str) -> None:
        with self._lifecycle_lock:
            process_id = self._session_processes.get(session_id)
            if process_id is None or process_id not in self._poisoned_processes:
                return
            self._recovery_identity_confirmed.add(process_id)
        _ = self._recover_poisoned_process(
            process_id,
            session_id,
            allow_unresolved_read=True,
            readback_succeeded=True,
        )

    def _call_recovery_read(
        self,
        operation: Callable[[], T],
        *,
        session_id: str,
    ) -> T:
        result = self._call(operation, session_id=session_id)
        self._recover_after_document_read(session_id)
        return result

    def reconcile_confirmed_save(
        self,
        selector: str | None,
        operation_id: str | None,
    ) -> bool:
        known = self._known_connection(selector)
        if known is None:
            return False
        with self._lifecycle_lock:
            process_id = self._session_processes.get(known.session_id)
            recovery = (
                None if process_id is None else self._poison_recoveries.get(process_id)
            )
        if (
            process_id is None
            or recovery is None
            or not recovery.contexts
            or operation_id is None
            or operation_id not in recovery.operation_ids
        ):
            return False
        for context in recovery.contexts:
            save_state = self._context_save_state(context)
            if save_state is None or not save_state.snapshot().terminal:
                return False
        with self._lifecycle_lock:
            if (
                process_id not in self._poisoned_processes
                or self._poison_recoveries.get(process_id) is not recovery
            ):
                return process_id not in self._poisoned_processes
            self._recovery_outcome_confirmed.add(process_id)
        try:
            return self._recover_poisoned_process(
                process_id,
                known.session_id,
                allow_unresolved_read=True,
                outcome_confirmed=True,
            )
        except HwpLiveError:
            return False

    @override
    def _call(
        self,
        operation: Callable[[], T],
        *,
        session_id: str | None = None,
        process_id: int | None = None,
        mutation: bool = False,
        save_operation: bool = False,
    ) -> T:
        public_dispatch_scope = current_public_native_dispatch_scope(self)
        progress = NativeProgressClock()

        def invoke() -> T:
            previous_process_id = self._thread_state.process_id
            self._thread_state.process_id = queue_key
            controller = self._bridge_controller()
            try:
                with native_progress_sink(progress), native_dispatch_operation_scope():
                    try:
                        delays = () if mutation else _COM_BUSY_DELAYS
                        for delay in (*delays, None):
                            try:
                                return operation()
                            except BaseException as error:
                                if delay is None or not _is_com_busy_error(error):
                                    raise
                            finally:
                                controller.restore_activation()
                            time.sleep(delay)
                        raise AssertionError("unreachable COM retry state")
                    except BaseException as error:
                        if is_hwp_target_process_lost_error(error):
                            try:
                                controller.invalidate_process_loss()
                            except (HwpLiveError, com_error, OSError, RuntimeError):
                                pass
                        raise
            finally:
                self._thread_state.process_id = previous_process_id

        started = time.monotonic()
        with self._lifecycle_lock:
            if self._closed:
                raise HwpLiveError("한컴 브리지가 이미 종료되었습니다")
            lost_process_id = self._lost_sessions.get(session_id or "")
            if lost_process_id is not None:
                raise _process_lost_error(
                    process_id=lost_process_id,
                    mutation=mutation,
                    phase="queued",
                    diagnostic=None,
                )
            queue_key = (
                process_id
                if process_id is not None
                else self._session_processes.get(session_id or "", 0)
            )
            poisoned = queue_key in self._poisoned_processes
        unresolved_recovery_read = False
        if poisoned:
            recovered = self._recover_poisoned_process(
                queue_key,
                session_id,
                allow_unresolved_read=not mutation,
            )
            unresolved_recovery_read = not mutation and not recovered
        call_timeout_seconds = (
            min(self._call_timeout_seconds, _RECOVERY_READ_TIMEOUT_SECONDS)
            if unresolved_recovery_read
            else self._call_timeout_seconds
        )
        with self._lifecycle_lock:
            if self._closed:
                raise HwpLiveError("한컴 브리지가 이미 종료되었습니다")
            lost_process_id = self._lost_sessions.get(session_id or "")
            if lost_process_id is not None:
                raise _process_lost_error(
                    process_id=lost_process_id,
                    mutation=mutation,
                    phase="queued",
                    diagnostic=None,
                )
            if mutation and queue_key in self._poisoned_processes:
                recovery = self._poison_recoveries.get(queue_key)
                raise _poison_recovery_error(
                    process_id=queue_key,
                    recovery=recovery,
                    observation=None,
                    pending=(
                        recovery is not None
                        and any(not future.done() for future in recovery.futures)
                    ),
                )
            lane = self._process_lanes.get(queue_key)
            if lane is None:
                lane = HwpProcessLane(queue_key, self._process_queue_limit)
                self._process_lanes[queue_key] = lane
            operation_context = self._mutation_context(
                session_id,
                mutation=mutation,
                save_operation=save_operation,
            )

        def invoke_in_public_scope() -> T:
            if public_dispatch_scope is None:
                return invoke()
            return public_dispatch_scope.run(lane, invoke)

        watch: ProcessExitWatch | None = (
            open_process_exit_watch(queue_key) if queue_key > 0 else None
        )
        mutation_watchdog = self._mutation_watchdog(
            operation_context,
            started_at=started,
        )
        observer_poll_seconds = (
            None if mutation_watchdog is None else mutation_watchdog.poll_seconds
        )
        process_poll_seconds = (
            _PROCESS_EXIT_POLL_SECONDS
            if observer_poll_seconds is None
            else max(_PROCESS_EXIT_POLL_SECONDS, observer_poll_seconds)
        )
        dialog_probe_seconds = (
            _MODAL_DIAGNOSTIC_INTERVAL_SECONDS
            if observer_poll_seconds is None
            else max(
                _MODAL_DIAGNOSTIC_INTERVAL_SECONDS,
                observer_poll_seconds * 2,
            )
        )
        next_dialog_probe = started + max(
            _MODAL_DIAGNOSTIC_DELAY_SECONDS,
            dialog_probe_seconds,
        )
        acquired = False
        future: Future[T] | None = None
        try:
            while not acquired:
                remaining = call_timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    with self._lifecycle_lock:
                        recovery_pending = queue_key in self._poisoned_processes
                    if recovery_pending:
                        _ = self._recover_poisoned_process(
                            queue_key,
                            session_id,
                            allow_unresolved_read=False,
                        )
                    save_state = self._mark_save_uncertain(
                        operation_context,
                        source="queue_deadline",
                        watchdog=mutation_watchdog,
                    )
                    raise _deadline_error(
                        mutation=mutation,
                        phase="queue_wait",
                        process_lane_isolation=False,
                        save_state=save_state,
                        watchdog=(
                            None
                            if mutation_watchdog is None
                            else mutation_watchdog.snapshot()
                        ),
                    )
                wait_seconds = min(
                    (
                        process_poll_seconds
                        if watch is not None
                        else dialog_probe_seconds
                    ),
                    remaining,
                )
                acquired = lane.acquire(timeout=wait_seconds)
                if acquired:
                    break
                if watch is not None and watch.exited():
                    self._invalidate_process(
                        queue_key,
                    )
                    diagnostic = stalled_hwp_call_diagnostic(
                        self._windows,
                        queue_key,
                    )
                    raise _process_lost_error(
                        process_id=queue_key,
                        mutation=mutation,
                        phase="queue_wait",
                        diagnostic=diagnostic,
                    )
                now = time.monotonic()
                if now >= next_dialog_probe:
                    diagnostic = stalled_hwp_call_diagnostic(
                        self._windows,
                        queue_key,
                    )
                    next_dialog_probe = now + dialog_probe_seconds
                    if diagnostic is not None:
                        _ = self._quarantine_unresolved_process(
                            queue_key,
                            lane,
                            session_id=session_id,
                            mutation=mutation,
                            phase="queue_wait",
                            operation_context=operation_context,
                            future=None,
                        )
                        raise _modal_dialog_error(
                            mutation=mutation,
                            phase="queue_wait",
                            diagnostic=diagnostic,
                        )
            signal = (
                None
                if operation_context is None
                else self._events.get(operation_context.process_id)
            )

            def arm_mutation_observer() -> None:
                # 레인 워커가 이 작업을 실제로 집어든 순간이다. 큐·락 대기는
                # 대상 프로세스가 굼뜬 것이 아니라 우리 쪽 직렬화이므로, 실행
                # 구간의 마감은 여기서부터 잰다.
                progress.mark_execution_start()
                if operation_context is None:
                    return
                operation_context.arm_watchdog_baseline()
                if mutation_watchdog is not None:
                    mutation_watchdog.arm(
                        started_at=time.monotonic(),
                        after_event_sequence=(
                            signal.sequence() if signal is not None else 0
                        ),
                    )

            with self._lifecycle_lock:
                if self._closed:
                    raise HwpLiveError("한컴 브리지가 이미 종료되었습니다")
                if mutation and queue_key in self._poisoned_processes:
                    recovery = self._poison_recoveries.get(queue_key)
                    raise _poison_recovery_error(
                        process_id=queue_key,
                        recovery=recovery,
                        observation=None,
                        pending=(
                            recovery is not None
                            and any(not tracked.done() for tracked in recovery.futures)
                        ),
                    )
                # 컨트롤러 작업이 레인에 올라가는 유일한 지점이다. 여기서만
                # 표시하고, close 의 스냅샷과 같은 _lifecycle_lock 아래에서
                # 표시하므로 "제출은 했는데 close 가 미배포로 읽는" 경합이
                # 생기지 않는다. 제출 직전에 적는 것은 의도한 것이다 —
                # lane.submit 이 실패해도 그 레인은 이미 닫혔거나 poison 된
                # 상태라 종료가 안전한 쪽으로 기운다.
                self._worked_lanes.add(queue_key)
                future = lane.submit(
                    invoke_in_public_scope,
                    context=operation_context,
                    on_start=arm_mutation_observer,
                )
                tracked_future = cast(Future[object], future)
                self._process_futures.setdefault(queue_key, set()).add(tracked_future)
                if mutation:
                    self._mutation_futures.setdefault(queue_key, set()).add(
                        tracked_future
                    )

            def release_process_future(_: Future[T]) -> None:
                lane.release()
                with self._lifecycle_lock:
                    tracked = self._process_futures.get(queue_key)
                    if tracked is None:
                        return
                    tracked.discard(tracked_future)
                    if not tracked:
                        del self._process_futures[queue_key]
                    mutation_tracked = self._mutation_futures.get(queue_key)
                    if mutation_tracked is None:
                        return
                    mutation_tracked.discard(tracked_future)
                    if not mutation_tracked:
                        del self._mutation_futures[queue_key]

            future.add_done_callback(release_process_future)
            acquired = False
            # 실행 구간의 마감은 "호출을 시작한 시각" 이 아니라 "마지막으로
            # 확인된 진행" 에서 잰다. 한 번의 COM 호출 안에서 여러 네이티브
            # 배치가 도는 작업(대형 표 삽입 등)은 배치가 끝날 때마다 살아
            # 있다는 사실을 스스로 증명하므로, 그 증명이 오는 동안에는
            # 죽었다고 판정하지 않는다.
            #
            # 진짜 행(hang)을 늦게 잡게 되지는 않는다. 판정 기준이
            # "시작 후 N초" 에서 "마지막 진행 후 N초" 로 바뀔 뿐이라, 멈춘
            # 시점부터 재는 검출 지연은 그대로다. 대신 전체 벽시계 시간은
            # 실제 작업량만큼 늘어난다 — 늘어나는 폭은 진행 보고 횟수가
            # 유한하다는 사실로 묶인다.
            #
            # 구간의 크기가 미리 선언돼 있으면(네이티브 호출 한 번은 안에서
            # 진행을 알릴 수 없다) 그 작업량만큼 더 기다린다. 그 대가는
            # _native_work_allowance_seconds 에 적어 두었다.
            observed_beats = 0
            allowance = call_timeout_seconds
            segment_base = started
            while True:
                beat = progress.mark()
                if beat.count != observed_beats:
                    observed_beats = beat.count
                    if mutation_watchdog is not None:
                        mutation_watchdog.arm(
                            started_at=beat.at,
                            after_event_sequence=(
                                signal.sequence() if signal is not None else 0
                            ),
                        )
                # 매 회차 기준을 다시 계산한다. 상태로 들고 다니면 실행 시작과
                # 진행 표식 중 어느 것이 최신인지가 회차마다 어긋난다.
                segment_base = max(started, beat.started_at, beat.at)
                allowance = _native_work_allowance_seconds(
                    call_timeout_seconds,
                    beat.declaration,
                )
                remaining = allowance - (time.monotonic() - segment_base)
                if remaining <= 0:
                    if future.done():
                        return future.result()
                    queued = future.cancel()
                    phase = "queued" if queued else "running"
                    save_state = self._mark_save_uncertain(
                        operation_context,
                        source="com_deadline",
                        watchdog=mutation_watchdog,
                    )
                    watchdog_observation = (
                        None
                        if mutation_watchdog is None
                        else mutation_watchdog.snapshot()
                    )
                    if not queued:
                        if not self._quarantine_unresolved_process(
                            queue_key,
                            lane,
                            session_id=session_id,
                            mutation=mutation,
                            phase=phase,
                            operation_context=operation_context,
                            future=tracked_future,
                        ):
                            return future.result()
                    diagnostic = (
                        stalled_hwp_call_diagnostic(self._windows, queue_key)
                        if time.monotonic() >= next_dialog_probe
                        else None
                    )
                    if diagnostic is not None:
                        raise _modal_dialog_error(
                            mutation=mutation,
                            phase=phase,
                            diagnostic=diagnostic,
                            save_state=save_state,
                            watchdog=watchdog_observation,
                        )
                    raise _deadline_error(
                        mutation=mutation,
                        phase=phase,
                        process_lane_isolation=not queued,
                        save_state=save_state,
                        watchdog=watchdog_observation,
                        progress=progress.mark(),
                        allowance_seconds=allowance,
                    )
                wait_seconds = min(
                    (
                        process_poll_seconds
                        if watch is not None
                        else dialog_probe_seconds
                    ),
                    remaining,
                )
                try:
                    return future.result(timeout=wait_seconds)
                except FutureTimeoutError:
                    if future.done():
                        return future.result()
                except CancelledError as error:
                    with self._lifecycle_lock:
                        recovery = self._poison_recoveries.get(queue_key)
                    if recovery is None:
                        raise
                    raise _poison_recovery_error(
                        process_id=queue_key,
                        recovery=recovery,
                        observation=None,
                        pending=any(not tracked.done() for tracked in recovery.futures),
                    ) from error
                watchdog_observation = (
                    None if mutation_watchdog is None else mutation_watchdog.snapshot()
                )
                if (
                    watchdog_observation is not None
                    and watchdog_observation.terminal_uncertain
                    # 워치독의 종료선은 이 호출의 허용치를 모른다(생성 시점에
                    # min(180, call_timeout) 로 고정된다). 그 사이에 진행이
                    # 왔거나, 선언된 작업량이 아직 남아 있으면 관측이 낡은
                    # 것이므로 죽었다고 판정하지 않는다. 두 경우 모두
                    # segment_base 와 allowance 가 이미 표현하고 있다.
                    and time.monotonic() - segment_base >= allowance
                ):
                    if future.done():
                        return future.result()
                    queued = future.cancel()
                    phase = "queued" if queued else "running"
                    save_state = self._mark_save_uncertain(
                        operation_context,
                        source="watchdog_terminal",
                        watchdog=mutation_watchdog,
                    )
                    if not queued:
                        if not self._quarantine_unresolved_process(
                            queue_key,
                            lane,
                            session_id=session_id,
                            mutation=mutation,
                            phase=phase,
                            operation_context=operation_context,
                            future=tracked_future,
                        ):
                            return future.result()
                    raise _deadline_error(
                        mutation=mutation,
                        phase=phase,
                        process_lane_isolation=not queued,
                        save_state=save_state,
                        watchdog=watchdog_observation,
                        progress=progress.mark(),
                        allowance_seconds=allowance,
                    )
                if watch is not None and watch.exited():
                    self._invalidate_process(
                        queue_key,
                    )
                    diagnostic = stalled_hwp_call_diagnostic(
                        self._windows,
                        queue_key,
                    )
                    raise _process_lost_error(
                        process_id=queue_key,
                        mutation=mutation,
                        phase="running",
                        diagnostic=diagnostic,
                    )
                now = time.monotonic()
                if now >= next_dialog_probe:
                    diagnostic = stalled_hwp_call_diagnostic(
                        self._windows,
                        queue_key,
                    )
                    next_dialog_probe = now + dialog_probe_seconds
                    if diagnostic is not None:
                        if future.done():
                            return future.result()
                        queued = future.cancel()
                        if not queued:
                            if not self._quarantine_unresolved_process(
                                queue_key,
                                lane,
                                session_id=session_id,
                                mutation=mutation,
                                phase="running",
                                operation_context=operation_context,
                                future=tracked_future,
                            ):
                                return future.result()
                        save_state = self._mark_save_uncertain(
                            operation_context,
                            source="modal_dialog",
                            watchdog=mutation_watchdog,
                        )
                        raise _modal_dialog_error(
                            mutation=mutation,
                            phase="running",
                            diagnostic=diagnostic,
                            save_state=save_state,
                            watchdog=(
                                None
                                if mutation_watchdog is None
                                else mutation_watchdog.snapshot()
                            ),
                        )
        except HwpTargetProcessLostError:
            _ = self._mark_save_uncertain(
                operation_context,
                source="process_lost",
                watchdog=mutation_watchdog,
            )
            raise
        except BaseException as error:
            state = self._context_save_state(operation_context)
            if (
                future is not None
                and not future.cancelled()
                and state is not None
                and not state.snapshot().terminal
            ):
                _ = self._mark_save_uncertain(
                    operation_context,
                    source="operation_exception",
                    watchdog=mutation_watchdog,
                )
            if is_hwp_target_process_lost_error(error):
                _ = self._mark_save_uncertain(
                    operation_context,
                    source="process_lost",
                    watchdog=mutation_watchdog,
                )
                self._invalidate_process(
                    queue_key,
                )
                diagnostic = stalled_hwp_call_diagnostic(
                    self._windows, queue_key
                ) or popup_diagnostic(
                    self._windows,
                    target_process_id=queue_key,
                )
                lost_error = _process_lost_error(
                    process_id=queue_key,
                    mutation=mutation,
                    phase="running" if future is not None else "queued",
                    diagnostic=diagnostic,
                )
                raise HwpTargetProcessLostError(
                    f"{lost_error.reason}; 원래 COM 오류: {error}",
                    mutation_started=lost_error.mutation_started,
                ) from error
            if not isinstance(error, HwpLiveError):
                raise
            if (
                type(error) is not HwpLiveError
                or "worker_isolation_required=true" in error.reason
                or "process_lane_isolation_required=true" in error.reason
            ):
                raise
            popup = popup_diagnostic(
                self._windows,
                target_process_id=queue_key,
            )
            if popup is None:
                raise
            raise HwpLiveError(
                f"{error.reason}; 감지된 한컴 오류 창: {popup}",
                mutation_started=error.mutation_started,
            ) from error
        finally:
            if acquired:
                lane.release()
            if mutation_watchdog is not None:
                mutation_watchdog.stop()
            self._release_mutation_context(operation_context)
            if watch is not None:
                watch.close()

    def _schedule_recovery_probe(
        self,
        process_id: int,
        session_id: str,
        lane: HwpProcessLane,
    ) -> None:
        with self._lifecycle_lock:
            if process_id not in self._poisoned_processes:
                return
            target = self._session_watch_targets.get(session_id)
        if target is None:
            return

        def probe_known_document() -> bool:
            previous_process_id = self._thread_state.process_id
            self._thread_state.process_id = process_id
            controller = self._bridge_controller()
            try:
                with native_dispatch_operation_scope():
                    existing = controller.session_for_selector(target.document.selector)
                    if existing is None or existing.session_id != session_id:
                        return False
                    connected = controller.connect_deferred(target.document.selector)
                    return (
                        connected.session_id == session_id
                        and connected.document.document_id == target.document_id
                        and connected.document.window_handle == target.window_handle
                        and ntpath.normcase(
                            ntpath.normpath(connected.document.full_name)
                        )
                        == ntpath.normcase(ntpath.normpath(target.document.full_name))
                    )
            finally:
                try:
                    controller.restore_activation()
                finally:
                    self._thread_state.process_id = previous_process_id

        try:
            probe = lane.submit(probe_known_document)
        except RuntimeError:
            return

        def finish_recovery_probe(completed: Future[bool]) -> None:
            if completed.cancelled():
                return
            try:
                if not completed.result():
                    return
            except BaseException as error:
                if is_hwp_target_process_lost_error(error):
                    self._invalidate_process(process_id)
                return
            with self._lifecycle_lock:
                recovery = self._poison_recoveries.get(process_id)
                if recovery is not None:
                    self._recovery_identity_confirmed.add(process_id)
            if recovery is None:
                return
            readback_resolves = not recovery.reconcile_required
            if recovery.reconcile_required:
                readback_resolves = all(
                    tracked.cancelled()
                    or (tracked.done() and tracked.exception() is None)
                    for tracked in recovery.futures
                )
            try:
                _ = self._recover_poisoned_process(
                    process_id,
                    session_id,
                    allow_unresolved_read=True,
                    readback_succeeded=readback_resolves,
                )
            except HwpLiveError:
                return

        probe.add_done_callback(finish_recovery_probe)

    def _quarantine_unresolved_process(
        self,
        process_id: int,
        lane: HwpProcessLane,
        *,
        session_id: str | None,
        mutation: bool,
        phase: str,
        operation_context: HwpLaneOperationContext | None,
        future: Future[object] | None,
    ) -> bool:
        if operation_context is not None:
            _ = operation_context.watchdog_metadata_changed
        observation = self._poison_recovery_observation(
            process_id,
            session_id,
        )
        if observation.process_alive is False:
            self._invalidate_process(process_id)
            raise _process_lost_error(
                process_id=process_id,
                mutation=mutation,
                phase=phase,
                diagnostic=None,
            )
        if future is not None and future.done():
            return False
        first_quarantine = self._poison_process_lane(
            process_id,
            lane,
            operation_context=operation_context,
            reconcile_required=mutation and phase == "running",
        )
        if first_quarantine and session_id is not None:
            self._schedule_recovery_probe(process_id, session_id, lane)
        return True

    def _poison_process_lane(
        self,
        process_id: int,
        lane: HwpProcessLane,
        *,
        operation_context: HwpLaneOperationContext | None,
        reconcile_required: bool,
    ) -> bool:
        operation_id = _OPERATION_RECOVERY_ID.get()
        with self._lifecycle_lock:
            mutation_futures = tuple(self._mutation_futures.get(process_id, ()))
            active_futures = tuple(self._process_futures.get(process_id, ()))
            existing = self._poison_recoveries.get(process_id)
            if existing is None:
                self._recovery_identity_confirmed.discard(process_id)
                self._recovery_outcome_confirmed.discard(process_id)
            contexts = () if operation_context is None else (operation_context,)
            if existing is not None:
                active_futures = tuple(
                    dict.fromkeys((*existing.futures, *active_futures))
                )
                contexts = tuple(dict.fromkeys((*existing.contexts, *contexts)))
                reconcile_required = reconcile_required or existing.reconcile_required
            operation_ids = frozenset(
                (
                    *(() if existing is None else existing.operation_ids),
                    *(() if operation_id is None else (operation_id,)),
                )
            )
            reconcile_required = reconcile_required or any(
                future.running() for future in mutation_futures
            )
            self._poison_recoveries[process_id] = _PoisonedLaneRecovery(
                futures=active_futures,
                contexts=contexts,
                reconcile_required=reconcile_required,
                operation_ids=operation_ids,
            )
            self._poisoned_processes.add(process_id)
            if self._process_lanes.get(process_id) is None:
                self._process_lanes[process_id] = lane
        for mutation_future in mutation_futures:
            if not mutation_future.running():
                _ = mutation_future.cancel()
        return existing is None

    def _invalidate_process(
        self,
        process_id: int,
    ) -> None:
        if process_id < 1:
            return
        with self._lifecycle_lock:
            session_ids = self._process_sessions.pop(process_id, set())
            session_ids.update(
                session_id
                for session_id, session_process_id in self._session_processes.items()
                if session_process_id == process_id
            )
            for session_id in session_ids:
                _ = self._session_processes.pop(session_id, None)
                _ = self._session_watch_targets.pop(session_id, None)
                _ = self._style_revisions.pop(session_id, None)
                self._transient_sessions.discard(session_id)
                self._deferred_transient_releases.discard(session_id)
                self._lost_sessions[session_id] = process_id
            signal = self._events.pop(process_id, None)
            lane = self._process_lanes.pop(process_id, None)
            _ = self._controllers.pop(process_id, None)
            _ = self._poison_recoveries.pop(process_id, None)
            self._recovery_identity_confirmed.discard(process_id)
            self._recovery_outcome_confirmed.discard(process_id)
            _ = self._mutation_futures.pop(process_id, None)
            _ = self._process_futures.pop(process_id, None)
            self._poisoned_processes.discard(process_id)
        self._cache.clear()
        self._fast_inspections.clear()
        forget_cached_content_signatures()
        if lane is not None:
            lane.poison()
        if signal is not None:
            try:
                signal.stop()
            except (HwpLiveError, OSError, RuntimeError, ValueError):
                pass

    @override
    def _set_connection_lifetime(
        self,
        session_id: str,
        *,
        persistent: bool | None,
        created: bool,
    ) -> None:
        with self._lifecycle_lock:
            if persistent is False:
                if created:
                    self._transient_sessions.add(session_id)
                return
            self._transient_sessions.discard(session_id)
            self._deferred_transient_releases.discard(session_id)

    def _registered_lane_controller(
        self,
        process_id: int,
    ) -> LiveHwpController | None:
        """레인 ``process_id`` 의 작업이 쓰게 될 이미 존재하는 컨트롤러.

        factory 가 있는데 그 pid 자리가 비어 있으면 아직 만들어지지 않은
        것이므로 None 이다. factory 가 없으면 빈 자리는 언제나 시드가 맡는다 —
        이때 ``_controllers`` 에는 {0: seed} 만 남으므로, "pid 자리에 컨트롤러가
        없다 = 그 레인은 아무 컨트롤러도 안 쓴다"는 판정은 틀린다.
        """
        controller = self._controllers.get(process_id)
        if controller is not None:
            return controller
        return None if self._controller_factory is not None else self._controller

    # 여기는 컨트롤러를 "가져가는" 곳일 뿐이다. 순수 메모리 조회
    # (connection_moniker, current_session 등)도 이 문을 지나므로, 여기서
    # 표시하면 COM 을 한 번도 안 쓴 컨트롤러까지 배포된 것으로 셈해진다.
    # 표시는 _call 의 레인 제출 지점에서만 한다.
    @override
    def _bridge_controller(self) -> LiveHwpController:
        process_id = self._thread_state.process_id
        controller = self._registered_lane_controller(process_id)
        if controller is not None:
            return controller
        factory = self._controller_factory
        if factory is None:
            # _registered_lane_controller 가 factory 없는 빈 자리를 이미 시드로
            # 메운다. 여기 오면 그 규칙이 깨진 것이다.
            raise HwpLiveError("한컴 브리지 컨트롤러를 해석하지 못했습니다")
        controller = factory()
        self._controllers[process_id] = controller
        return controller

    @override
    def _known_connection(self, selector: str | None) -> ConnectedDocument | None:
        if selector is None:
            return None
        with self._lifecycle_lock:
            candidates = tuple(
                (session_id, target)
                for session_id, target in self._session_watch_targets.items()
                if self._session_processes.get(session_id) == target.process_id
            )
        try:
            selected = select_operation_document(
                OpenDocumentList(
                    documents=tuple(target.document for _, target in candidates)
                ),
                selector,
            )
        except HwpLiveError:
            return None
        matches = tuple(
            (session_id, target)
            for session_id, target in candidates
            if target.document.selector == selected.selector
        )
        if len(matches) != 1:
            return None
        session_id, target = matches[0]
        return ConnectedDocument(session_id=session_id, document=target.document)

    @override
    def _style_state_token(self, session_id: str) -> str:
        sequence = self._event_signal(session_id).sequence()
        with self._lifecycle_lock:
            revision = self._style_revisions.get(session_id, 0)
        return f"{sequence:016x}:{revision:016x}"

    @override
    def _call_style_read(
        self,
        operation: Callable[[LiveHwpController], T],
        *,
        session_id: str,
        degrade: Callable[[T], T] | None = None,
    ) -> T:
        # 이 토큰은 "문서가 바뀌었다"가 아니라 "그 프로세스에서 뭔가 일어났다"
        # 이다. _style_state_token 은 WinEvent 알림 시퀀스를 그대로 쓰고
        # (_RELEVANT_EVENTS 에는 FOREGROUND/CREATE/DESTROY/SHOW/FOCUS/NAMECHANGE
        # 처럼 본문 내용과 무관한 UI 이벤트가 들어 있다), 훅 스레드는 20ms 주기로
        # 펌프하므로 한 번의 읽기가 만든 이벤트가 다음 시도 도중에 도착할 수도
        # 있다. 스타일 읽기 자체가 선택 영역을 옮겼다 되돌리고(inspect_styles 의
        # MoveSelRight/select_text) 무거운 HWPML2X 덤프를 뜨므로, 한/글이 그
        # 조작에 UI 이벤트로 답하면 읽기가 스스로 토큰을 밀어 올린다. 실기에서
        # 커서·쪽 수·수정 여부가 시작과 동일한데도 "문서 상태가 연속으로
        # 변경되었습니다"로 두 번 거부한 것이 이 모양이다.
        #
        # 진짜 문서 상태 검증은 inspect_styles 가 이미 한다 — 선택 영역·커서·
        # IsModified 를 읽기 전후로 비교해 다르면 스스로 거부한다. 그 검증을
        # 통과한 값을 토큰이 움직였다는 이유만으로 버리면 스타일 작업 자체가
        # 불가능해진다. 그래서 재시도가 끝나면 거부하는 대신 읽은 값을 주고
        # "최신이라고 보증하지 못한다"고 표시한다. 표시할 자리가 없는 호출자
        # (degrade=None)만 종전처럼 거부한다.
        def invoke() -> T:
            controller = self._bridge_controller()
            attempts = 2
            for attempt in range(attempts):
                state_token = self._style_state_token(session_id)
                controller.set_style_state_token(session_id, state_token)
                result = operation(controller)
                if self._style_state_token(session_id) == state_token:
                    return result
                if attempt + 1 == attempts and degrade is not None:
                    return degrade(result)
            raise HwpLiveError(
                "스타일을 읽는 동안 한컴 문서 상태가 연속으로 변경되었습니다"
            )

        return self._call_recovery_read(invoke, session_id=session_id)

    @override
    def _bridge_process_id(self, window_handle: int) -> int:
        return self._windows.read(window_handle).process_id

    @override
    def _call_mutation(
        self,
        operation: Callable[[], T],
        *,
        session_id: str | None = None,
        process_id: int | None = None,
        save_operation: bool = False,
    ) -> T:
        def invoke() -> T:
            self._fast_inspections.clear()
            # 쓰기 **앞**의 무효화다. 지우지 마라 — 이건 중복이 아니다.
            #
            # 쓰기 경로 중에는 쓰기 직전에 읽은 지문으로 "내가 마지막으로 본
            # 문서가 맞는가"를 판정하는 관문이 있다. G04 apply_page_plan 이
            # 그것이다(hwp_live_session_operation.py 의 STALE_GROUNDING):
            # G01 ground_document 가 관측한 revision 해시와 지금 지문을
            # 대조해, 다르면 계획을 거부한다.
            #
            # 그런데 그 G01(`_call_recovery_read`)이 바로 이 워커에서 지문
            # 캐시를 채우고 버리지 않는 유일한 읽기 경로다. 그래서 이 줄이
            # 없으면 G04 의 대조가 엔진이 아니라 G01 이 남긴 자기 값을
            # 되받고, 그 사이 사용자가 한/글에서 직접 고친 문서도 **항상
            # 일치**로 통과한다. 그 관문은 이 워커 밖에서 일어난 변경을
            # 잡는 유일한 장치이고, 변경 이벤트 브리지가 죽어 있으면
            # note_native_window_content_change 도 뜨지 않는다.
            #
            # 쓰기 원시 호출들이 각자 자기 창을 무효화하는 것은 사실이지만
            # 그것은 **쓰기 시점**이라 쓰기 앞의 판정보다 늦다. 아래 finally
            # 는 호출이 끝난 뒤라 더 늦다. 쓰기 앞의 판정을 엔진에 붙여 두는
            # 것은 이 줄뿐이다.
            forget_cached_content_signatures()
            try:
                return operation()
            finally:
                self._fast_inspections.clear()
                forget_cached_content_signatures()
                if session_id is not None:
                    with self._lifecycle_lock:
                        self._style_revisions[session_id] = (
                            self._style_revisions.get(session_id, 0) + 1
                        )

        return self._call(
            invoke,
            session_id=session_id,
            process_id=process_id,
            mutation=True,
            save_operation=save_operation,
        )

    @override
    def _activate_connection(self, connected: ConnectedDocument) -> None:
        self._cache.clear()
        self._fast_inspections.clear()
        forget_cached_content_signatures()
        # A rebind is the one moment the worker stops knowing which document a
        # window holds, so the remembered "the engine cannot serialise this"
        # stops being about anything. Writes deliberately do not clear it.
        #
        # The window being activated is what is passed: the durable ledger is
        # shared by every worker on this machine, and clearing all of it from
        # here deleted the memory of processes servicing other 한/글 instances.
        # Memory is still cleared whole -- forgetting too much in this process
        # only costs this process an engine attempt.
        forget_content_signature_refusals_on_rebind(connected.document.window_handle)
        # ...but the first activation of a window is not a rebind: there was no
        # document this worker had bound in it and now stopped knowing. That is
        # the one moment a previous worker's durable refusal may be adopted, and
        # this is the once-per-window call that does it. Every later activation
        # of the same window is a real rebind and clears memory and that
        # window's entry together (the window set inside
        # ``forget_content_signature_refusals_on_rebind`` is what tells them
        # apart).
        #
        # Only this window's entry is read and judged. ``_batch_for_window``
        # (hwp_live_native_batch.py :1615) turns a handle into a dispatch for
        # whatever process ``GetWindowThreadProcessId`` names, so judging the
        # whole ledger from here sent a revision-token read into 한/글
        # instances this worker never bound, and dropped their entry on a
        # mismatch. Their entries are judged by their own worker's activation,
        # under this same rule.
        #
        # The answer does not move, because an entry is adopted only onto the
        # document it was measured on: the durable copy carries the session tag
        # and the revision token's two hashes, and anything but a full match --
        # a swapped document, a restarted 한/글, an edit since, an unreadable
        # token -- drops the entry and adopts nothing, leaving the engine to be
        # asked exactly as before. What is saved is the case where the document
        # really is untouched: today's first edit after a worker restart spends
        # the 17.2s serialisation attempt to be told it is unsignable, where
        # this spends the 0.024s revision token and returns the same empty
        # signature with the same notice. Why identity has to be proved here,
        # and may not be inferred from size, is on
        # ``load_persisted_refusals_once``.
        #
        # It cannot raise: warming a cache must never fail an activation.
        _ = load_persisted_refusals_once(connected.document.window_handle)
        window = self._windows.read(connected.document.window_handle)
        session_id = connected.session_id
        with self._lifecycle_lock:
            _ = self._lost_sessions.pop(session_id, None)
            _ = self._style_revisions.setdefault(session_id, 0)
            self._session_watch_targets[session_id] = _DocumentWatchTarget(
                process_id=window.process_id,
                window_handle=connected.document.window_handle,
                document_path=Path(connected.document.full_name),
                document_id=connected.document.document_id,
                document=connected.document,
            )
        if session_id in self._session_processes:
            self._observe_content_changes(
                self._events.get(self._session_processes[session_id]),
                connected.document.window_handle,
            )
            return
        moniker_name = self._bridge_controller().connection_moniker(
            connected.session_id
        )
        process_id = window.process_id
        signal = self._events.get(process_id)
        if signal is None:
            signal = self._initial_event_signal
            if signal is None:
                signal = HybridChangeSignal()
            else:
                self._initial_event_signal = None
            try:
                signal.start(process_id, moniker_name)
            except (HwpLiveError, OSError, RuntimeError, ValueError):
                if not self._events and self._initial_event_signal is None:
                    self._initial_event_signal = signal
                raise
            self._events[process_id] = signal
        self._session_processes[session_id] = process_id
        self._process_sessions.setdefault(process_id, set()).add(session_id)
        self._observe_content_changes(signal, connected.document.window_handle)

    def _observe_content_changes(
        self,
        signal: ChangeSignal | None,
        window_handle: int,
    ) -> None:
        """Let a user's own typing age this worker's view of the document.

        Before this, only writes issued through the bridge invalidated the
        signature, so a document edited by hand kept answering from a stale
        cache. The registered observer drops the cached signature and revision
        token and bumps the window's freshness counter -- the counter is what
        keeps a formatting-only manual edit, whose recaptured token is
        bit-identical, from re-issuing a pre-edit graph epoch. The observer
        runs on the event hook thread and therefore must stay dictionary
        bookkeeping; re-reading the signature, rebuilding the graph, or
        persisting the counter here would put document or disk work on every
        keystroke (the counter is persisted at bind time instead).
        """
        if not isinstance(signal, ChangeObservable):
            return
        signal.observe_window(window_handle, note_native_window_content_change)

    @override
    def _clear_connection_state(self, session_id: str | None = None) -> None:
        try:
            if session_id is None:
                self._stop_all_events()
                with self._lifecycle_lock:
                    self._transient_sessions.clear()
                    self._deferred_transient_releases.clear()
                return
            with self._lifecycle_lock:
                self._transient_sessions.discard(session_id)
                self._deferred_transient_releases.discard(session_id)
            _ = self._lost_sessions.pop(session_id, None)
            watch_target = self._session_watch_targets.pop(session_id, None)
            process_id = self._session_processes.pop(session_id, None)
            if process_id is None:
                return
            # Sibling sessions keep the signal alive, so drop only this window's
            # invalidation observer instead of leaking it onto the shared hook.
            observed = self._events.get(process_id)
            if watch_target is not None and isinstance(observed, ChangeObservable):
                observed.forget_window(watch_target.window_handle)
            sessions = self._process_sessions.get(process_id)
            if sessions is not None:
                sessions.discard(session_id)
            if sessions:
                return
            _ = self._process_sessions.pop(process_id, None)
            signal = self._events.pop(process_id, None)
            if signal is not None:
                signal.stop()
            lane = self._process_lanes.pop(process_id, None)
            _ = self._poison_recoveries.pop(process_id, None)
            self._recovery_identity_confirmed.discard(process_id)
            self._recovery_outcome_confirmed.discard(process_id)
            _ = self._mutation_futures.pop(process_id, None)
            _ = self._process_futures.pop(process_id, None)
            self._poisoned_processes.discard(process_id)
            if lane is not None:
                lane.shutdown(wait=False)
        finally:
            self._cache.clear()
            self._fast_inspections.clear()
            forget_cached_content_signatures()
            with self._lifecycle_lock:
                if session_id is None:
                    self._style_revisions.clear()
                else:
                    _ = self._style_revisions.pop(session_id, None)

    def _release_transient_connection_now(self, session_id: str) -> bool:
        def operation() -> bool:
            controller = self._bridge_controller()
            try:
                return controller.release_transient(session_id)
            finally:
                self._clear_connection_state(session_id)

        return self._call(operation, session_id=session_id)

    def release_transient_connection(self, session_id: str) -> bool:
        with self._lifecycle_lock:
            if session_id not in self._transient_sessions:
                return False
            if self._save_close_blocker_locked(session_id) is not None:
                self._deferred_transient_releases.add(session_id)
                return False
        return self._release_transient_connection_now(session_id)

    def release_idle_references(self, process_ids: Collection[int]) -> None:
        with self._lifecycle_lock:
            controllers = tuple(
                (process_id, controller)
                for process_id in process_ids
                if (controller := self._controllers.get(process_id)) is not None
            )
        seen: set[int] = set()
        for process_id, controller in controllers:
            if id(controller) in seen:
                continue
            seen.add(id(controller))
            try:
                self._call(
                    controller.release_idle_references,
                    process_id=process_id,
                )
            except Exception:
                # Public-call cleanup is best effort. A later call on the same
                # process lane gets its own scoped cleanup opportunity.
                continue

    @override
    def disconnect(self, session_id: str | None = None) -> MutationResult:
        selected_session_id = session_id
        if selected_session_id is None:
            current = self._controller.current_session()
            selected_session_id = None if current is None else current.session_id
        if selected_session_id is None:
            with self._lifecycle_lock:
                controllers = tuple(self._controllers.values())
            current_session_ids = {
                current.session_id
                for controller in controllers
                if (current := controller.current_session()) is not None
            }
            if len(current_session_ids) == 1:
                selected_session_id = next(iter(current_session_ids))
        if selected_session_id is not None:
            with self._lifecycle_lock:
                blocker = self._save_close_blocker_locked(selected_session_id)
            if blocker is not None:
                raise self._save_close_error(blocker)

        def operation() -> MutationResult:
            controller = self._bridge_controller()
            current = controller.current_session()
            selected_session = (
                (None if current is None else current.session_id)
                if selected_session_id is None
                else selected_session_id
            )
            if selected_session is None:
                raise HwpLiveError("연결된 한컴 라이브 세션이 없습니다")
            with self._lifecycle_lock:
                blocker = self._save_close_blocker_locked(selected_session)
            if blocker is not None:
                raise self._save_close_error(blocker)
            try:
                return controller.disconnect(selected_session)
            finally:
                self._clear_connection_state(selected_session)

        return self._call(operation, session_id=selected_session_id)

    def _stop_all_events(self) -> None:
        signals = tuple(self._events.values())
        self._events.clear()
        self._process_sessions.clear()
        self._session_processes.clear()
        self._session_watch_targets.clear()
        first_error: HwpLiveError | OSError | RuntimeError | ValueError | None = None
        for signal in signals:
            try:
                signal.stop()
            except (HwpLiveError, OSError, RuntimeError, ValueError) as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def _event_signal(self, session_id: str) -> ChangeSignal:
        process_id = self._session_processes.get(session_id)
        signal = None if process_id is None else self._events.get(process_id)
        if signal is None:
            raise HwpLiveError("한컴 문서 세션의 프로세스 이벤트 감시가 없습니다")
        return signal

    def close(self) -> None:
        """이벤트를 멈추고 컨트롤러를 닫은 뒤 프로세스 레인을 회수한다.

        컨트롤러는 그 컨트롤러가 실제로 작업을 돌린 레인에서 닫는다. 레인
        스레드만 STA 를 열고(HwpProcessLane._run) HwpRotCatalog 의
        ``_initialized`` 는 스레드 지역이므로(hwp_live_rot.py:507-513, 924-931),
        다른 스레드에서 닫으면 CoUninitialize 도 래퍼 해제도 제 아파트에서
        일어나지 않는다. 어느 레인에도 작업이 제출된 적이 없는 컨트롤러 —
        전형적으로 ``__init__`` 이 pid 0 자리에 꽂아둔 뒤 한 번도 쓰이지 않은
        시드 — 만 그 자리에서 닫는다. 그런 컨트롤러의 close 는 COM 을 건드릴
        수 없다: 세션이 비어 restore_activation 이 즉시 빠져나가고
        (hwp_live_session_core.py:1452-1456, 1457-1484), HwpRotCatalog.close 는
        스레드 지역 ``_initialized`` 가 False 라 pythoncom 을 부르기 전에
        반환한다. 이를 위해 레인을 새로 만들면 그 워커가 즉시 pythoncom 을
        적재하고 아파트를 연다.

        한계 — 마감이 없는 구간이 둘 있다. 레인에 올린 정리 future 는
        ``call_timeout_seconds`` 로 끊고 시간이 지나면 레인을 poison 하지만,
        (1) 제자리 ``controller.close()`` 와 (2) 마지막
        ``lane.shutdown(wait=True)`` 의 스레드 join 은 무제한이다. ``close`` 는
        ``LiveHwpController`` Protocol 의 메서드라 임베더 구현이 영원히
        막히면 이 호출도 영원히 막히고, 컨트롤러 호출 안에서 돌아오지 않는
        레인 작업(예: hwp_live_edit_history.py:722)도 같은 join 을 붙든다.
        """
        with self._lifecycle_lock:
            if self._closed:
                return
            blocker = self._save_close_blocker_locked(None)
            if blocker is not None:
                raise self._save_close_error(blocker)
            self._closed = True
            lanes_by_process = dict(self._process_lanes)
            poisoned_processes = set(self._poisoned_processes)
            controllers = tuple(self._controllers.items())
            registered_controllers = dict(self._controllers)
            worked_lanes = sorted(self._worked_lanes)
            seed_controller = self._controller
            seed_serves_empty_slots = self._controller_factory is None

        def lane_controller(lane_process_id: int) -> LiveHwpController | None:
            # _registered_lane_controller 와 같은 규칙을 close 스냅샷 위에서
            # 다시 적용한다. 시드가 0이 아닌 pid 의 레인에서 쓰였어도
            # _controllers 에는 {0: seed} 만 남으므로 pid 대조만으로는 못 찾는다.
            registered = registered_controllers.get(lane_process_id)
            if registered is not None:
                return registered
            return seed_controller if seed_serves_empty_slots else None

        try:
            self._stop_all_events()
        finally:
            cleanup_tasks: list[tuple[HwpProcessLane, Future[None]]] = []
            direct_closes: list[LiveHwpController] = []
            seen_controllers: set[int] = set()
            try:
                for process_id, controller in controllers:
                    if (
                        process_id in poisoned_processes
                        or id(controller) in seen_controllers
                    ):
                        continue
                    seen_controllers.add(id(controller))
                    worked_on = tuple(
                        lane_process_id
                        for lane_process_id in worked_lanes
                        if lane_controller(lane_process_id) is controller
                    )
                    if not worked_on:
                        direct_closes.append(controller)
                        continue
                    lane = lanes_by_process.get(process_id)
                    if lane is None:
                        for lane_process_id in worked_on:
                            if lane_process_id in poisoned_processes:
                                continue
                            candidate = lanes_by_process.get(lane_process_id)
                            if candidate is not None:
                                lane = candidate
                                break
                    if lane is None:
                        # 작업을 돌린 레인이 모두 사라졌다(프로세스 무효화·연결
                        # 정리). 아파트를 되찾을 곳이 없으니 종전대로 등록된
                        # pid 에 레인을 만들어 그 위에서 닫는다.
                        lane = HwpProcessLane(
                            process_id,
                            self._process_queue_limit,
                        )
                        lanes_by_process[process_id] = lane
                    try:
                        cleanup_tasks.append((lane, lane.submit(controller.close)))
                    except RuntimeError:
                        lane.poison()
                for lane, cleanup in cleanup_tasks:
                    try:
                        cleanup.result(timeout=self._call_timeout_seconds)
                    except FutureTimeoutError:
                        lane.poison()
                    except (HwpLiveError, com_error, OSError, RuntimeError):
                        lane.poison()
                # 레인에 올린 정리를 모두 거둔 뒤에 제자리 close 를 한다.
                # 순서가 뒤바뀌면 여기서 새는 예외가 레인 future 를 기다리기
                # 전에 finally 로 뛰고, lane.shutdown 의 _cancel_pending 이 아직
                # 실행되지 않은 컨트롤러 close 를 취소해 버린다.
                for controller in direct_closes:
                    try:
                        controller.close()
                    except (HwpLiveError, com_error, OSError, RuntimeError):
                        # 격리할 레인이 없다. 나머지 정리를 계속한다.
                        continue
            finally:
                self._cache.clear()
                self._fast_inspections.clear()
                forget_cached_content_signatures()
                self._controllers.clear()
                self._worked_lanes.clear()
                self._deferred_transient_releases.clear()
                self._lost_sessions.clear()
                self._poison_recoveries.clear()
                self._recovery_identity_confirmed.clear()
                self._recovery_outcome_confirmed.clear()
                self._mutation_futures.clear()
                self._process_futures.clear()
                self._process_lanes.clear()
                self._poisoned_processes.clear()
                self._session_watch_targets.clear()
                self._style_revisions.clear()
                self._transient_sessions.clear()
                self._active_mutation_contexts.clear()
                self._uncertain_save_states.clear()
                for process_id, lane in tuple(lanes_by_process.items()):
                    lane.shutdown(wait=process_id not in poisoned_processes)

    def _inspect_and_refresh(
        self,
        session_id: str,
        page: int,
        after_revision: int,
    ) -> BridgeState:
        context, structure, window_handle = self._bridge_controller().inspect_state(
            session_id,
            page,
        )
        snapshot = BridgeSnapshot(
            context=context,
            structure=structure,
            window=self._windows.read(window_handle),
        )
        return self._cache.refresh(snapshot, after_revision)

    def window_state(self, window_handle: int) -> HancomWindowState:
        if window_handle < 1:
            raise HwpLiveError("한컴 창 핸들은 1 이상이어야 합니다")
        return self._windows.read(window_handle)

    def list_window_states(self) -> HancomWindowStateList:
        return self._windows.list_visible_hwp_windows()

    def inspect_dialog(self, dialog_window_handle: int) -> HancomPopupStructure:
        if dialog_window_handle < 1:
            raise HwpLiveError("한컴 팝업 핸들은 1 이상이어야 합니다")
        return self._windows.inspect_dialog(dialog_window_handle)

    def invoke_dialog_action(
        self,
        dialog_window_handle: int,
        control_id: int,
    ) -> HancomDialogActionResult:
        if dialog_window_handle < 1:
            raise HwpLiveError("한컴 팝업 핸들은 1 이상이어야 합니다")
        if control_id < -1 or control_id > 65_535:
            raise HwpLiveError("한컴 팝업 control_id는 -1~65535여야 합니다")
        # A modal dialog may be blocking the document COM lane itself. The
        # bounded popup reader revalidates the exact control before delivery;
        # queuing that recovery action behind document COM would deadlock it.
        return self._windows.invoke_dialog_action(dialog_window_handle, control_id)

    def dismiss_dialogs(self, window_handle: int) -> HancomDialogDismissResult:
        if window_handle < 1:
            raise HwpLiveError("한컴 창 핸들은 1 이상이어야 합니다")
        return self._call(
            lambda: self._windows.dismiss_dialogs(window_handle),
            process_id=self._bridge_process_id(window_handle),
        )

    def context(self, session_id: str) -> LiveContext:
        return self._call_recovery_read(
            lambda: self._bridge_controller().context(session_id),
            session_id=session_id,
        )

    def styles(self, session_id: str) -> StyleReadOutcome:
        controllers: list[LiveHwpController] = []

        def read(controller: LiveHwpController) -> StyleReadOutcome:
            if not controllers or controllers[-1] is not controller:
                controllers.append(controller)
            return StyleReadOutcome(styles=controller.styles(session_id))

        try:
            return self._call_style_read(
                read,
                session_id=session_id,
                degrade=_unverified_style_read,
            )
        finally:
            for controller in controllers:
                controller.finish_public_convention_read()

    def content_revision(self, session_id: str) -> str:
        return self._call_recovery_read(
            lambda: self._bridge_controller().content_revision(session_id),
            session_id=session_id,
        )

    def structure(self, session_id: str, page: int) -> DocumentStructure:
        return self._call_recovery_read(
            lambda: self._bridge_controller().structure(session_id, page),
            session_id=session_id,
        )

    def inspect_page_fast(
        self,
        session_id: str,
        page: int,
        *,
        include_cells: bool = False,
    ) -> FastPageInspection:
        def inspect() -> FastPageInspection:
            key = (session_id, page, include_cells)
            signal = self._event_signal(session_id)
            sequence = signal.sequence()
            cached = self._fast_inspections.get(key)
            if cached is not None and cached.sequence == sequence:
                if signal.sequence() == sequence:
                    return cached.inspection
            inspection = self._bridge_controller().inspect_page_fast(
                session_id,
                page,
                include_cells=include_cells,
            )
            if (
                key not in self._fast_inspections
                and len(self._fast_inspections) >= _FAST_INSPECTION_CACHE_LIMIT
            ):
                self._fast_inspections.clear()
            self._fast_inspections[key] = _FastInspectionCacheEntry(
                sequence=signal.sequence(),
                inspection=inspection,
            )
            return inspection

        return self._call_recovery_read(inspect, session_id=session_id)

    def has_cached_fast_inspections(self) -> bool:
        return bool(self._fast_inspections)

    def watch_state(
        self,
        session_id: str,
        page: int,
        after_revision: int,
        timeout_ms: int,
    ) -> BridgeState:
        if after_revision < 0:
            raise HwpLiveError("브리지 revision은 0 이상이어야 합니다")
        if timeout_ms < 0 or timeout_ms > 30_000:
            raise HwpLiveError("브리지 대기 시간은 0~30000ms여야 합니다")
        deadline = time.monotonic() + timeout_ms / 1_000
        signal = self._event_signal(session_id)
        while True:
            poll_started = time.monotonic()
            sequence = signal.sequence()
            state = self._call_recovery_read(
                lambda: self._inspect_and_refresh(
                    session_id,
                    page,
                    after_revision,
                ),
                session_id=session_id,
            )
            if state.revision > after_revision or time.monotonic() >= deadline:
                return state
            if signal.sequence() > sequence:
                continue
            next_poll = min(deadline, poll_started + 0.25)
            remaining = max(0.0, next_poll - time.monotonic())
            _ = signal.wait(sequence, remaining)

    def render_page(self, session_id: str, page: int, dpi: int) -> PreviewResult:
        return self._call_recovery_read(
            lambda: self._bridge_controller().render_page(session_id, page, dpi),
            session_id=session_id,
        )

    def ground_document(
        self,
        session_id: str,
        request: HwpGroundingRequest,
    ) -> HwpGroundingReport:
        def ground() -> HwpGroundingReport:
            controller = self._bridge_controller()
            # ground_document calls the controller's direct styles() method,
            # rather than HancomBridge.styles(). Seed the same per-session
            # token that the public style wrapper would otherwise install so
            # G01 can bind its revision to the live read.
            controller.set_style_state_token(
                session_id,
                self._style_state_token(session_id),
            )
            return controller.ground_document(session_id, request)

        return self._call_recovery_read(ground, session_id=session_id)

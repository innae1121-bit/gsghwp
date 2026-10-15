from __future__ import annotations

from typing import Final, Literal


type HwpPhaseTimingName = Literal[
    "validation",
    "planning_preflight",
    "checkpoint_before",
    "mutation",
    "readback",
    "checkpoint_after_history",
    "rollback",
    "total",
]
type HwpPhaseTimingSource = Literal["python", "native"]


_RPC_S_SERVER_UNAVAILABLE: Final = -2_147_023_174
_CO_E_OBJNOTCONNECTED: Final = -2_147_220_995
_RPC_E_DISCONNECTED: Final = -2_147_417_848
_TARGET_PROCESS_LOST_HRESULTS: Final = frozenset(
    {
        _RPC_S_SERVER_UNAVAILABLE,
        _CO_E_OBJNOTCONNECTED,
        _RPC_E_DISCONNECTED,
    }
)
_TARGET_PROCESS_LOST_MESSAGES: Final = (
    "rpc server is unavailable",
    "rpc 서버를 사용할 수 없습니다",
    "rpc 서버 사용 불가",
    "object is not connected to server",
    "object invoked has disconnected from its clients",
    "disconnected from its clients",
    "개체가 서버에 연결되어 있지 않습니다",
    "개체가 해당 클라이언트와 연결이 끊겼습니다",
)


class DocumentAutomationError(RuntimeError):
    """Expected input, template, or Hancom operation failure."""


class ProcessScanError(DocumentAutomationError):
    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HwpRuntimeSafetyError(DocumentAutomationError):
    reason: str

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HwpLiveError(DocumentAutomationError):
    reason: str
    mutation_started: bool | None
    # Whether running the very same call again is free. None means "no opinion",
    # which is every failure that has not thought about it, and leaves the
    # existing rule alone: retry is offered when nothing was mutated.
    #
    # False is for the failures where that rule is wrong — the document did not
    # change, so the mutation test says "retry", but something outside the
    # document did and a retry would spend it again. 한/글's own undo stack is
    # the case that exists: the engine reports a step applied, no probe can see
    # any difference in the document, and each retry quietly eats another step.
    #
    # It can only ever take retry-safety away (`transport_error_result` ands it
    # in), never grant it. Deliberately not named `retry_safe`: `NativeActionFailure`
    # already owns that attribute with a different meaning.
    safe_to_repeat: bool | None
    phase_timings: tuple[tuple[HwpPhaseTimingName, HwpPhaseTimingSource, int], ...]

    def __init__(
        self,
        reason: str,
        *,
        mutation_started: bool | None = None,
        safe_to_repeat: bool | None = None,
        phase_timings: tuple[
            tuple[HwpPhaseTimingName, HwpPhaseTimingSource, int], ...
        ] = (),
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.mutation_started = mutation_started
        self.safe_to_repeat = safe_to_repeat
        self.phase_timings = phase_timings


class HwpTargetProcessLostError(HwpLiveError):
    pass


class HwpTextPatchPreflightError(HwpLiveError):
    """Preserve the native index and location through the read-only boundary."""

    index: int
    code: str
    location: str

    def __init__(
        self,
        index: int,
        code: str,
        location: str,
        message: str,
        *,
        mutation_started: bool,
        safe_to_repeat: bool,
    ) -> None:
        super().__init__(
            f"text.patch preflight patches[{index}] {code}: {message}; location={location}",
            mutation_started=mutation_started,
            safe_to_repeat=safe_to_repeat,
        )
        self.index = index
        self.code = code
        self.location = location


def is_hwp_target_process_lost_error(error: BaseException) -> bool:
    current: BaseException | None = error
    visited: set[int] = set()
    signed = {str(value) for value in _TARGET_PROCESS_LOST_HRESULTS}
    unsigned = {str(value & 0xFFFF_FFFF) for value in _TARGET_PROCESS_LOST_HRESULTS}
    hexadecimal = {
        f"0x{value & 0xFFFF_FFFF:08x}" for value in _TARGET_PROCESS_LOST_HRESULTS
    }
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, HwpTargetProcessLostError):
            return True
        hresult = getattr(current, "hresult", None)
        if isinstance(hresult, int):
            normalized = hresult - 0x1_0000_0000 if hresult > 0x7FFF_FFFF else hresult
            if normalized in _TARGET_PROCESS_LOST_HRESULTS:
                return True
        message = str(current).casefold()
        if any(token in message for token in signed | unsigned | hexadecimal):
            return True
        if any(token in message for token in _TARGET_PROCESS_LOST_MESSAGES):
            return True
        current = current.__cause__ or current.__context__
    return False

from __future__ import annotations

# pyright: reportAny=false

import re
import time
from collections.abc import Callable
from ctypes import (
    POINTER,
    Structure,
    WinDLL,
    byref,
    cast as ctypes_cast,
    create_string_buffer,
    create_unicode_buffer,
)
from ctypes import c_int, c_long, c_void_p, c_wchar_p, wstring_at
from ctypes import wintypes
from dataclasses import dataclass
from importlib import import_module
from pathlib import PureWindowsPath
from typing import Literal, Protocol, final, runtime_checkable

from hwp_errors import HwpLiveError
from hwp_live_bridge_contract import (
    HancomDialogActionResult,
    HancomDialogControlState,
    HancomDialogDismissResult,
    HancomDialogState,
    HancomPopupStructure,
    HancomWindowChildState,
    HancomWindowState,
    HancomWindowStateList,
)
from hwp_live_uia import (
    PowerShellUiAutomationReader,
    UiAutomationControlState,
    UiAutomationReader,
)


WindowEnumerator = Callable[[int, int], bool]
DialogProcessState = Literal["alive", "exited", "unknown"]
DialogCorrelation = Literal[
    "same_process",
    "owned_target",
    "crash_reporter_target",
    "none",
]
DialogSafetyGrade = Literal[
    "a_manual_decision",
    "b_safe_information",
    "c_unknown",
]


@final
@dataclass(frozen=True, slots=True)
class DialogControlState:
    window_handle: int
    title: str
    class_name: str
    control_id: int
    style: int
    visible: bool
    enabled: bool


@final
@dataclass(frozen=True, slots=True)
class HwpDialogSnapshot:
    window_handle: int
    process_id: int
    owner_handle: int
    root_owner_handle: int
    title: str
    class_name: str
    visible: bool
    enabled: bool
    target_process_id: int
    target_process_state: DialogProcessState
    target_main_handle: int | None
    target_main_enabled: bool | None
    enabled_popup_handle: int
    correlation: DialogCorrelation
    controls: tuple[DialogControlState, ...]
    default_button_id: int | None
    structure_complete: bool


@final
@dataclass(frozen=True, slots=True)
class HwpDialogAssessment:
    snapshot: HwpDialogSnapshot
    safety_grade: DialogSafetyGrade
    auto_dismiss_allowed: bool
    structural_evidence: tuple[str, ...]
    user_action: str

    @property
    def window_handle(self) -> int:
        return self.snapshot.window_handle

    @property
    def process_id(self) -> int:
        return self.snapshot.process_id

    @property
    def title(self) -> str:
        return self.snapshot.title

    @property
    def class_name(self) -> str:
        return self.snapshot.class_name


@final
@dataclass(frozen=True, slots=True)
class HwpSafeDialogDismissResult:
    close_sent: bool
    dialog_gone: bool


@runtime_checkable
class Win32GuiModule(Protocol):
    def EnumChildWindows(
        self,
        window_handle: int,
        callback: WindowEnumerator,
        extra: int,
    ) -> None: ...

    def EnumWindows(self, callback: WindowEnumerator, extra: int) -> None: ...

    def GetClassName(self, window_handle: int) -> str: ...

    def GetAncestor(self, window_handle: int, flags: int) -> int: ...

    def GetDlgCtrlID(self, window_handle: int) -> int: ...

    def GetForegroundWindow(self) -> int: ...

    def GetWindow(self, window_handle: int, command: int) -> int: ...

    def GetWindowLong(self, window_handle: int, index: int) -> int: ...

    def GetWindowText(self, window_handle: int) -> str: ...

    def IsWindow(self, window_handle: int) -> bool: ...

    def IsWindowEnabled(self, window_handle: int) -> bool: ...

    def IsWindowVisible(self, window_handle: int) -> bool: ...

    def PostMessage(
        self,
        window_handle: int,
        message: int,
        wparam: int,
        lparam: int,
    ) -> None: ...

    def SendMessageTimeout(
        self,
        window_handle: int,
        message: int,
        wparam: int,
        lparam: int,
        flags: int,
        timeout: int,
    ) -> tuple[int, int]: ...


@runtime_checkable
class Win32ProcessModule(Protocol):
    def GetWindowThreadProcessId(self, window_handle: int) -> tuple[int, int]: ...


@runtime_checkable
class Win32ApiModule(Protocol):
    def CloseHandle(self, handle: int) -> None: ...

    def OpenProcess(
        self,
        desired_access: int,
        inherit_handle: bool,
        process_id: int,
    ) -> int: ...


@runtime_checkable
class Win32EventModule(Protocol):
    def WaitForSingleObject(self, handle: int, milliseconds: int) -> int: ...


@runtime_checkable
class PyWinTypesModule(Protocol):
    @property
    def error(self) -> type[Exception]: ...


class WindowStateReader(Protocol):
    def list_visible_hwp_windows(self) -> HancomWindowStateList: ...

    def read(self, window_handle: int) -> HancomWindowState: ...

    def dismiss_dialogs(self, window_handle: int) -> HancomDialogDismissResult: ...

    def inspect_dialog(self, dialog_window_handle: int) -> HancomPopupStructure: ...

    def invoke_dialog_action(
        self,
        dialog_window_handle: int,
        control_id: int,
    ) -> HancomDialogActionResult: ...


@runtime_checkable
class DialogSafetyReader(Protocol):
    def assess_dialog(
        self,
        window_handle: int,
        target_process_id: int,
        target_main_handle: int | None,
    ) -> HwpDialogAssessment: ...

    def list_correlated_orphan_dialogs(
        self,
        target_process_id: int,
    ) -> tuple[HwpDialogAssessment, ...]: ...

    def dismiss_safe_dialog(
        self,
        assessment: HwpDialogAssessment,
    ) -> HwpSafeDialogDismissResult: ...


def _load_gui() -> Win32GuiModule:
    module = import_module("win32gui")
    if not isinstance(module, Win32GuiModule):
        raise HwpLiveError("Windows 창 상태 API를 찾을 수 없습니다")
    return module


def _load_process() -> Win32ProcessModule:
    module = import_module("win32process")
    if not isinstance(module, Win32ProcessModule):
        raise HwpLiveError("Windows 프로세스 상태 API를 찾을 수 없습니다")
    return module


def _load_uia() -> UiAutomationReader:
    return PowerShellUiAutomationReader()


def _load_process_api() -> Win32ApiModule:
    module = import_module("win32api")
    if not isinstance(module, Win32ApiModule):
        raise HwpLiveError("Windows 프로세스 대기 API를 찾을 수 없습니다")
    return module


def _load_process_events() -> Win32EventModule:
    module = import_module("win32event")
    if not isinstance(module, Win32EventModule):
        raise HwpLiveError("Windows 프로세스 이벤트 API를 찾을 수 없습니다")
    return module


def _load_window_error() -> type[Exception]:
    module = import_module("pywintypes")
    if not isinstance(module, PyWinTypesModule):
        raise HwpLiveError("Windows 창 오류 형식을 찾을 수 없습니다")
    return module.error


_WINDOW_ERROR = _load_window_error()
_GW_OWNER = 4
_GW_ENABLEDPOPUP = 6
_GA_ROOTOWNER = 3
_GWL_STYLE = -16
_WS_SYSMENU = 0x0008_0000
_WM_CLOSE = 0x0010
_DM_GETDEFID = 0x0400
_DC_HASDEFID = 0x534B
_BM_CLICK = 0x00F5
_SMTO_ABORTIFHUNG = 0x0002
_SMTO_ERRORONEXIT = 0x0020
_MESSAGE_TIMEOUT_MILLISECONDS = 25
_ACTION_MESSAGE_TIMEOUT_MILLISECONDS = 1_000


def _dialog_control_role(class_name: str) -> str:
    normalized = class_name.casefold()
    if normalized == "button":
        return "button"
    if normalized in {"static", "text"}:
        return "label"
    if normalized == "edit":
        return "text_input"
    if normalized == "combobox":
        return "combo_box"
    if normalized == "listbox":
        return "list"
    if normalized == "systabcontrol32":
        return "tab_list"
    return "custom"


def _dialog_control_accelerator(title: str) -> str | None:
    match = re.search(r"&([A-Za-z0-9])", title)
    if match is None:
        match = re.search(r"\(([A-Za-z0-9])\)\s*$", title)
    return match.group(1).upper() if match is not None else None


def _public_dialog_control(
    control: DialogControlState,
    default_button_id: int | None,
) -> HancomDialogControlState:
    role = _dialog_control_role(control.class_name)
    return HancomDialogControlState(
        window_handle=control.window_handle,
        title=control.title,
        class_name=control.class_name,
        control_id=control.control_id,
        style=control.style,
        visible=control.visible,
        enabled=control.enabled,
        role=role,
        actionable=(
            role == "button"
            and control.control_id >= 0
            and control.visible
            and control.enabled
        ),
        default=(
            default_button_id is not None and control.control_id == default_button_id
        ),
        accelerator=_dialog_control_accelerator(control.title),
    )


def _public_uia_control(
    dialog_window_handle: int,
    control: UiAutomationControlState,
) -> HancomDialogControlState:
    return HancomDialogControlState(
        window_handle=dialog_window_handle,
        title=control.title,
        class_name=control.class_name,
        control_id=control.control_id,
        style=0,
        visible=control.visible,
        enabled=control.enabled,
        source="uia",
        automation_id=control.automation_id or None,
        control_type=control.control_type,
        focused=control.focused,
        role=_dialog_control_role(control.control_type),
        actionable=control.actionable,
        default=control.focused and control.actionable,
        accelerator=control.accelerator or _dialog_control_accelerator(control.title),
    )


def _window_close_control(
    dialog_window_handle: int,
    *,
    visible: bool,
    enabled: bool,
) -> HancomDialogControlState:
    return HancomDialogControlState(
        window_handle=dialog_window_handle,
        title="window_close",
        class_name="title_bar",
        control_id=-1,
        style=0,
        visible=visible,
        enabled=enabled,
        source="window",
        automation_id="SC_CLOSE",
        control_type="WindowClose",
        focused=False,
        role="window_close",
        actionable=visible and enabled,
        default=False,
        accelerator=None,
    )


_SYNCHRONIZE = 0x0010_0000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_ERROR_INVALID_PARAMETER = 87
_PROCESS_COMMAND_LINE_INFORMATION = 60
_STATUS_INFO_LENGTH_MISMATCH = 0xC000_0004
_BUTTON_TYPE_MASK = 0x000F
_PUSH_BUTTON_TYPES = frozenset((0, 1))
_TERMINAL_BUTTON_IDS = frozenset((1, 2, 8))
_SAFE_NON_BUTTON_CLASSES = frozenset(("static",))
_CRASH_REPORTER_IMAGES = frozenset(("werfault.exe", "werfaultsecure.exe"))
_REPORTER_PID_OPTIONS = frozenset(("-p", "/p", "-pid", "/pid", "--pid"))
_MAX_DIAGNOSTIC_DIALOGS = 2
_MAX_DISPLAY_TEXT = 160
_MAX_DIAGNOSTIC_LENGTH = 2_000


@final
class _UnicodeString(Structure):
    _fields_ = (
        ("Length", wintypes.USHORT),
        ("MaximumLength", wintypes.USHORT),
        ("Buffer", wintypes.LPWSTR),
    )


def _window_error_code(error: BaseException) -> int | None:
    for value in (
        getattr(error, "winerror", None),
        getattr(error, "errno", None),
        error.args[0] if error.args else None,
    ):
        if isinstance(value, int):
            return value
    return None


def _process_exit_state(process_id: int) -> DialogProcessState:
    if process_id < 1:
        return "unknown"
    api = _load_process_api()
    try:
        handle = api.OpenProcess(_SYNCHRONIZE, False, process_id)
    except _WINDOW_ERROR as error:
        return (
            "exited"
            if _window_error_code(error) == _ERROR_INVALID_PARAMETER
            else "unknown"
        )
    try:
        result = _load_process_events().WaitForSingleObject(handle, 0)
        if result == _WAIT_OBJECT_0:
            return "exited"
        if result == _WAIT_TIMEOUT:
            return "alive"
        return "unknown"
    except (_WINDOW_ERROR, OSError, RuntimeError):
        return "unknown"
    finally:
        api.CloseHandle(handle)


def _native_process_identity(process_id: int) -> tuple[str, str] | None:
    api = _load_process_api()
    try:
        handle = api.OpenProcess(
            _PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            process_id,
        )
    except _WINDOW_ERROR:
        return None
    raw_handle = wintypes.HANDLE(int(handle))
    try:
        kernel32 = WinDLL("kernel32", use_last_error=True)
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = (
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            POINTER(wintypes.DWORD),
        )
        query_image.restype = wintypes.BOOL
        image_buffer = create_unicode_buffer(32_768)
        image_length = wintypes.DWORD(len(image_buffer))
        if not query_image(
            raw_handle,
            0,
            image_buffer,
            byref(image_length),
        ):
            return None

        ntdll = WinDLL("ntdll")
        query_process = ntdll.NtQueryInformationProcess
        query_process.argtypes = (
            wintypes.HANDLE,
            wintypes.ULONG,
            c_void_p,
            wintypes.ULONG,
            POINTER(wintypes.ULONG),
        )
        query_process.restype = c_long
        required = wintypes.ULONG()
        status = int(
            query_process(
                raw_handle,
                _PROCESS_COMMAND_LINE_INFORMATION,
                None,
                0,
                byref(required),
            )
        )
        if status & 0xFFFF_FFFF != _STATUS_INFO_LENGTH_MISMATCH:
            return None
        if required.value < 1 or required.value > 1_048_576:
            return None
        command_buffer = create_string_buffer(required.value)
        status = int(
            query_process(
                raw_handle,
                _PROCESS_COMMAND_LINE_INFORMATION,
                command_buffer,
                required.value,
                byref(required),
            )
        )
        if status != 0:
            return None
        command = _UnicodeString.from_buffer(command_buffer)
        if (
            command.Length < 1
            or command.Length % 2
            or command.Length > command.MaximumLength
            or not command.Buffer
        ):
            return None
        return (
            image_buffer.value,
            wstring_at(command.Buffer, command.Length // 2),
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    finally:
        api.CloseHandle(handle)


def _windows_command_line_tokens(command_line: str) -> tuple[str, ...]:
    try:
        shell32 = WinDLL("shell32", use_last_error=True)
        command_line_to_argv = shell32.CommandLineToArgvW
        command_line_to_argv.argtypes = (
            wintypes.LPCWSTR,
            POINTER(c_int),
        )
        command_line_to_argv.restype = POINTER(c_wchar_p)
        argument_count = c_int()
        arguments = command_line_to_argv(command_line, byref(argument_count))
        if not arguments or argument_count.value < 1:
            return ()
        try:
            return tuple(arguments[index] for index in range(argument_count.value))
        finally:
            kernel32 = WinDLL("kernel32", use_last_error=True)
            local_free = kernel32.LocalFree
            local_free.argtypes = (c_void_p,)
            local_free.restype = c_void_p
            _ = local_free(ctypes_cast(arguments, c_void_p))
    except (OSError, RuntimeError, TypeError, ValueError):
        return ()


def _reported_target_process_id(command_line: str) -> int | None:
    tokens = _windows_command_line_tokens(command_line)
    process_ids: list[int] = []
    for index, token in enumerate(tokens):
        option = token.casefold()
        if option in _REPORTER_PID_OPTIONS:
            if index + 1 >= len(tokens) or not tokens[index + 1].isdecimal():
                return None
            process_ids.append(int(tokens[index + 1]))
            continue
        attached = re.fullmatch(
            r"(?:--?p(?:id)?|/p(?:id)?)[=:](\d+)",
            option,
        )
        if attached is not None:
            process_ids.append(int(attached.group(1)))
        elif re.match(r"(?:--?p(?:id)?|/p(?:id)?)[=:]", option):
            return None
    return process_ids[0] if len(process_ids) == 1 else None


def _windows_directory() -> str | None:
    try:
        kernel32 = WinDLL("kernel32", use_last_error=True)
        get_windows_directory = kernel32.GetWindowsDirectoryW
        get_windows_directory.argtypes = (
            wintypes.LPWSTR,
            wintypes.UINT,
        )
        get_windows_directory.restype = wintypes.UINT
        buffer = create_unicode_buffer(32_768)
        length = int(get_windows_directory(buffer, len(buffer)))
        if length < 1 or length >= len(buffer):
            return None
        return buffer.value
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _is_trusted_crash_reporter_image(image_path: str) -> bool:
    windows_directory = _windows_directory()
    if not windows_directory:
        return False
    image = PureWindowsPath(image_path)
    if image.name.casefold() not in _CRASH_REPORTER_IMAGES:
        return False
    trusted = {
        PureWindowsPath(windows_directory, directory, image.name)
        for directory in ("System32", "SysWOW64")
    }
    return str(image).casefold() in {str(path).casefold() for path in trusted}


def _is_correlated_crash_reporter(
    reporter_process_id: int,
    target_process_id: int,
) -> bool:
    if reporter_process_id < 1 or target_process_id < 1:
        return False
    identity = _native_process_identity(reporter_process_id)
    if identity is None:
        return False
    image_path, command_line = identity
    if not _is_trusted_crash_reporter_image(image_path):
        return False
    return _reported_target_process_id(command_line) == target_process_id


@final
class ProcessExitWatch:
    __slots__ = ("_api", "_events", "_handle")

    _api: Win32ApiModule
    _events: Win32EventModule
    _handle: int | None

    def __init__(
        self,
        handle: int,
        api: Win32ApiModule,
        events: Win32EventModule,
    ) -> None:
        self._api = api
        self._events = events
        self._handle = handle

    def exited(self) -> bool:
        handle = self._handle
        if handle is None:
            return True
        result = self._events.WaitForSingleObject(handle, 0)
        if result == _WAIT_OBJECT_0:
            return True
        if result == _WAIT_TIMEOUT:
            return False
        raise OSError(f"한컴 프로세스 종료 대기 결과가 올바르지 않습니다: {result}")

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        self._api.CloseHandle(handle)


def open_process_exit_watch(process_id: int) -> ProcessExitWatch | None:
    if process_id < 1:
        return None
    api = _load_process_api()
    try:
        handle = api.OpenProcess(_SYNCHRONIZE, False, process_id)
    except _WINDOW_ERROR:
        return None
    return ProcessExitWatch(handle, api, _load_process_events())


def _window_texts(window: HancomWindowState) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            text.strip()
            for text in (
                window.title,
                *(child.title for child in window.children),
            )
            if text.strip()
        )
    )


def _linked_target_modal(snapshot: HwpDialogSnapshot) -> bool:
    main = snapshot.target_main_handle
    return (
        main is not None
        and snapshot.target_main_enabled is not None
        and not snapshot.target_main_enabled
        and snapshot.visible
        and snapshot.enabled
        and (snapshot.owner_handle == main or snapshot.root_owner_handle == main)
    )


def _button_controls(
    snapshot: HwpDialogSnapshot,
) -> tuple[DialogControlState, ...]:
    return tuple(
        control
        for control in snapshot.controls
        if control.visible
        and control.enabled
        and control.class_name.casefold() == "button"
    )


def _unknown_interactive_controls(
    snapshot: HwpDialogSnapshot,
) -> tuple[DialogControlState, ...]:
    return tuple(
        control
        for control in snapshot.controls
        if control.visible
        and control.enabled
        and control.class_name.casefold() not in _SAFE_NON_BUTTON_CLASSES | {"button"}
    )


def classify_hwp_dialog(snapshot: HwpDialogSnapshot) -> HwpDialogAssessment:
    linked_modal = _linked_target_modal(snapshot)
    if (
        snapshot.target_process_state == "alive"
        and snapshot.correlation in {"same_process", "owned_target"}
        and linked_modal
    ):
        enabled_popup_matches = snapshot.enabled_popup_handle == snapshot.window_handle
        return HwpDialogAssessment(
            snapshot=snapshot,
            safety_grade="a_manual_decision",
            auto_dismiss_allowed=False,
            structural_evidence=(
                "target_process=alive",
                "target_modal=linked",
                f"target_correlation={snapshot.correlation}",
                f"enabled_popup_match={enabled_popup_matches}",
            ),
            user_action=(
                "대화상자의 내용과 각 선택 결과를 직접 확인해 의도한 결정을 "
                "완료한 뒤 대상 한컴 문서에 다시 연결하십시오"
            ),
        )

    buttons = _button_controls(snapshot)
    terminal_buttons = tuple(
        button
        for button in buttons
        if button.control_id in _TERMINAL_BUTTON_IDS
        and button.style & _BUTTON_TYPE_MASK in _PUSH_BUTTON_TYPES
    )
    unknown_controls = _unknown_interactive_controls(snapshot)
    ownerless = snapshot.owner_handle == 0 and snapshot.root_owner_handle in {
        0,
        snapshot.window_handle,
    }
    terminal_default = snapshot.default_button_id in {
        None,
        terminal_buttons[0].control_id if len(terminal_buttons) == 1 else -1,
    }
    if (
        snapshot.structure_complete
        and snapshot.target_process_state == "exited"
        and snapshot.correlation == "crash_reporter_target"
        and snapshot.process_id != snapshot.target_process_id
        and snapshot.class_name == "#32770"
        and snapshot.visible
        and snapshot.enabled
        and ownerless
        and len(buttons) == 1
        and len(terminal_buttons) == 1
        and not unknown_controls
        and terminal_default
    ):
        return HwpDialogAssessment(
            snapshot=snapshot,
            safety_grade="b_safe_information",
            auto_dismiss_allowed=True,
            structural_evidence=(
                "target_process=exited",
                "target_correlation=crash_reporter",
                "dialog_owner=none",
                "dialog_class=#32770",
                "terminal_buttons=1",
                "interactive_inputs=0",
                f"default_button={snapshot.default_button_id or 'none'}",
            ),
            user_action=(
                "종료된 대상 프로세스와 정확히 연관된 정보성 오류창이므로 "
                "자동 정리할 수 있습니다"
            ),
        )

    return HwpDialogAssessment(
        snapshot=snapshot,
        safety_grade="c_unknown",
        auto_dismiss_allowed=False,
        structural_evidence=(
            f"target_process={snapshot.target_process_state}",
            f"target_correlation={snapshot.correlation}",
            f"dialog_class={snapshot.class_name or 'unknown'}",
            f"dialog_owner={snapshot.owner_handle}",
            f"enabled_buttons={len(buttons)}",
            f"interactive_controls={len(unknown_controls)}",
            f"structure_complete={'true' if snapshot.structure_complete else 'false'}",
        ),
        user_action=(
            "창 구조만으로 안전한 자동 처리를 입증할 수 없습니다. 대화상자 "
            "내용을 직접 확인해 필요한 입력이나 선택을 완료한 뒤 대상 한컴 "
            "문서에 다시 연결하십시오"
        ),
    )


def _fallback_assessment(
    window: HancomWindowState,
    dialog: HancomDialogState,
) -> HwpDialogAssessment:
    linked = (
        dialog.modal
        and dialog.owner_handle == window.window_handle
        and not window.enabled
    )
    snapshot = HwpDialogSnapshot(
        window_handle=dialog.window_handle,
        process_id=window.process_id,
        owner_handle=dialog.owner_handle,
        root_owner_handle=dialog.owner_handle,
        title=dialog.title,
        class_name=dialog.class_name,
        visible=dialog.visible,
        enabled=dialog.enabled,
        target_process_id=window.process_id,
        target_process_state="alive" if window.exists else "unknown",
        target_main_handle=window.window_handle,
        target_main_enabled=window.enabled,
        enabled_popup_handle=dialog.window_handle if linked else 0,
        correlation="owned_target" if linked else "same_process",
        controls=(),
        default_button_id=None,
        structure_complete=linked,
    )
    return classify_hwp_dialog(snapshot)


def _escaped_display_text(value: str) -> str:
    compact = " ".join(value.replace(";", "；").replace("=", "＝").split())
    return compact[:_MAX_DISPLAY_TEXT] or "(제목 없음)"


def _assessment_diagnostic(
    scope: Literal["target", "orphan"],
    assessment: HwpDialogAssessment,
    dismissal: HwpSafeDialogDismissResult,
) -> str:
    dialog_gone = dismissal.dialog_gone
    user_action_required = not dialog_gone
    auto_dismissed = dismissal.close_sent and dialog_gone
    if dialog_gone:
        user_action = "대상 한컴 프로세스에 다시 연결하십시오"
    elif assessment.safety_grade == "b_safe_information":
        user_action = (
            "안전 등급 창의 닫힘을 확인하지 못했습니다. 화면에서 해당 정보성 "
            "오류창을 닫은 뒤 대상 한컴 프로세스에 다시 연결하십시오"
        )
    else:
        user_action = assessment.user_action
    prefix = (
        "대상 한컴 창에 대화상자가 떠 있습니다"
        if scope == "target"
        else "고아 한컴 오류 대화상자가 남아 있습니다"
    )
    evidence = ",".join(
        _escaped_display_text(item) for item in assessment.structural_evidence
    )
    return "; ".join(
        (
            prefix,
            "dialog_detected=true",
            f"dialog_safety_grade={assessment.safety_grade}",
            (
                "dialog_auto_dismiss_allowed=true"
                if assessment.auto_dismiss_allowed
                else "dialog_auto_dismiss_allowed=false"
            ),
            (
                "dialog_auto_dismiss_attempted=true"
                if dismissal.close_sent
                else "dialog_auto_dismiss_attempted=false"
            ),
            (
                "dialog_auto_dismissed=true"
                if auto_dismissed
                else "dialog_auto_dismissed=false"
            ),
            (
                "dialog_present_after=false"
                if dialog_gone
                else "dialog_present_after=true"
            ),
            (
                "dialog_user_action_required=true"
                if user_action_required
                else "dialog_user_action_required=false"
            ),
            f"dialog_handle={assessment.window_handle}",
            f"dialog_process_id={assessment.process_id}",
            f"dialog_class={_escaped_display_text(assessment.class_name)}",
            f"dialog_title={_escaped_display_text(assessment.title)}",
            f"dialog_structural_evidence={evidence}",
            f"user_action={user_action}",
        )
    )


def stalled_hwp_call_diagnostic(
    windows: WindowStateReader,
    process_id: int,
) -> str | None:
    try:
        visible_windows = windows.list_visible_hwp_windows().windows
    except (HwpLiveError, OSError, RuntimeError):
        return None
    policy = windows if isinstance(windows, DialogSafetyReader) else None
    assessments: list[tuple[Literal["target", "orphan"], HwpDialogAssessment]] = []
    seen: set[int] = set()
    for window in visible_windows:
        if process_id > 0 and window.process_id == process_id:
            if window.class_name == "#32770":
                snapshot = HwpDialogSnapshot(
                    window_handle=window.window_handle,
                    process_id=window.process_id,
                    owner_handle=0,
                    root_owner_handle=window.window_handle,
                    title=" | ".join(_window_texts(window)),
                    class_name=window.class_name,
                    visible=window.visible,
                    enabled=window.enabled,
                    target_process_id=process_id,
                    target_process_state="alive",
                    target_main_handle=None,
                    target_main_enabled=None,
                    enabled_popup_handle=0,
                    correlation="same_process",
                    controls=(),
                    default_button_id=None,
                    structure_complete=False,
                )
                assessments.append(("target", classify_hwp_dialog(snapshot)))
                seen.add(window.window_handle)
            for dialog in window.dialogs:
                if not dialog.modal or dialog.window_handle in seen:
                    continue
                try:
                    assessment = (
                        policy.assess_dialog(
                            dialog.window_handle,
                            process_id,
                            window.window_handle,
                        )
                        if policy is not None
                        else _fallback_assessment(window, dialog)
                    )
                except (HwpLiveError, OSError, RuntimeError):
                    assessment = _fallback_assessment(window, dialog)
                assessments.append(("target", assessment))
                seen.add(dialog.window_handle)
            continue
    if policy is not None and process_id > 0:
        try:
            orphans = policy.list_correlated_orphan_dialogs(process_id)
        except (HwpLiveError, OSError, RuntimeError):
            orphans = ()
        assessments.extend(
            ("orphan", assessment)
            for assessment in orphans
            if assessment.window_handle not in seen
        )

    ordered = sorted(
        assessments,
        key=lambda item: (item[0] != "target", item[1].window_handle),
    )
    overflow = max(0, len(ordered) - _MAX_DIAGNOSTIC_DIALOGS)
    diagnostics: list[str] = []
    if overflow:
        diagnostics.append(
            "; ".join(
                (
                    "추가 한컴 대화상자가 남아 있습니다",
                    "dialog_detected=true",
                    f"additional_dialogs={overflow}",
                    "dialog_present_after=true",
                    "dialog_user_action_required=true",
                    (
                        "user_action=나머지 대화상자도 화면에서 직접 확인해 "
                        "완료한 뒤 대상 한컴 문서에 다시 연결하십시오"
                    ),
                )
            )
        )
    for scope, assessment in ordered[:_MAX_DIAGNOSTIC_DIALOGS]:
        dismissal = HwpSafeDialogDismissResult(
            close_sent=False,
            dialog_gone=False,
        )
        if policy is not None and assessment.auto_dismiss_allowed:
            try:
                dismissal = policy.dismiss_safe_dialog(assessment)
            except (HwpLiveError, OSError, RuntimeError):
                dismissal = HwpSafeDialogDismissResult(
                    close_sent=False,
                    dialog_gone=False,
                )
        diagnostics.append(_assessment_diagnostic(scope, assessment, dismissal))
    diagnostic = "; ".join(diagnostics)
    return diagnostic[:_MAX_DIAGNOSTIC_LENGTH] or None


@final
class Win32WindowStateReader:
    __slots__ = ("_gui", "_process", "_uia")

    _gui: Win32GuiModule
    _process: Win32ProcessModule
    _uia: UiAutomationReader

    def __init__(self) -> None:
        self._gui = _load_gui()
        self._process = _load_process()
        self._uia = _load_uia()

    def _dialog(
        self,
        window_handle: int,
        owner_handle: int,
        main_enabled: bool,
    ) -> HancomDialogState:
        return HancomDialogState(
            window_handle=window_handle,
            owner_handle=owner_handle,
            title=self._gui.GetWindowText(window_handle),
            class_name=self._gui.GetClassName(window_handle),
            visible=self._gui.IsWindowVisible(window_handle),
            enabled=self._gui.IsWindowEnabled(window_handle),
            modal=not main_enabled,
        )

    def _dialogs(
        self,
        main_handle: int,
        process_id: int,
        main_enabled: bool,
    ) -> tuple[HancomDialogState, ...]:
        dialogs: list[HancomDialogState] = []

        def collect(window_handle: int, extra: int) -> bool:
            _ = extra
            try:
                _, candidate_pid = self._process.GetWindowThreadProcessId(window_handle)
                if candidate_pid != process_id or window_handle == main_handle:
                    return True
                if not self._gui.IsWindowVisible(window_handle):
                    return True
                owner = self._gui.GetWindow(window_handle, _GW_OWNER)
                class_name = self._gui.GetClassName(window_handle)
                title = self._gui.GetWindowText(window_handle)
                orphan_wpf_dialog = (
                    not main_enabled
                    and owner == 0
                    and class_name.startswith("HwndWrapper[Hwp.exe;")
                    and not title.endswith(" - 한글")
                )
                if owner == 0 and class_name != "#32770" and not orphan_wpf_dialog:
                    return True
                dialogs.append(self._dialog(window_handle, owner, main_enabled))
            except _WINDOW_ERROR:
                return True
            return True

        self._gui.EnumWindows(collect, 0)
        return tuple(sorted(dialogs, key=lambda item: item.window_handle))

    def _children(self, window_handle: int) -> tuple[HancomWindowChildState, ...]:
        children: list[HancomWindowChildState] = []

        def collect(child_handle: int, extra: int) -> bool:
            _ = extra
            try:
                children.append(
                    HancomWindowChildState(
                        window_handle=child_handle,
                        title=self._gui.GetWindowText(child_handle),
                        class_name=self._gui.GetClassName(child_handle),
                        visible=self._gui.IsWindowVisible(child_handle),
                        enabled=self._gui.IsWindowEnabled(child_handle),
                    )
                )
            except _WINDOW_ERROR:
                return True
            return True

        self._gui.EnumChildWindows(window_handle, collect, 0)
        return tuple(sorted(children, key=lambda item: item.window_handle))

    def _dialog_controls(
        self,
        window_handle: int,
    ) -> tuple[tuple[DialogControlState, ...], bool]:
        controls: list[DialogControlState] = []
        complete = True

        def collect(child_handle: int, extra: int) -> bool:
            nonlocal complete
            _ = extra
            try:
                controls.append(
                    DialogControlState(
                        window_handle=child_handle,
                        title=self._gui.GetWindowText(child_handle),
                        class_name=self._gui.GetClassName(child_handle),
                        control_id=self._gui.GetDlgCtrlID(child_handle),
                        style=self._gui.GetWindowLong(child_handle, _GWL_STYLE),
                        visible=self._gui.IsWindowVisible(child_handle),
                        enabled=self._gui.IsWindowEnabled(child_handle),
                    )
                )
            except _WINDOW_ERROR:
                complete = False
            return True

        try:
            self._gui.EnumChildWindows(window_handle, collect, 0)
        except _WINDOW_ERROR:
            return (), False
        return (
            tuple(sorted(controls, key=lambda item: item.window_handle)),
            complete,
        )

    def _default_button_id(
        self,
        window_handle: int,
        class_name: str,
    ) -> tuple[int | None, bool]:
        if class_name != "#32770":
            return None, True
        try:
            delivered, result = self._gui.SendMessageTimeout(
                window_handle,
                _DM_GETDEFID,
                0,
                0,
                _SMTO_ABORTIFHUNG | _SMTO_ERRORONEXIT,
                _MESSAGE_TIMEOUT_MILLISECONDS,
            )
        except _WINDOW_ERROR:
            return None, False
        if not delivered:
            return None, False
        if result >> 16 & 0xFFFF != _DC_HASDEFID:
            return None, True
        return result & 0xFFFF, True

    def _owner_process_id(self, owner_handle: int) -> int:
        if owner_handle < 1 or not self._gui.IsWindow(owner_handle):
            return 0
        try:
            _, process_id = self._process.GetWindowThreadProcessId(owner_handle)
        except _WINDOW_ERROR:
            return 0
        return process_id

    def assess_dialog(
        self,
        window_handle: int,
        target_process_id: int,
        target_main_handle: int | None,
    ) -> HwpDialogAssessment:
        if not self._gui.IsWindow(window_handle):
            raise HwpLiveError("판정할 대화상자가 이미 사라졌습니다")
        try:
            _, process_id = self._process.GetWindowThreadProcessId(window_handle)
            owner_handle = self._gui.GetWindow(window_handle, _GW_OWNER)
            root_owner_handle = self._gui.GetAncestor(
                window_handle,
                _GA_ROOTOWNER,
            )
            class_name = self._gui.GetClassName(window_handle)
            controls, controls_complete = self._dialog_controls(window_handle)
            default_button_id, default_complete = self._default_button_id(
                window_handle,
                class_name,
            )
            main_exists = target_main_handle is not None and self._gui.IsWindow(
                target_main_handle
            )
            main_enabled = (
                self._gui.IsWindowEnabled(target_main_handle)
                if main_exists and target_main_handle is not None
                else None
            )
            enabled_popup_handle = (
                self._gui.GetWindow(target_main_handle, _GW_ENABLEDPOPUP)
                if main_exists and target_main_handle is not None
                else 0
            )
            target_process_state = _process_exit_state(target_process_id)
            owner_process_id = self._owner_process_id(owner_handle)
            if owner_process_id == target_process_id:
                correlation: DialogCorrelation = "owned_target"
            elif process_id == target_process_id:
                correlation = "same_process"
            elif target_process_state == "exited" and _is_correlated_crash_reporter(
                process_id,
                target_process_id,
            ):
                correlation = "crash_reporter_target"
            else:
                correlation = "none"
            snapshot = HwpDialogSnapshot(
                window_handle=window_handle,
                process_id=process_id,
                owner_handle=owner_handle,
                root_owner_handle=root_owner_handle,
                title=self._gui.GetWindowText(window_handle),
                class_name=class_name,
                visible=self._gui.IsWindowVisible(window_handle),
                enabled=self._gui.IsWindowEnabled(window_handle),
                target_process_id=target_process_id,
                target_process_state=target_process_state,
                target_main_handle=target_main_handle if main_exists else None,
                target_main_enabled=main_enabled,
                enabled_popup_handle=enabled_popup_handle,
                correlation=correlation,
                controls=controls,
                default_button_id=default_button_id,
                structure_complete=controls_complete and default_complete,
            )
        except _WINDOW_ERROR as error:
            raise HwpLiveError(
                "대화상자의 구조적 안전 근거를 읽을 수 없습니다"
            ) from error
        return classify_hwp_dialog(snapshot)

    def list_correlated_orphan_dialogs(
        self,
        target_process_id: int,
    ) -> tuple[HwpDialogAssessment, ...]:
        if _process_exit_state(target_process_id) != "exited":
            return ()
        handles: list[int] = []

        def collect(window_handle: int, extra: int) -> bool:
            _ = extra
            try:
                if (
                    not self._gui.IsWindowVisible(window_handle)
                    or self._gui.GetClassName(window_handle) != "#32770"
                ):
                    return True
                _, process_id = self._process.GetWindowThreadProcessId(window_handle)
                if process_id != target_process_id and _is_correlated_crash_reporter(
                    process_id,
                    target_process_id,
                ):
                    handles.append(window_handle)
            except _WINDOW_ERROR:
                return True
            return True

        self._gui.EnumWindows(collect, 0)
        assessments: list[HwpDialogAssessment] = []
        for handle in sorted(set(handles)):
            try:
                assessments.append(
                    self.assess_dialog(
                        handle,
                        target_process_id,
                        None,
                    )
                )
            except HwpLiveError:
                continue
        return tuple(assessments)

    @staticmethod
    def _structure_key(snapshot: HwpDialogSnapshot) -> tuple[object, ...]:
        return (
            snapshot.window_handle,
            snapshot.process_id,
            snapshot.owner_handle,
            snapshot.root_owner_handle,
            snapshot.class_name,
            snapshot.visible,
            snapshot.enabled,
            snapshot.target_process_id,
            snapshot.target_process_state,
            snapshot.correlation,
            tuple(
                (
                    control.window_handle,
                    control.class_name,
                    control.control_id,
                    control.style,
                    control.visible,
                    control.enabled,
                )
                for control in snapshot.controls
            ),
            snapshot.default_button_id,
            snapshot.structure_complete,
        )

    def dismiss_safe_dialog(
        self,
        assessment: HwpDialogAssessment,
    ) -> HwpSafeDialogDismissResult:
        if not assessment.auto_dismiss_allowed:
            return HwpSafeDialogDismissResult(
                close_sent=False,
                dialog_gone=False,
            )
        original = assessment.snapshot
        try:
            current = self.assess_dialog(
                original.window_handle,
                original.target_process_id,
                original.target_main_handle,
            )
        except HwpLiveError:
            return HwpSafeDialogDismissResult(
                close_sent=False,
                dialog_gone=not self._gui.IsWindow(original.window_handle),
            )
        if not current.auto_dismiss_allowed or self._structure_key(
            current.snapshot
        ) != self._structure_key(original):
            return HwpSafeDialogDismissResult(
                close_sent=False,
                dialog_gone=False,
            )
        try:
            delivered, _ = self._gui.SendMessageTimeout(
                original.window_handle,
                _WM_CLOSE,
                0,
                0,
                _SMTO_ABORTIFHUNG | _SMTO_ERRORONEXIT,
                _MESSAGE_TIMEOUT_MILLISECONDS,
            )
        except _WINDOW_ERROR:
            return HwpSafeDialogDismissResult(
                close_sent=False,
                dialog_gone=False,
            )
        if not delivered:
            return HwpSafeDialogDismissResult(
                close_sent=False,
                dialog_gone=False,
            )
        if not self._gui.IsWindow(original.window_handle):
            return HwpSafeDialogDismissResult(
                close_sent=True,
                dialog_gone=True,
            )
        try:
            _, process_id = self._process.GetWindowThreadProcessId(
                original.window_handle
            )
            class_name = self._gui.GetClassName(original.window_handle)
        except _WINDOW_ERROR:
            return HwpSafeDialogDismissResult(
                close_sent=True,
                dialog_gone=False,
            )
        return HwpSafeDialogDismissResult(
            close_sent=True,
            dialog_gone=(
                process_id != original.process_id or class_name != original.class_name
            ),
        )

    def read(self, window_handle: int) -> HancomWindowState:
        if not self._gui.IsWindow(window_handle):
            return HancomWindowState(
                window_handle=window_handle,
                process_id=0,
                exists=False,
                visible=False,
                enabled=False,
                foreground=False,
                title="",
                class_name="",
                dialogs=(),
                children=(),
            )
        try:
            _, process_id = self._process.GetWindowThreadProcessId(window_handle)
            enabled = self._gui.IsWindowEnabled(window_handle)
            return HancomWindowState(
                window_handle=window_handle,
                process_id=process_id,
                exists=True,
                visible=self._gui.IsWindowVisible(window_handle),
                enabled=enabled,
                foreground=self._gui.GetForegroundWindow() == window_handle,
                title=self._gui.GetWindowText(window_handle),
                class_name=self._gui.GetClassName(window_handle),
                dialogs=self._dialogs(window_handle, process_id, enabled),
                children=self._children(window_handle),
            )
        except _WINDOW_ERROR as error:
            raise HwpLiveError("한컴 창과 대화상자 상태를 읽을 수 없습니다") from error

    def list_visible_hwp_windows(self) -> HancomWindowStateList:
        handles: list[int] = []

        def collect(window_handle: int, extra: int) -> bool:
            _ = extra
            try:
                if not self._gui.IsWindowVisible(window_handle):
                    return True
                class_name = self._gui.GetClassName(window_handle)
                title = self._gui.GetWindowText(window_handle)
                if (
                    "hwp" in class_name.casefold()
                    or title == "Hwp"
                    or "hwp" in title.casefold()
                    or title.endswith(" - 한글")
                ):
                    handles.append(window_handle)
            except _WINDOW_ERROR:
                return True
            return True

        self._gui.EnumWindows(collect, 0)
        return HancomWindowStateList(
            windows=tuple(self.read(handle) for handle in sorted(set(handles)))
        )

    def inspect_dialog(self, dialog_window_handle: int) -> HancomPopupStructure:
        try:
            if not self._gui.IsWindow(dialog_window_handle):
                raise ValueError(
                    f"dialog window does not exist: {dialog_window_handle}"
                )
            _, process_id = self._process.GetWindowThreadProcessId(dialog_window_handle)
            owner_handle = self._gui.GetWindow(dialog_window_handle, _GW_OWNER)
            root_owner_handle = self._gui.GetAncestor(
                dialog_window_handle,
                _GA_ROOTOWNER,
            )
            class_name = self._gui.GetClassName(dialog_window_handle)
            controls, controls_complete = self._dialog_controls(dialog_window_handle)
            default_button_id, default_complete = self._default_button_id(
                dialog_window_handle,
                class_name,
            )
            public_controls = tuple(
                _public_dialog_control(control, default_button_id)
                for control in controls
            )
            if not public_controls and class_name.startswith("HwndWrapper[Hwp.exe;"):
                public_controls = tuple(
                    _public_uia_control(dialog_window_handle, control)
                    for control in self._uia.inspect(dialog_window_handle)
                )
                default_button_id = next(
                    (
                        control.control_id
                        for control in public_controls
                        if control.default
                    ),
                    None,
                )
            wpf_close = any(
                control.source == "uia"
                and control.automation_id == "PART_Close"
                and control.visible
                and control.enabled
                and not control.actionable
                for control in public_controls
            )
            if wpf_close or (
                not any(control.actionable for control in public_controls)
                and self._gui.GetWindowLong(dialog_window_handle, _GWL_STYLE)
                & _WS_SYSMENU
            ):
                public_controls += (
                    _window_close_control(
                        dialog_window_handle,
                        visible=self._gui.IsWindowVisible(dialog_window_handle),
                        enabled=self._gui.IsWindowEnabled(dialog_window_handle),
                    ),
                )
            return HancomPopupStructure(
                window_handle=dialog_window_handle,
                process_id=process_id,
                owner_handle=owner_handle,
                root_owner_handle=root_owner_handle,
                title=self._gui.GetWindowText(dialog_window_handle),
                class_name=class_name,
                visible=self._gui.IsWindowVisible(dialog_window_handle),
                enabled=self._gui.IsWindowEnabled(dialog_window_handle),
                controls=public_controls,
                default_button_id=default_button_id,
                structure_complete=controls_complete and default_complete,
            )
        except _WINDOW_ERROR as error:
            raise HwpLiveError("한컴 팝업 구조를 읽을 수 없습니다") from error

    def invoke_dialog_action(
        self,
        dialog_window_handle: int,
        control_id: int,
    ) -> HancomDialogActionResult:
        before = self.inspect_dialog(dialog_window_handle)
        matches = tuple(
            control for control in before.controls if control.control_id == control_id
        )
        if len(matches) != 1:
            raise ValueError(
                f"dialog action requires exactly one requested control_id; found {len(matches)}"
            )
        selected = matches[0]
        if not selected.actionable:
            raise ValueError(f"control_id {control_id} is not an actionable control")
        if selected.source == "window":
            wpf_close = any(
                control.source == "uia"
                and control.automation_id == "PART_Close"
                and control.visible
                and control.enabled
                for control in before.controls
            )
            if (
                not self._gui.IsWindow(dialog_window_handle)
                or not self._gui.IsWindowVisible(dialog_window_handle)
                or not self._gui.IsWindowEnabled(dialog_window_handle)
                or not (
                    wpf_close
                    or self._gui.GetWindowLong(dialog_window_handle, _GWL_STYLE)
                    & _WS_SYSMENU
                )
            ):
                raise ValueError(
                    "dialog title-bar close action changed before delivery"
                )
            delivered, _ = self._gui.SendMessageTimeout(
                dialog_window_handle,
                _WM_CLOSE,
                0,
                0,
                _SMTO_ABORTIFHUNG | _SMTO_ERRORONEXIT,
                _ACTION_MESSAGE_TIMEOUT_MILLISECONDS,
            )
            dialog_present_after = self._gui.IsWindow(dialog_window_handle)
            after = (
                self.inspect_dialog(dialog_window_handle)
                if dialog_present_after
                else None
            )
            return HancomDialogActionResult(
                before=before,
                selected_control=selected,
                message_delivered=bool(delivered),
                dialog_present_after=dialog_present_after,
                after=after,
            )
        if selected.source == "uia":
            current = self.inspect_dialog(dialog_window_handle)
            current_matches = tuple(
                control
                for control in current.controls
                if control.control_id == control_id
            )
            if len(current_matches) != 1 or current_matches[0] != selected:
                raise ValueError(
                    "dialog or selected UI Automation control changed before action delivery"
                )
            delivered = self._uia.invoke(dialog_window_handle, selected)
            dialog_present_after = self._gui.IsWindow(dialog_window_handle)
            after = (
                self.inspect_dialog(dialog_window_handle)
                if dialog_present_after
                else None
            )
            return HancomDialogActionResult(
                before=before,
                selected_control=selected,
                message_delivered=delivered,
                dialog_present_after=dialog_present_after,
                after=after,
            )
        try:
            if (
                not self._gui.IsWindow(dialog_window_handle)
                or not self._gui.IsWindow(selected.window_handle)
                or self._gui.GetDlgCtrlID(selected.window_handle) != control_id
                or self._gui.GetClassName(selected.window_handle) != selected.class_name
                or self._gui.GetWindowText(selected.window_handle) != selected.title
                or not self._gui.IsWindowVisible(selected.window_handle)
                or not self._gui.IsWindowEnabled(selected.window_handle)
            ):
                raise ValueError(
                    "dialog or selected control changed before action delivery"
                )
            delivered, _ = self._gui.SendMessageTimeout(
                selected.window_handle,
                _BM_CLICK,
                0,
                0,
                _SMTO_ABORTIFHUNG | _SMTO_ERRORONEXIT,
                _ACTION_MESSAGE_TIMEOUT_MILLISECONDS,
            )
            dialog_present_after = self._gui.IsWindow(dialog_window_handle)
            after = (
                self.inspect_dialog(dialog_window_handle)
                if dialog_present_after
                else None
            )
            return HancomDialogActionResult(
                before=before,
                selected_control=selected,
                message_delivered=bool(delivered),
                dialog_present_after=dialog_present_after,
                after=after,
            )
        except _WINDOW_ERROR as error:
            raise HwpLiveError("한컴 팝업 버튼을 실행할 수 없습니다") from error

    def dismiss_dialogs(self, window_handle: int) -> HancomDialogDismissResult:
        before = self.read(window_handle)
        dismissed: list[int] = []
        state = before
        for _ in range(8):
            if not state.dialogs:
                break
            target = next(
                (dialog for dialog in state.dialogs if dialog.enabled),
                state.dialogs[0],
            )
            try:
                self._gui.PostMessage(target.window_handle, _WM_CLOSE, 0, 0)
            except _WINDOW_ERROR:
                break
            dismissed.append(target.window_handle)
            time.sleep(0.25)
            state = self.read(window_handle)
        return HancomDialogDismissResult(
            before=before,
            after=state,
            dismissed_handles=tuple(dismissed),
        )

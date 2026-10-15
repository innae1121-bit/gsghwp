from __future__ import annotations

from ctypes import (
    Structure,
    WinDLL,
    byref,
    c_long,
    c_size_t,
    c_void_p,
    get_last_error,
    sizeof,
    wintypes,
)
import os
import re
import shutil
import sys
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Protocol, cast, final
from winreg import (
    CreateKeyEx,
    HKEY_CURRENT_USER,
    HKEYType,
    KEY_QUERY_VALUE,
    KEY_SET_VALUE,
    QueryValueEx,
    REG_DWORD,
    REG_SZ,
    SetValueEx,
)

from hwp_errors import HwpLiveError
from hwp_native_registration_desktop import (
    DesktopRegistrationResult,
    NativeBridgeRegistration as NativeBridgeRegistration,
    register_native_bridge_on_desktop,
)


NATIVE_BRIDGE_VERSION = "0.5.180"
MODULE_NAME = "한컴브릿지"
MODULES_KEY = r"Software\HNC\HwpUserAction\Modules"
USES_KEY = rf"{MODULES_KEY}\Uses"
ERROR_SHARING_VIOLATION: Final = 32
CONTENT_HASH_PREFIX_LENGTH: Final = 16
# Which processes are asked "did you map the bridge?". The editor only.
#
# Not the same question as hwp_runtime._HWP_PROCESS_NAMES, which watches for
# *conflicting* Hancom processes and is right to include HwpApi.exe there.
# HwpApi.exe is Hancom's automation host, not the editor: the bridge is
# registered under HKCU\Software\HNC\HwpUserAction\Modules and only Hwp.exe
# loads it, so a perfectly healthy HwpApi.exe never carries it. Measured: with
# Hwp.exe PID 27496 holding 0.5.130 and matches_expected=true, HwpApi.exe PID
# 61092 had 84 modules read successfully and no bridge among them -- and that
# alone produced native_bridge_state="not_loaded" plus
# hangul_restart_required=true on a healthy system, a verdict that stayed on
# across restarts because nothing about it was ever wrong.
BRIDGE_HOST_PROCESS_NAMES: Final = frozenset({"hwp.exe"})
BRIDGE_MODULE_PREFIX: Final = "hancomlivebridge"
_VERSION_PATTERN: Final = re.compile(r"\d+(?:\.\d+)+")
_DIGEST_CACHE_LIMIT: Final = 32
_DIGEST_CACHE: Final[dict[tuple[str, int, int], str]] = {}


@dataclass(frozen=True, slots=True)
class LoadedBridgeModule:
    """A HancomLiveBridge DLL a live Hancom process has actually mapped."""

    process_id: int
    process_name: str
    path: Path
    version: str | None


@dataclass(frozen=True, slots=True)
class LoadedBridgeScan:
    processes: tuple[tuple[int, str], ...]
    modules: tuple[LoadedBridgeModule, ...]
    unreadable_processes: tuple[int, ...]
    failure: str | None


def _file_digest(path: Path) -> bytes:
    checksum = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.digest()


def content_digest(path: Path) -> str | None:
    """sha256 of ``path``, cached per (path, size, mtime). None when unreadable."""
    try:
        status = path.stat()
    except OSError:
        return None
    key = (str(path).casefold(), status.st_size, status.st_mtime_ns)
    cached = _DIGEST_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        digest = _file_digest(path).hex()
    except OSError:
        return None
    if len(_DIGEST_CACHE) >= _DIGEST_CACHE_LIMIT:
        _DIGEST_CACHE.clear()
    _DIGEST_CACHE[key] = digest
    return digest


def packaged_native_bridge_path(package_root: Path | None = None) -> Path:
    root = Path(__file__).parents[3] if package_root is None else package_root
    return (
        root
        / "addon"
        / "HancomLiveBridgeNative"
        / "bin"
        / NATIVE_BRIDGE_VERSION
        / "HancomLiveBridge.dll"
    ).resolve()


def packaged_native_bridge_digest() -> str | None:
    return content_digest(packaged_native_bridge_path())


def bridge_version_from_path(path: Path) -> str | None:
    """Installed bridges live in ``.../native/<version>/HancomLiveBridge*.dll``."""
    name = path.parent.name
    return name if _VERSION_PATTERN.fullmatch(name) else None


def _version_order(version: str | None) -> tuple[int, ...] | None:
    if version is None:
        return None
    return tuple(int(part) for part in version.split("."))


def _same_file_content(left: Path, right: Path) -> bool:
    if not right.is_file():
        return False
    left_stat = left.stat()
    right_stat = right.stat()
    if (
        left_stat.st_size == right_stat.st_size
        and left_stat.st_mtime_ns == right_stat.st_mtime_ns
    ):
        return True
    if left_stat.st_size != right_stat.st_size:
        return False

    return _file_digest(left) == _file_digest(right)


def _read_registry_value(
    key: int | HKEYType,
    name: str,
) -> tuple[str | int | None, int | None]:
    try:
        current_value, current_type = cast(
            tuple[str | int, int],
            QueryValueEx(key, name),
        )
    except FileNotFoundError:
        return None, None
    return current_value, current_type


def _set_registry_value_if_changed(
    key: int | HKEYType,
    name: str,
    value: str | int,
    value_type: int,
) -> None:
    current_value, current_type = _read_registry_value(key, name)
    if current_value != value or current_type != value_type:
        SetValueEx(key, name, 0, value_type, value)


def keeps_registered_bridge(
    current_value: str | int | None,
    current_type: int | None,
    candidate: Path,
) -> bool:
    """True when the existing registration must be left alone.

    Hancom reads this value once, at process start. Several workers can be alive
    at the same time, each holding the NATIVE_BRIDGE_VERSION it was launched
    with, and every one of them re-registers on startup. Without this guard an
    older worker silently downgrades the registration a newer worker just wrote,
    and the next Hangul start loads the older DLL. Registration may move
    forward or stay put; it must never move backward.
    """
    if (
        current_type != REG_SZ
        or not isinstance(current_value, str)
        or not current_value
    ):
        return False
    registered = Path(current_value)
    if registered == candidate:
        return True
    try:
        if not registered.is_file():
            return False
    except OSError:
        return False
    registered_order = _version_order(bridge_version_from_path(registered))
    candidate_order = _version_order(bridge_version_from_path(candidate))
    if registered_order is None or candidate_order is None:
        return False
    return registered_order > candidate_order


def _register_native_bridge(path: Path) -> Path:
    """Point Hancom at ``path`` unless a newer bridge is already registered.

    Returns the path the registry points at after this call.
    """
    access = KEY_QUERY_VALUE | KEY_SET_VALUE
    with CreateKeyEx(HKEY_CURRENT_USER, MODULES_KEY, 0, access) as modules:
        current_value, current_type = _read_registry_value(modules, MODULE_NAME)
        if keeps_registered_bridge(current_value, current_type, path):
            registered = Path(cast(str, current_value))
        else:
            SetValueEx(modules, MODULE_NAME, 0, REG_SZ, str(path))
            registered = path
    with CreateKeyEx(HKEY_CURRENT_USER, USES_KEY, 0, access) as uses:
        _set_registry_value_if_changed(uses, MODULE_NAME, 1, REG_DWORD)
    return registered


def registered_native_bridge_path() -> Path | None:
    """Read-only view of the DLL Hancom will load at its next start."""
    try:
        with CreateKeyEx(
            HKEY_CURRENT_USER,
            MODULES_KEY,
            0,
            KEY_QUERY_VALUE,
        ) as modules:
            current_value, current_type = _read_registry_value(modules, MODULE_NAME)
    except OSError:
        return None
    if (
        current_type != REG_SZ
        or not isinstance(current_value, str)
        or not current_value
    ):
        return None
    return Path(current_value)


def ensure_native_bridge_registered(
    *,
    package_root: Path | None = None,
    local_app_data: Path | None = None,
) -> NativeBridgeRegistration:
    registration = install_native_bridge_current_process(
        package_root=package_root, local_app_data=local_app_data
    )
    if local_app_data is None and os.environ.get(
        "CODEX_WINDOWS_SANDBOX_PACKAGE_FAMILY"
    ):
        # Codex descendants can inherit an MSIX registry silo without package
        # identity. Their HKCU value is not what Explorer-launched Hangul reads.
        registration = register_native_bridge_on_desktop(
            Path(__file__).parents[3] if package_root is None else package_root
        )
        # Keep Codex-launched Hangul in sync too, preserving the no-downgrade rule.
        _ = _register_native_bridge(registration.registered_path or registration.path)
    return registration


def verify_native_bridge_registration(registration: NativeBridgeRegistration) -> None:
    expected = registration.registered_path or registration.path
    if registered_native_bridge_path() != expected:
        raise HwpLiveError(
            "일반 한컴용 브리지 DLL 등록 경로의 되읽기 검증이 실패했습니다"
        )
    with CreateKeyEx(HKEY_CURRENT_USER, USES_KEY, 0, KEY_QUERY_VALUE) as uses:
        enabled, value_type = _read_registry_value(uses, MODULE_NAME)
    if enabled != 1 or value_type != REG_DWORD:
        raise HwpLiveError(
            "일반 한컴용 브리지 활성화 등록의 되읽기 검증이 실패했습니다"
        )


def install_native_bridge_current_process(
    *,
    package_root: Path | None = None,
    local_app_data: Path | None = None,
) -> NativeBridgeRegistration:
    """Install in this process's registry view; also used by the desktop helper."""
    source = packaged_native_bridge_path(package_root)
    if not source.is_file():
        raise HwpLiveError(f"패키지 네이티브 한컴 브리지 DLL이 없습니다: {source}")
    if local_app_data is None:
        configured = os.environ.get("LOCALAPPDATA")
        if not configured:
            raise HwpLiveError("Windows LOCALAPPDATA 경로를 확인할 수 없습니다")
        local_app_data = Path(configured)
    destination = (
        local_app_data
        / "HancomDocumentAutomation"
        / "native"
        / NATIVE_BRIDGE_VERSION
        / "HancomLiveBridge.dll"
    ).resolve()
    copied = not _same_file_content(source, destination)
    if copied:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            _ = shutil.copy2(source, destination)
        except PermissionError as error:
            if error.winerror != ERROR_SHARING_VIOLATION:
                raise
            digest_prefix = _file_digest(source).hex()[:CONTENT_HASH_PREFIX_LENGTH]
            destination = destination.with_name(f"HancomLiveBridge-{digest_prefix}.dll")
            copied = not _same_file_content(source, destination)
            if copied:
                _ = shutil.copy2(source, destination)
    registered = _register_native_bridge(destination)
    return NativeBridgeRegistration(
        path=destination,
        copied=copied,
        registered_path=registered,
    )


# Reading the DLL a running Hancom process actually mapped.
#
# The registry says what the next Hangul start will load and
# NATIVE_BRIDGE_VERSION says what this Python build expects. Neither is what a
# process that is already running has in memory: Hancom resolves the module
# once, at start. Only the loader's module list answers that, so ask it.
#
# The bridge is a Win32 (32-bit) DLL while this Python is 64-bit, so
# EnumProcessModules is not usable here: a 64-bit caller cannot enumerate a
# 32-bit process that way. Toolhelp with TH32CS_SNAPMODULE32 can, and is the
# documented path for exactly this direction.
_TH32CS_SNAPPROCESS: Final = 0x0000_0002
_TH32CS_SNAPMODULE: Final = 0x0000_0008
_TH32CS_SNAPMODULE32: Final = 0x0000_0010
_MAX_PATH: Final = 260
_MAX_MODULE_NAME: Final = 256
_INVALID_HANDLE: Final = c_void_p(-1).value


class _ProcessEntryView(Protocol):
    """What attribute access on _ProcessEntry32 yields; ctypes types it as Any."""

    dwSize: int
    th32ProcessID: int
    szExeFile: str


class _ModuleEntryView(Protocol):
    dwSize: int
    th32ProcessID: int
    szModule: str
    szExePath: str


@final
class _ProcessEntry32(Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    )


@final
class _ModuleEntry32(Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", c_void_p),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * _MAX_MODULE_NAME),
        ("szExePath", wintypes.WCHAR * _MAX_PATH),
    )


class _Toolhelp(Protocol):
    def CreateToolhelp32Snapshot(self, flags: int, process_id: int) -> int | None: ...

    def CloseHandle(self, handle: int) -> int: ...

    def Process32FirstW(self, handle: int, entry: object) -> int: ...

    def Process32NextW(self, handle: int, entry: object) -> int: ...

    def Module32FirstW(self, handle: int, entry: object) -> int: ...

    def Module32NextW(self, handle: int, entry: object) -> int: ...


def _load_toolhelp() -> _Toolhelp:
    kernel32 = WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, c_void_p)
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, c_void_p)
    kernel32.Module32FirstW.restype = wintypes.BOOL
    kernel32.Module32FirstW.argtypes = (wintypes.HANDLE, c_void_p)
    kernel32.Module32NextW.restype = wintypes.BOOL
    kernel32.Module32NextW.argtypes = (wintypes.HANDLE, c_void_p)
    return cast(_Toolhelp, cast(object, kernel32))


_KERNEL32: Final = _load_toolhelp()


def _open_snapshot(flags: int, process_id: int) -> int | None:
    raw = _KERNEL32.CreateToolhelp32Snapshot(flags, process_id)
    handle = 0 if raw is None else int(raw)
    return None if handle in {0, _INVALID_HANDLE} else handle


def _bridge_host_processes() -> tuple[tuple[int, str], ...]:
    """Running processes that are supposed to have the bridge mapped."""
    handle = _open_snapshot(_TH32CS_SNAPPROCESS, 0)
    if handle is None:
        raise OSError(get_last_error(), "프로세스 스냅숏을 만들지 못했습니다")
    entry = _ProcessEntry32()
    view = cast(_ProcessEntryView, cast(object, entry))
    view.dwSize = sizeof(_ProcessEntry32)
    found: list[tuple[int, str]] = []
    try:
        more = bool(_KERNEL32.Process32FirstW(handle, byref(entry)))
        while more:
            name = view.szExeFile
            if name.casefold() in BRIDGE_HOST_PROCESS_NAMES:
                found.append((view.th32ProcessID, name))
            more = bool(_KERNEL32.Process32NextW(handle, byref(entry)))
    finally:
        _ = _KERNEL32.CloseHandle(handle)
    return tuple(found)


def _loaded_bridge_modules(
    process_id: int,
    process_name: str,
) -> tuple[LoadedBridgeModule, ...] | None:
    """None when this process's module list could not be read at all."""
    handle = _open_snapshot(
        _TH32CS_SNAPMODULE | _TH32CS_SNAPMODULE32,
        process_id,
    )
    if handle is None:
        return None
    entry = _ModuleEntry32()
    view = cast(_ModuleEntryView, cast(object, entry))
    view.dwSize = sizeof(_ModuleEntry32)
    found: list[LoadedBridgeModule] = []
    try:
        more = bool(_KERNEL32.Module32FirstW(handle, byref(entry)))
        if not more:
            return None
        while more:
            if view.szModule.casefold().startswith(BRIDGE_MODULE_PREFIX):
                path = Path(view.szExePath)
                found.append(
                    LoadedBridgeModule(
                        process_id=process_id,
                        process_name=process_name,
                        path=path,
                        version=bridge_version_from_path(path),
                    )
                )
            more = bool(_KERNEL32.Module32NextW(handle, byref(entry)))
    finally:
        _ = _KERNEL32.CloseHandle(handle)
    return tuple(found)


def scan_loaded_native_bridges() -> LoadedBridgeScan:
    """Never raises: a failed scan is reported, it does not block any work."""
    try:
        processes = _bridge_host_processes()
    except (OSError, ValueError) as error:
        return LoadedBridgeScan((), (), (), f"한컴 프로세스 목록 조회 실패: {error}")
    modules: list[LoadedBridgeModule] = []
    unreadable: list[int] = []
    for process_id, process_name in processes:
        try:
            found = _loaded_bridge_modules(process_id, process_name)
        except (OSError, ValueError):
            found = None
        if found is None:
            unreadable.append(process_id)
            continue
        modules.extend(found)
    return LoadedBridgeScan(processes, tuple(modules), tuple(unreadable), None)


def write_desktop_registration_result(package_root: Path, result_path: Path) -> None:
    try:
        # Do not dispatch another helper, even if the host inherited an env flag.
        registration = install_native_bridge_current_process(package_root=package_root)
        verify_native_bridge_registration(registration)
        result = DesktopRegistrationResult(registration=registration)
    except Exception as error:
        result = DesktopRegistrationResult(error=str(error))
    temporary = result_path.with_suffix(".tmp")
    _ = temporary.write_text(result.model_dump_json(), encoding="utf-8")
    _ = temporary.replace(result_path)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--register-desktop":
        raise SystemExit(
            "usage: hwp_native_install.py --register-desktop PACKAGE_ROOT RESULT"
        )
    write_desktop_registration_result(Path(sys.argv[2]), Path(sys.argv[3]))

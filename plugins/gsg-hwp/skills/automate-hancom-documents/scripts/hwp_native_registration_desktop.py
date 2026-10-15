from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from pydantic import BaseModel, ValidationError

from hwp_errors import HwpLiveError


@dataclass(frozen=True, slots=True)
class NativeBridgeRegistration:
    path: Path
    copied: bool
    registered_path: Path | None = None


class DesktopRegistrationResult(BaseModel):
    registration: NativeBridgeRegistration | None = None
    error: str | None = None


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _launch_desktop_registration(package_root: Path, result_path: Path) -> None:
    # WMI starts the helper outside the caller's MSIX registry silo. Elevation,
    # 32/64-bit registry flags and CREATE_BREAKAWAY_FROM_JOB do not do that.
    command = subprocess.list2cmdline(
        [
            sys.executable,
            str(Path(__file__).with_name("hwp_native_install.py").resolve()),
            "--register-desktop",
            str(package_root),
            str(result_path),
        ]
    )
    script = f"""
$ErrorActionPreference = 'Stop'
$startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{{ShowWindow=[uint16]0}}
$launch = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine={_ps_literal(command)}; ProcessStartupInformation=$startup}}
if ($launch.ReturnValue -ne 0) {{
    throw ('Desktop bridge registration launch failed: ' + $launch.ReturnValue)
}}
Wait-Process -Id $launch.ProcessId -Timeout 15 -ErrorAction SilentlyContinue
if (!(Test-Path -LiteralPath {_ps_literal(str(result_path))})) {{
    throw 'Desktop bridge registration did not return a result'
}}
exit 0
"""
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        process = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise HwpLiveError(
            f"일반 한컴용 브리지 등록 프로세스를 완료하지 못했습니다: {error}"
        ) from error
    if process.returncode != 0:
        raise HwpLiveError(
            "일반 한컴용 브리지 등록을 확인하지 못했습니다: "
            + (process.stderr.strip() or process.stdout.strip())[-2000:]
        )


def register_native_bridge_on_desktop(package_root: Path) -> NativeBridgeRegistration:
    with TemporaryDirectory(prefix="hwp-native-registration-") as directory:
        result_path = Path(directory) / "result.json"
        _launch_desktop_registration(package_root.resolve(), result_path)
        try:
            result = DesktopRegistrationResult.model_validate_json(
                result_path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError) as error:
            raise HwpLiveError(
                f"일반 한컴용 브리지 등록 결과를 읽지 못했습니다: {error}"
            ) from error
    if result.error is not None or result.registration is None:
        raise HwpLiveError(
            "일반 한컴용 브리지 등록 검증이 실패했습니다: "
            + (result.error or "검증된 등록 결과가 없습니다")
        )
    return result.registration

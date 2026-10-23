"""Prepare WSL before a per-user Docker Desktop installation on Windows."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kilntainers.errors import BackendError

MINIMUM_WSL = (2, 1, 5)


def decode_windows_output(output: bytes) -> str:
    """WSL uses UTF-16LE even when redirected; WinGet normally uses UTF-8."""
    if output.startswith((b"\xff\xfe", b"\xfe\xff")):
        return output.decode("utf-16", errors="replace").strip()
    if b"\x00" in output[:256]:
        return output.decode("utf-16-le", errors="replace").strip()
    return output.decode("utf-8-sig", errors="replace").strip()


def _system_executable(relative: str) -> str:
    return str(
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / relative
    )


def _powershell(script: str, timeout: int = 30) -> dict[str, Any]:
    executable = _system_executable(r"WindowsPowerShell\v1.0\powershell.exe")
    try:
        result = subprocess.run(
            [executable, "-NoProfile", "-NonInteractive", "-Command", script],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as error:
        raise BackendError(
            "Windows prerequisite setup timed out. A Windows setup process may "
            "still be running; let it finish before retrying."
        ) from error
    except OSError as error:
        raise BackendError(f"Could not check Windows prerequisites: {error}") from error
    if result.returncode:
        raise BackendError(
            "Could not prepare Windows prerequisites: "
            + decode_windows_output(result.stderr)[-2_000:]
        )
    try:
        data = json.loads(decode_windows_output(result.stdout))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        return data
    except ValueError as error:
        raise BackendError(
            "Windows prerequisite check returned invalid output."
        ) from error


def _machine_status() -> dict[str, Any]:
    return _powershell("""$ErrorActionPreference = 'Stop'
$computer = Get-CimInstance Win32_ComputerSystem
$processors = @(Get-CimInstance Win32_Processor)
$feature = Get-CimInstance Win32_OptionalFeature -Filter "Name='VirtualMachinePlatform'"
@{
    hypervisor = [bool]$computer.HypervisorPresent
    virtualization = @($processors | ForEach-Object { $_.VirtualizationFirmwareEnabled })
    virtualMachinePlatform = $feature.InstallState
} | ConvertTo-Json -Compress
""")


def _wsl_version() -> tuple[int, int, int] | None:
    try:
        result = subprocess.run(
            [_system_executable("wsl.exe"), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=20,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    # The first version is WSL itself; do not depend on its localized label.
    match = re.search(
        r"\b(\d+)\.(\d+)\.(\d+)(?:\.\d+)?\b", decode_windows_output(result.stdout)
    )
    if result.returncode or match is None:
        return None
    return (int(match[1]), int(match[2]), int(match[3]))


def _configure_wsl(*, install: bool) -> int:
    # Only this fixed OS setup command is elevated, never the MCP server.
    # Windows displays UAC; cancelling is an actionable result, not an install retry.
    arguments = (
        "'--install','--no-distribution','--web-download'"
        if install
        else "'--update','--web-download'"
    )
    result = _powershell(
        r"""
$ErrorActionPreference = 'Stop'
try {
    $wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
    $process = Start-Process -FilePath $wsl -ArgumentList ARGUMENTS -Verb RunAs -WindowStyle Hidden -Wait -PassThru
    @{ exitCode = $process.ExitCode } | ConvertTo-Json -Compress
} catch {
    if ($_.Exception.NativeErrorCode -eq 1223) {
        @{ exitCode = 1223 } | ConvertTo-Json -Compress
    } else { throw }
}
""".replace("ARGUMENTS", arguments),
        timeout=1_200,
    )
    return int(result["exitCode"])


def prepare_windows_wsl(report: Callable[[str], None]) -> None:
    """Check compatibility, prepare WSL through UAC, and stop at a required reboot."""
    version = sys.getwindowsversion()
    if version.product_type != 1:
        raise BackendError(
            "Docker Desktop requires Windows 10/11, not Windows Server. "
            "Use a Windows 11 client VM for Docker Desktop installation testing."
        )
    if version.build < 19045 or 22000 <= version.build < 22631:
        raise BackendError(
            "This Windows build is too old for Docker Desktop. Update to a supported "
            "Windows 10 22H2 or Windows 11 23H2 (or later) release, then retry."
        )
    report("Checking WSL 2 and hardware virtualization...")
    machine = _machine_status()
    firmware = machine.get("virtualization", [])
    if (
        not machine.get("hypervisor")
        and firmware
        and all(value is False for value in firmware)
    ):
        raise BackendError(
            "Hardware virtualization is disabled. Enable Intel VT-x/AMD-V in "
            "BIOS/UEFI, then restart Windows. For a VM, enable nested virtualization "
            "on its host first."
        )
    current = _wsl_version()
    install = current is None or machine.get("virtualMachinePlatform") != 1
    if install or current is None or current < MINIMUM_WSL:
        report(
            "Preparing WSL 2. Accept the Windows administrator prompt to continue. "
            "Windows may need a restart; setup will not restart it automatically."
        )
        code = _configure_wsl(install=install)
        if code == 1223:
            raise BackendError(
                "WSL setup was cancelled at the Windows administrator prompt. "
                "Retry and approve that prompt, or ask an administrator to run "
                "'wsl --install --no-distribution --web-download'."
            )
        if code in {3010, 1641}:
            raise BackendError(
                "WSL setup requires a Windows restart. Restart, reopen FLUJO, and retry the computer."
            )
        if code != 0:
            command = (
                "wsl --install --no-distribution --web-download"
                if install
                else "wsl --update --web-download"
            )
            raise BackendError(
                f"WSL setup failed (exit {code}). Run '{command}' in an administrator "
                "terminal to see Windows' diagnostic, then restart if requested and retry."
            )
        machine = _machine_status()
        current = _wsl_version()
    if machine.get("virtualMachinePlatform") != 1 or not machine.get("hypervisor"):
        raise BackendError(
            "WSL 2 cannot start yet. Restart Windows to activate Virtual Machine "
            "Platform, then reopen FLUJO and retry. If it still fails, check BIOS/UEFI "
            "virtualization and nested virtualization when using a VM."
        )
    if current is None or current < MINIMUM_WSL:
        raise BackendError(
            "Docker Desktop needs WSL 2.1.5 or later. Run 'wsl --update --web-download' "
            "in an administrator terminal, restart if requested, and retry."
        )

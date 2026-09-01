"""Lazy Docker Desktop bootstrap for local Windows computers.

The MCP server itself must remain available while Docker is being installed, so
this module is intentionally synchronous and is run in a worker thread by the
Docker backend. Progress callbacks contain only observed phases and, when
WinGet prints byte counts, genuine download totals.
"""

from __future__ import annotations

import ctypes
import importlib
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kilntainers.config import env_flag
from kilntainers.errors import BackendError

DOCKER_DESKTOP_PACKAGE = "Docker.DockerDesktop"
DOCKER_INSTALL_TIMEOUT_SECONDS = 1_200
DOCKER_START_TIMEOUT_SECONDS = 240
_DOWNLOAD_RE = re.compile(
    r"(?P<done>[\d.,]+)\s*(?P<done_unit>[KMGT]?i?B)\s*/\s*"
    r"(?P<total>[\d.,]+)\s*(?P<total_unit>[KMGT]?i?B)",
    re.IGNORECASE,
)
_UNIT_FACTORS = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KIB": 1_024,
    "MIB": 1_048_576,
    "GIB": 1_073_741_824,
    "TIB": 1_099_511_627_776,
}


@dataclass(frozen=True, slots=True)
class DockerRuntimeProgress:
    """One genuine state change from Windows Docker preparation."""

    state: str
    phase: str
    message: str
    progress: float
    total: float = 4.0
    downloaded_bytes: int | None = None
    download_total_bytes: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "runtime_state": self.state,
            "runtime_phase": self.phase,
            "runtime_message": self.message,
            "runtime_progress": self.progress,
            "runtime_total": self.total,
            "downloaded_bytes": self.downloaded_bytes,
            "download_total_bytes": self.download_total_bytes,
            "runtime_error": self.error,
        }


ProgressCallback = Callable[[DockerRuntimeProgress], None]


def windows_docker_bootstrap_required(engine: str) -> bool:
    """Return whether ``engine`` is the default Docker CLI on Windows."""
    return sys.platform == "win32" and Path(engine).name.casefold() in {
        "docker",
        "docker.exe",
    }


def initial_docker_runtime_status(engine: str) -> DockerRuntimeProgress:
    """Return the truthful status before lazy preparation begins."""
    if windows_docker_bootstrap_required(engine):
        return DockerRuntimeProgress(
            state="pending",
            phase="pending",
            message="Docker will be checked on first use.",
            progress=0.0,
        )
    return DockerRuntimeProgress(
        state="ready",
        phase="ready",
        message="Container runtime bootstrap is not required.",
        progress=4.0,
    )


def _timeout_from_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        timeout = int(value)
    except ValueError as error:
        raise BackendError(f"{name} must be a whole number of seconds.") from error
    if timeout < 1:
        raise BackendError(f"{name} must be at least 1 second.")
    return timeout


def _docker_cli_candidates() -> list[Path]:
    candidates: list[Path] = []
    local_app_data = os.getenv("LOCALAPPDATA")
    program_files = os.getenv("ProgramFiles")
    if local_app_data:
        root = Path(local_app_data) / "Programs" / "DockerDesktop"
        candidates.extend(
            [
                root / "resources" / "bin" / "docker.exe",
                root / "resources" / "docker.exe",
            ]
        )
    if program_files:
        root = Path(program_files) / "Docker" / "Docker"
        candidates.extend(
            [
                root / "resources" / "bin" / "docker.exe",
                root / "resources" / "docker.exe",
            ]
        )
    return candidates


def _docker_desktop_candidates() -> list[Path]:
    candidates: list[Path] = []
    local_app_data = os.getenv("LOCALAPPDATA")
    program_files = os.getenv("ProgramFiles")
    if local_app_data:
        candidates.append(
            Path(local_app_data)
            / "Programs"
            / "DockerDesktop"
            / "Docker Desktop.exe"
        )
    if program_files:
        candidates.append(
            Path(program_files) / "Docker" / "Docker" / "Docker Desktop.exe"
        )
    return candidates


def _find_docker(engine: str = "docker") -> tuple[Path | None, bool]:
    """Return the Docker CLI and whether its directory is missing from PATH."""
    explicit = Path(engine)
    if explicit.is_absolute() and explicit.is_file():
        return explicit.resolve(), shutil.which(explicit.name) is None

    discovered = shutil.which(engine)
    if discovered:
        return Path(discovered).resolve(), False

    for candidate in _docker_cli_candidates():
        if candidate.is_file():
            return candidate.resolve(), True
    return None, False


def _normalized_path_entry(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.expandvars(value.strip(' "'))))


def _path_contains(value: str, entry: Path) -> bool:
    target = _normalized_path_entry(str(entry))
    return any(
        _normalized_path_entry(item) == target
        for item in value.split(";")
        if item.strip()
    )


def _broadcast_environment_change() -> None:
    """Tell already-running Windows apps that the user environment changed."""
    try:
        windll: Any = getattr(ctypes, "windll")
        user32 = windll.user32
        result = ctypes.c_size_t()
        user32.SendMessageTimeoutW(
            0xFFFF,
            0x001A,
            0,
            "Environment",
            0x0002,
            5_000,
            ctypes.byref(result),
        )
    except (AttributeError, OSError):
        pass


def _persist_user_path(directory: Path) -> None:
    """Append a directory to HKCU's PATH without ``setx`` truncation."""
    # Import dynamically because ``winreg`` only exposes its API on Windows.
    winreg = cast(Any, importlib.import_module("winreg"))

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        try:
            current, value_type = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            current, value_type = "", winreg.REG_EXPAND_SZ
        current = str(current)
        if _path_contains(current, directory):
            return
        updated = f"{current.rstrip(';')};{directory}" if current else str(directory)
        if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ}:
            value_type = winreg.REG_EXPAND_SZ
        winreg.SetValueEx(key, "Path", 0, value_type, updated)
    _broadcast_environment_change()


def _ensure_docker_on_path(docker: Path, persist: bool) -> None:
    directory = docker.parent
    process_path = os.environ.get("PATH", "")
    if not _path_contains(process_path, directory):
        os.environ["PATH"] = (
            f"{directory};{process_path}" if process_path else str(directory)
        )
    if persist:
        try:
            _persist_user_path(directory)
        except OSError as error:
            raise BackendError(
                "Docker was found, but its directory could not be added to the "
                f"current user's PATH: {error}"
            ) from error


def _bytes(value: str, unit: str) -> int:
    number = float(value.replace(",", ""))
    return int(number * _UNIT_FACTORS[unit.upper()])


def _download_amount(text: str) -> tuple[int, int] | None:
    matches = list(_DOWNLOAD_RE.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    downloaded = _bytes(match.group("done"), match.group("done_unit"))
    total = _bytes(match.group("total"), match.group("total_unit"))
    if total <= 0 or downloaded < 0:
        return None
    return min(downloaded, total), total


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _install_with_winget(
    winget: str,
    report: ProgressCallback,
    timeout: int,
) -> None:
    """Install per-user Docker Desktop and stream observable WinGet state."""
    command = [
        winget,
        "install",
        "--id",
        DOCKER_DESKTOP_PACKAGE,
        "--exact",
        "--source",
        "winget",
        "--silent",
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
        "--override",
        "install --user --quiet --accept-license",
    ]
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags,
        )
    except OSError as error:
        raise BackendError(f"Could not start WinGet: {error}") from error

    chunks: queue.Queue[bytes | None] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        read = getattr(process.stdout, "read1", process.stdout.read)
        try:
            while chunk := read(4_096):
                chunks.put(chunk)
        finally:
            chunks.put(None)

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + timeout
    output = bytearray()
    last_downloaded = -1
    installing_reported = False

    while True:
        if time.monotonic() >= deadline:
            _terminate_process(process)
            raise BackendError(
                f"Docker Desktop installation exceeded {timeout} seconds. "
                "The setup task was stopped; retry after checking WinGet and your "
                "internet connection."
            )
        try:
            chunk = chunks.get(timeout=0.2)
        except queue.Empty:
            if process.poll() is not None:
                continue
            continue
        if chunk is None:
            break
        output.extend(chunk)
        del output[:-65_536]
        decoded = output.decode("utf-8", errors="replace")
        lowered = decoded.casefold()
        amount = _download_amount(decoded)
        if amount is not None and amount[0] > last_downloaded:
            last_downloaded = amount[0]
            ratio = amount[0] / amount[1]
            report(
                DockerRuntimeProgress(
                    state="working",
                    phase="downloading",
                    message="Downloading Docker...",
                    progress=1.0 + ratio * 0.99,
                    downloaded_bytes=amount[0],
                    download_total_bytes=amount[1],
                )
            )
        if not installing_reported and (
            "starting package install" in lowered
            or "successfully verified installer hash" in lowered
        ):
            installing_reported = True
            report(
                DockerRuntimeProgress(
                    state="working",
                    phase="installing",
                    message="Installing Docker Desktop...",
                    progress=2.0,
                )
            )

    return_code = process.wait(timeout=5)
    if return_code != 0:
        detail = output.decode("utf-8", errors="replace").strip()
        detail = detail[-2_000:] if detail else "WinGet returned no diagnostic output."
        raise BackendError(
            f"WinGet could not install {DOCKER_DESKTOP_PACKAGE} "
            f"(exit {return_code}).\n{detail}"
        )


def _docker_ready(docker: Path) -> bool:
    try:
        result = subprocess.run(
            [str(docker), "info"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _launch_docker_desktop(docker: Path, timeout: int) -> None:
    """Start Docker Desktop through its supported CLI, with a GUI fallback."""
    try:
        result = subprocess.run(
            [str(docker), "desktop", "start", "--timeout", str(timeout)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout + 30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0:
            return
    except (OSError, subprocess.TimeoutExpired):
        pass

    application = next(
        (candidate for candidate in _docker_desktop_candidates() if candidate.is_file()),
        None,
    )
    if application is None:
        raise BackendError(
            "Docker Desktop was installed, but Docker Desktop.exe could not be found."
        )
    try:
        subprocess.Popen(
            [str(application)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as error:
        raise BackendError(f"Docker Desktop could not be started: {error}") from error


def _wsl_diagnostic() -> str:
    wsl = shutil.which("wsl.exe") or shutil.which("wsl")
    if wsl is None:
        return (
            "WSL is not installed. Run 'wsl --install' in an elevated terminal, "
            "then restart Windows."
        )
    try:
        result = subprocess.run(
            [wsl, "--status"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Check that WSL 2 and hardware virtualization are enabled."
    detail = result.stdout.decode("utf-8", errors="replace").strip()
    return detail or "Check that WSL 2 and hardware virtualization are enabled."


def prepare_windows_docker_runtime(
    *,
    engine: str,
    host: str | None,
    report: ProgressCallback,
) -> None:
    """Find, install, add, and start Docker Desktop when Windows needs it."""
    if not windows_docker_bootstrap_required(engine):
        report(initial_docker_runtime_status(engine))
        return

    report(
        DockerRuntimeProgress(
            state="working",
            phase="checking",
            message="Checking Docker...",
            progress=0.0,
        )
    )
    docker, path_missing = _find_docker(engine)
    if docker is None:
        try:
            auto_install = env_flag("AUTO_INSTALL_DOCKER", default=True)
        except ValueError as error:
            raise BackendError(str(error)) from error
        if not auto_install:
            raise BackendError(
                "Docker was not found and AUTO_INSTALL_DOCKER=false. Install Docker "
                "Desktop or set AUTO_INSTALL_DOCKER=true."
            )
        winget = shutil.which("winget") or shutil.which("winget.exe")
        if winget is None:
            raise BackendError(
                "Docker was not found, and WinGet is unavailable. Install Microsoft's "
                "App Installer (which provides winget), or install Docker Desktop from "
                "https://docs.docker.com/desktop/setup/install/windows-install/."
            )
        report(
            DockerRuntimeProgress(
                state="working",
                phase="downloading",
                message="Downloading Docker...",
                progress=1.0,
            )
        )
        _install_with_winget(
            winget,
            report,
            _timeout_from_env(
                "DOCKER_INSTALL_TIMEOUT",
                DOCKER_INSTALL_TIMEOUT_SECONDS,
            ),
        )
        report(
            DockerRuntimeProgress(
                state="working",
                phase="installing",
                message="Finishing Docker Desktop installation...",
                progress=2.0,
            )
        )
        docker, path_missing = _find_docker(engine)
        if docker is None:
            raise BackendError(
                "WinGet completed, but docker.exe was not found in Docker Desktop's "
                "per-user or all-users install locations. Sign out and back in, then retry."
            )

    _ensure_docker_on_path(docker, path_missing)
    if host is not None or _docker_ready(docker):
        report(
            DockerRuntimeProgress(
                state="ready",
                phase="ready",
                message="Docker is ready.",
                progress=4.0,
            )
        )
        return

    start_timeout = _timeout_from_env(
        "DOCKER_START_TIMEOUT",
        DOCKER_START_TIMEOUT_SECONDS,
    )
    report(
        DockerRuntimeProgress(
            state="working",
            phase="starting",
            message="Starting Docker Desktop...",
            progress=3.0,
        )
    )
    _launch_docker_desktop(docker, start_timeout)
    deadline = time.monotonic() + start_timeout
    while time.monotonic() < deadline:
        if _docker_ready(docker):
            report(
                DockerRuntimeProgress(
                    state="ready",
                    phase="ready",
                    message="Docker is ready.",
                    progress=4.0,
                )
            )
            return
        time.sleep(2)

    raise BackendError(
        "Docker Desktop was installed but its Linux engine did not become ready. "
        "A one-time Windows restart or WSL setup may be required. "
        f"WSL status: {_wsl_diagnostic()}"
    )

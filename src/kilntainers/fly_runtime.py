"""Locate or install flyctl without requiring administrator privileges."""

from __future__ import annotations

import os
import platform
import shutil
import stat
import tarfile
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from kilntainers.errors import BackendError

_INSTALL_LOCK = threading.Lock()
_RELEASE_ENDPOINT = "https://api.fly.io/app/flyctl_releases/{system}/{arch}/latest"
_USER_AGENT = "mcp-virtual-computer flyctl bootstrap"


def _env_flag(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise BackendError(
        f"{name} must be true or false; got {value!r}."
    )


def _install_root() -> Path:
    configured = os.getenv("FLYCTL_INSTALL")
    return Path(configured).expanduser() if configured else Path.home() / ".fly"


def _executable_name() -> str:
    return "flyctl.exe" if os.name == "nt" else "flyctl"


def _installed_flyctl() -> Path:
    return _install_root() / "bin" / _executable_name()


def _platform_release() -> tuple[str, str, bool]:
    system_name = platform.system()
    if system_name == "Windows":
        release_system = "windows"
        zipped = True
    elif system_name in {"Linux", "Darwin"}:
        release_system = system_name
        zipped = False
    else:
        raise BackendError(
            f"Automatic flyctl installation is not supported on {system_name}. "
            "Install flyctl manually and pass --fly-cli."
        )

    machine = platform.machine().casefold()
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine)
    if architecture is None:
        raise BackendError(
            f"Automatic flyctl installation does not support architecture {machine!r}. "
            "Install flyctl manually and pass --fly-cli."
        )
    return release_system, architecture, zipped


def _open_url(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(request, timeout=60)


def _download(url: str, destination: Path) -> None:
    with _open_url(url) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output, length=1024 * 1024)


def _safe_name(raw_name: str) -> str:
    name = PurePosixPath(raw_name).name
    if not name or name in {".", ".."}:
        raise BackendError("The flyctl release archive contains an invalid filename.")
    return name


def _extract_release(archive: Path, destination: Path, *, zipped: bool) -> None:
    wanted = {"flyctl.exe", "fly.exe", "wintun.dll"} if zipped else {"flyctl"}
    extracted: set[str] = set()
    if zipped:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                name = _safe_name(member.filename)
                if name not in wanted or member.is_dir():
                    continue
                with bundle.open(member) as source, (destination / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted.add(name)
    else:
        with tarfile.open(archive, mode="r:gz") as bundle:
            for member in bundle.getmembers():
                name = _safe_name(member.name)
                if name not in wanted or not member.isfile():
                    continue
                source = bundle.extractfile(member)
                if source is None:
                    continue
                with source, (destination / name).open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted.add(name)

    if _executable_name() not in extracted:
        raise BackendError("The downloaded flyctl archive did not contain flyctl.")


def install_flyctl() -> Path:
    """Download the current official flyctl release into ``~/.fly/bin``."""
    system_name, architecture, zipped = _platform_release()
    endpoint = _RELEASE_ENDPOINT.format(system=system_name, arch=architecture)
    try:
        with _open_url(endpoint) as response:
            release_url = response.read(8192).decode("utf-8").strip()
    except Exception as error:
        raise BackendError(f"Could not resolve the current flyctl release: {error}") from error
    if not release_url.startswith("https://"):
        raise BackendError("Fly.io returned an invalid flyctl download URL.")

    bin_directory = _install_root() / "bin"
    bin_directory.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="flyctl-", dir=bin_directory) as raw_temp:
            temp = Path(raw_temp)
            archive = temp / ("flyctl.zip" if zipped else "flyctl.tar.gz")
            _download(release_url, archive)
            _extract_release(archive, temp, zipped=zipped)
            for name in ({"flyctl.exe", "fly.exe", "wintun.dll"} if zipped else {"flyctl"}):
                source = temp / name
                if not source.exists():
                    continue
                if name.startswith("fly") and os.name != "nt":
                    source.chmod(source.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                os.replace(source, bin_directory / name)
    except BackendError:
        raise
    except Exception as error:
        raise BackendError(f"Could not download and install flyctl: {error}") from error

    executable = _installed_flyctl()
    if not executable.is_file():
        raise BackendError(f"flyctl installation completed but {executable} was not found.")
    if os.name != "nt":
        alias = executable.with_name("fly")
        try:
            if alias.exists() or alias.is_symlink():
                alias.unlink()
            alias.symlink_to(executable.name)
        except OSError:
            pass
    return executable


def ensure_flyctl(command: str = "fly") -> str:
    """Return a usable flyctl path, installing it automatically when absent."""
    resolved = shutil.which(command)
    if resolved:
        return resolved

    direct = Path(command).expanduser()
    if direct.is_file():
        return str(direct.resolve())

    installed = _installed_flyctl()
    if installed.is_file():
        return str(installed)

    if Path(command).name.casefold() not in {"fly", "flyctl", "fly.exe", "flyctl.exe"}:
        raise BackendError(
            f"Fly CLI {command!r} was not found. Correct --fly-cli or remove it "
            "to enable automatic installation."
        )
    if not _env_flag("AUTO_INSTALL_FLYCTL", default=True):
        raise BackendError(
            "flyctl was not found and AUTO_INSTALL_FLYCTL=false. Install flyctl "
            "manually or enable automatic installation."
        )

    with _INSTALL_LOCK:
        if installed.is_file():
            return str(installed)
        return str(install_flyctl())

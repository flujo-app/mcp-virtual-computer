"""Tests for the lazy Windows Docker Desktop bootstrap."""

import asyncio
import io
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kilntainers import windows_docker as bootstrap
from kilntainers.backends import docker as docker_backend_module
from kilntainers.backends.docker import DockerBackend, DockerBackendConfig
from kilntainers.errors import BackendError


def test_download_amount_parses_decimal_and_binary_units() -> None:
    assert bootstrap._download_amount(" 12.5 MB / 500 MB ") == (
        12_500_000,
        500_000_000,
    )
    assert bootstrap._download_amount("2 MiB / 8 MiB") == (2_097_152, 8_388_608)
    assert bootstrap._download_amount("no byte totals") is None


def test_custom_engine_does_not_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    updates: list[bootstrap.DockerRuntimeProgress] = []

    bootstrap.prepare_windows_docker_runtime(
        engine="podman",
        host=None,
        report=updates.append,
    )

    assert updates[-1].state == "ready"
    assert updates[-1].message == "Container runtime bootstrap is not required."


def test_existing_docker_repairs_path_and_skips_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    docker = tmp_path / "docker.exe"
    docker.touch()
    monkeypatch.setattr(bootstrap, "_find_docker", lambda engine: (docker, True))
    ensure_path = MagicMock()
    monkeypatch.setattr(bootstrap, "_ensure_docker_on_path", ensure_path)
    monkeypatch.setattr(bootstrap, "_docker_ready", lambda path: True)
    install = MagicMock()
    monkeypatch.setattr(bootstrap, "_install_with_winget", install)
    updates: list[bootstrap.DockerRuntimeProgress] = []

    bootstrap.prepare_windows_docker_runtime(
        engine="docker",
        host=None,
        report=updates.append,
    )

    ensure_path.assert_called_once_with(docker, True)
    install.assert_not_called()
    assert [update.phase for update in updates] == ["checking", "ready"]


def test_missing_docker_respects_auto_install_opt_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap, "_find_docker", lambda engine: (None, False))
    monkeypatch.setenv("AUTO_INSTALL_DOCKER", "false")

    with pytest.raises(BackendError, match="AUTO_INSTALL_DOCKER=false"):
        bootstrap.prepare_windows_docker_runtime(
            engine="docker",
            host=None,
            report=lambda update: None,
        )


def test_missing_winget_has_actionable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    monkeypatch.setattr(bootstrap, "_find_docker", lambda engine: (None, False))
    monkeypatch.delenv("AUTO_INSTALL_DOCKER", raising=False)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)

    with pytest.raises(BackendError, match="App Installer"):
        bootstrap.prepare_windows_docker_runtime(
            engine="docker",
            host=None,
            report=lambda update: None,
        )


def test_fresh_install_is_per_user_then_starts_engine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")
    docker = tmp_path / "docker.exe"
    find_results = iter([(None, False), (docker, True)])
    monkeypatch.setattr(bootstrap, "_find_docker", lambda engine: next(find_results))
    monkeypatch.setattr(
        bootstrap.shutil,
        "which",
        lambda name: "C:/Windows/winget.exe" if name == "winget" else None,
    )
    install = MagicMock()
    ensure_path = MagicMock()
    launch = MagicMock()
    ready_results = iter([False, True])
    monkeypatch.setattr(bootstrap, "_install_with_winget", install)
    monkeypatch.setattr(bootstrap, "_ensure_docker_on_path", ensure_path)
    monkeypatch.setattr(bootstrap, "_launch_docker_desktop", launch)
    monkeypatch.setattr(bootstrap, "_docker_ready", lambda path: next(ready_results))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda seconds: None)
    updates: list[bootstrap.DockerRuntimeProgress] = []

    bootstrap.prepare_windows_docker_runtime(
        engine="docker",
        host=None,
        report=updates.append,
    )

    install.assert_called_once_with(
        "C:/Windows/winget.exe",
        updates.append,
        bootstrap.DOCKER_INSTALL_TIMEOUT_SECONDS,
    )
    ensure_path.assert_called_once_with(docker, True)
    launch.assert_called_once_with(docker, bootstrap.DOCKER_START_TIMEOUT_SECONDS)
    assert updates[-1].state == "ready"
    assert {update.phase for update in updates} >= {
        "checking",
        "downloading",
        "installing",
        "starting",
        "ready",
    }


class _FakePopen:
    def __init__(self, command: list[str], output: bytes) -> None:
        self.command = command
        self.stdout = io.BytesIO(output)
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 1

    def kill(self) -> None:
        self.returncode = 1


def test_winget_command_is_exact_silent_per_user_and_streams_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_FakePopen] = []

    def fake_popen(command: list[str], **kwargs: object) -> _FakePopen:
        process = _FakePopen(
            command,
            b"Downloading 125 MB / 500 MB\rSuccessfully verified installer hash\r",
        )
        created.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    updates: list[bootstrap.DockerRuntimeProgress] = []

    bootstrap._install_with_winget("winget.exe", updates.append, timeout=5)

    command = created[0].command
    assert command[:5] == [
        "winget.exe",
        "install",
        "--id",
        "Docker.DockerDesktop",
        "--exact",
    ]
    assert command[command.index("--override") + 1] == (
        "install --user --quiet --accept-license"
    )
    download = next(update for update in updates if update.download_total_bytes)
    assert download.downloaded_bytes == 125_000_000
    assert download.download_total_bytes == 500_000_000


def test_process_path_is_updated_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    directory = tmp_path / "Docker" / "bin"
    docker = directory / "docker.exe"
    persist = MagicMock()
    monkeypatch.setattr(bootstrap, "_persist_user_path", persist)
    monkeypatch.setenv("PATH", "C:\\Windows")

    bootstrap._ensure_docker_on_path(docker, persist=True)
    bootstrap._ensure_docker_on_path(docker, persist=False)

    assert bootstrap._path_contains(bootstrap.os.environ["PATH"], directory)
    assert bootstrap.os.environ["PATH"].split(";").count(str(directory)) == 1
    persist.assert_called_once_with(directory)


@pytest.mark.asyncio
async def test_backend_bootstrap_survives_cancelled_tool_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")

    def prepare(
        *,
        engine: str,
        host: str | None,
        report: bootstrap.ProgressCallback,
    ) -> None:
        report(
            bootstrap.DockerRuntimeProgress(
                state="working",
                phase="downloading",
                message="Downloading Docker...",
                progress=1.0,
            )
        )
        time.sleep(0.05)
        report(
            bootstrap.DockerRuntimeProgress(
                state="ready",
                phase="ready",
                message="Docker is ready.",
                progress=4.0,
            )
        )

    monkeypatch.setattr(
        docker_backend_module,
        "prepare_windows_docker_runtime",
        prepare,
    )
    backend = DockerBackend(DockerBackendConfig())
    waiter = asyncio.create_task(backend.ensure_runtime())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    await asyncio.sleep(0.08)
    assert backend.runtime_status()["runtime_state"] == "ready"


@pytest.mark.asyncio
async def test_backend_reports_strictly_increasing_mcp_progress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bootstrap.sys, "platform", "win32")

    def prepare(
        *,
        engine: str,
        host: str | None,
        report: bootstrap.ProgressCallback,
    ) -> None:
        for update in (
            bootstrap.DockerRuntimeProgress(
                "working", "checking", "Checking Docker...", 0.0
            ),
            bootstrap.DockerRuntimeProgress(
                "working", "downloading", "Downloading Docker...", 1.0
            ),
            bootstrap.DockerRuntimeProgress(
                "ready", "ready", "Docker is ready.", 4.0
            ),
        ):
            report(update)

    monkeypatch.setattr(
        docker_backend_module,
        "prepare_windows_docker_runtime",
        prepare,
    )
    backend = DockerBackend(DockerBackendConfig())
    received: list[float] = []

    async def receive(update: bootstrap.DockerRuntimeProgress) -> None:
        received.append(update.progress)

    await backend.ensure_runtime(receive)
    await asyncio.sleep(0)

    assert received == sorted(set(received))
    assert received[-1] == 4.0

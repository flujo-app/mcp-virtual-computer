"""Docker named-computer management tests."""

import json
from typing import cast

import pytest

from kilntainers.backends.docker import (
    COMPUTER_ID_LABEL,
    IMAGE_LABEL,
    TEMPORARY_LABEL,
    DockerBackend,
    DockerBackendConfig,
    DockerSandbox,
)


def inspect_row(
    computer_id: str = "dev-box",
    *,
    temporary: bool = False,
    running: bool = True,
) -> dict[str, object]:
    return {
        "Id": "a" * 64,
        "Created": "2026-08-12T12:00:00Z",
        "Config": {
            "Labels": {
                COMPUTER_ID_LABEL: computer_id,
                TEMPORARY_LABEL: str(temporary).lower(),
                IMAGE_LABEL: "debian:bookworm-slim",
            }
        },
        "State": {"Running": running, "Status": "running" if running else "exited"},
    }


def test_permanent_run_command_has_name_labels_and_no_auto_remove() -> None:
    backend = DockerBackend(DockerBackendConfig())
    command = backend._build_run_command(computer_id="dev-box", temporary=False)

    assert "--rm" not in command
    assert "kilntainer-dev-box" in command
    assert f"{COMPUTER_ID_LABEL}=dev-box" in command
    assert f"{TEMPORARY_LABEL}=false" in command


def test_temporary_run_command_uses_auto_remove() -> None:
    backend = DockerBackend(DockerBackendConfig())
    command = backend._build_run_command(computer_id="scratch", temporary=True)
    assert "--rm" in command
    assert f"{TEMPORARY_LABEL}=true" in command


@pytest.mark.asyncio
async def test_list_computers_reads_provider_labels(monkeypatch) -> None:
    backend = DockerBackend(DockerBackendConfig())
    backend._validated = True

    async def fake_run(*args, **kwargs):
        if args[:2] == ("ps", "-aq"):
            return 0, b"aaaaaaaaaaaa\n", b""
        if args[:2] == ("container", "inspect"):
            return 0, json.dumps([inspect_row()]).encode(), b""
        raise AssertionError(args)

    monkeypatch.setattr(backend, "_run_docker", fake_run)
    computers = await backend.list_computers()

    assert len(computers) == 1
    assert computers[0].computer_id == "dev-box"
    assert computers[0].sandbox_id == "a" * 12
    assert computers[0].temporary is False
    assert computers[0].state == "running"


@pytest.mark.asyncio
async def test_attach_starts_stopped_permanent_container(monkeypatch) -> None:
    backend = DockerBackend(DockerBackendConfig())
    backend._validated = True
    running = False
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, **kwargs):
        nonlocal running
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return 0, json.dumps([inspect_row(running=running)]).encode(), b""
        if args[0] == "start":
            running = True
            return 0, b"", b""
        raise AssertionError(args)

    async def ready(self):
        return None

    monkeypatch.setattr(backend, "_run_docker", fake_run)
    monkeypatch.setattr(DockerSandbox, "_verify_readiness", ready)
    sandbox = await backend.attach_sandbox("dev-box")

    assert sandbox is not None
    assert sandbox.computer_id == "dev-box"
    assert sandbox.temporary is False
    assert any(call[0] == "start" for call in calls)


@pytest.mark.asyncio
async def test_attach_preserves_live_desktop_and_network_state(monkeypatch) -> None:
    backend = DockerBackend(
        DockerBackendConfig(desktop_environment=False, network_enabled=True)
    )
    backend._validated = True
    row = inspect_row()

    async def inspect(_computer_id):
        return row

    async def ready(self):
        return None

    async def sync(_sandbox):
        return None

    async def read_desktop(sandbox):
        sandbox._desktop_environment = True
        return True

    async def read_network(sandbox):
        sandbox._network_access = False
        return False

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("attach must not reapply startup defaults")

    monkeypatch.setattr(backend, "_inspect_computer", inspect)
    monkeypatch.setattr(DockerSandbox, "_verify_readiness", ready)
    monkeypatch.setattr(backend, "_sync_desktop_helpers", sync)
    monkeypatch.setattr(backend, "_read_desktop_mode", read_desktop)
    monkeypatch.setattr(backend, "_read_network_access", read_network)
    monkeypatch.setattr(backend, "_set_desktop_mode", unexpected)
    monkeypatch.setattr(backend, "_set_network_access_on_sandbox", unexpected)

    sandbox = await backend.attach_sandbox("dev-box")

    assert sandbox is not None
    assert sandbox.desktop_environment is True
    assert sandbox.network_access is False


@pytest.mark.asyncio
async def test_refresh_reads_shared_modes_with_one_docker_exec(monkeypatch) -> None:
    backend = DockerBackend(DockerBackendConfig())
    row = inspect_row()
    network = cast(dict[str, object], row.setdefault("NetworkSettings", {}))
    assert isinstance(network, dict)
    network["Ports"] = {"6080/tcp": [{"HostPort": "49152"}]}
    sandbox = backend._sandbox_from_inspect(row)
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return 0, b"true\nfalse\n", b""

    monkeypatch.setattr(backend, "_run_docker", fake_run)

    refreshed = await backend.refresh_sandbox("dev-box", sandbox)

    assert refreshed is sandbox
    assert refreshed.desktop_environment is True
    assert refreshed.network_access is False
    assert len(calls) == 1
    assert calls[0][:3] == ("exec", "a" * 64, "sh")


@pytest.mark.asyncio
async def test_delete_uses_force_remove(monkeypatch) -> None:
    backend = DockerBackend(DockerBackendConfig())
    backend._validated = True
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("container", "inspect"):
            return 0, json.dumps([inspect_row()]).encode(), b""
        if args[:2] == ("rm", "-f"):
            return 0, b"", b""
        raise AssertionError(args)

    monkeypatch.setattr(backend, "_run_docker", fake_run)
    assert await backend.delete_computer("dev-box") is True
    assert any(call[:2] == ("rm", "-f") for call in calls)

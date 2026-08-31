"""Unit tests for the Fly Machines backend (no Fly account required)."""

import json

import pytest

from kilntainers.backends.base import ExecRequest
from kilntainers.backends.fly import (
    COMPUTER_ID_METADATA,
    IMAGE_METADATA,
    TEMPORARY_METADATA,
    FlyBackend,
    FlyBackendConfig,
    FlySandbox,
    _FlySandboxState,
)
from kilntainers.errors import BackendError


def machine_row(
    computer_id: str = "dev-box",
    *,
    temporary: bool = False,
    state: str = "started",
) -> dict[str, object]:
    return {
        "id": "e286de3f123456",
        "name": computer_id,
        "state": state,
        "created_at": "2026-08-12T12:00:00Z",
        "config": {
            "metadata": {
                COMPUTER_ID_METADATA: computer_id,
                TEMPORARY_METADATA: str(temporary).lower(),
                IMAGE_METADATA: "debian:bookworm-slim",
            }
        },
    }


@pytest.mark.asyncio
async def test_validate_requires_app_and_token() -> None:
    backend = FlyBackend(FlyBackendConfig(app=None, token=None))
    with pytest.raises(BackendError, match="--fly-app"):
        await backend.validate()

    backend = FlyBackend(FlyBackendConfig(app="sandbox-app", token=None))
    with pytest.raises(BackendError, match="FLY_API_TOKEN"):
        await backend.validate()


@pytest.mark.asyncio
async def test_list_computers_filters_to_owned_metadata(monkeypatch) -> None:
    backend = FlyBackend(FlyBackendConfig(app="sandbox-app", token="token"))
    backend._validated = True

    async def fake_rows():
        return [machine_row(), {"id": "host", "state": "started", "config": {}}]

    monkeypatch.setattr(backend, "_list_machine_rows", fake_rows)
    computers = await backend.list_computers()

    assert len(computers) == 1
    assert computers[0].computer_id == "dev-box"
    assert computers[0].temporary is False
    assert computers[0].backend == "fly"


@pytest.mark.asyncio
async def test_create_permanent_machine_uses_persistent_rootfs(monkeypatch) -> None:
    backend = FlyBackend(FlyBackendConfig(app="sandbox-app", token="token"))
    calls: list[tuple[str, ...]] = []
    created = False

    async def fake_run(*args, **kwargs):
        nonlocal created
        calls.append(args)
        if args[:2] == ("machine", "run"):
            created = True
        return 0, b"", b""

    async def fake_rows():
        return [machine_row()] if created else []

    async def ready(self):
        return None

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    monkeypatch.setattr(backend, "_list_machine_rows", fake_rows)
    monkeypatch.setattr(FlySandbox, "_verify_readiness", ready)

    sandbox = await backend._create_sandbox(
        computer_id="dev-box",
        temporary=False,
    )

    run_call = next(call for call in calls if call[:2] == ("machine", "run"))
    assert "--rootfs-persist" in run_call
    assert run_call[run_call.index("--rootfs-persist") + 1] == "always"
    assert run_call[run_call.index("--restart") + 1] == "always"
    assert "--rm" not in run_call
    assert sandbox.computer_id == "dev-box"
    assert sandbox.temporary is False


@pytest.mark.asyncio
async def test_create_removes_machine_when_readiness_fails(monkeypatch) -> None:
    backend = FlyBackend(FlyBackendConfig(app="sandbox-app", token="token"))
    created = False
    destroyed = False

    async def fake_run(*args, **kwargs):
        nonlocal created, destroyed
        if args[:2] == ("machine", "run"):
            created = True
        if args[:2] == ("machine", "destroy"):
            destroyed = True
        return 0, b"", b""

    async def fake_rows():
        return [machine_row()] if created and not destroyed else []

    async def not_ready(self):
        raise BackendError("readiness failed")

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    monkeypatch.setattr(backend, "_list_machine_rows", fake_rows)
    monkeypatch.setattr(FlySandbox, "_verify_readiness", not_ready)

    with pytest.raises(BackendError, match="readiness failed"):
        await backend._create_sandbox(computer_id="dev-box", temporary=False)

    assert destroyed is True


@pytest.mark.asyncio
async def test_fly_exec_parses_machine_json(monkeypatch) -> None:
    backend = FlyBackend(FlyBackendConfig(app="sandbox-app", token="token"))
    sandbox = FlySandbox(
        backend,
        _FlySandboxState(
            machine_id="e286de3f123456",
            computer_id="dev-box",
            temporary=False,
            image="debian:bookworm-slim",
        ),
    )
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return (
            0,
            json.dumps({"exit_code": 7, "stdout": "out\n", "stderr": "err\n"}).encode(),
            b"",
        )

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    result = await sandbox.exec(
        ExecRequest(command="echo test", timeout=30, output_limit=4096)
    )

    assert result.exit_code == 7
    assert result.stdout == "out\n"
    assert result.stderr == "err\n"
    assert calls[0][:2] == ("machine", "exec")
    assert "/bin/bash -c" in calls[0][-1]


def test_remote_command_supports_workdir_args_and_stdin() -> None:
    backend = FlyBackend(FlyBackendConfig(app="sandbox-app", token="token"))
    sandbox = FlySandbox(
        backend,
        _FlySandboxState(
            machine_id="machine",
            computer_id="dev-box",
            temporary=True,
            image="debian:bookworm-slim",
        ),
    )
    command = sandbox._remote_command(
        ExecRequest(
            args=["cat", "file with spaces"],
            stdin="hello",
            working_directory="/workspace dir",
            timeout=30,
            output_limit=4096,
        )
    )
    assert "cd -- '/workspace dir'" in command
    assert "base64 -d" in command
    assert "cat 'file with spaces'" in command

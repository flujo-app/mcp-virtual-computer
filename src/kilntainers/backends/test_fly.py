"""Unit tests for the persistent Xfce Fly backend (no Fly account required)."""

import json
import runpy
import shlex
from pathlib import Path

import pytest

from kilntainers.backends.base import ExecRequest
from kilntainers.backends.fly import (
    COMPUTER_ID_METADATA,
    DESKTOP_METADATA,
    IMAGE_METADATA,
    NETWORK_METADATA,
    TEMPORARY_METADATA,
    VNC_TOKEN_METADATA,
    FlyBackend,
    FlyBackendConfig,
    FlySandbox,
)
from kilntainers.errors import BackendError


def machine_row(*, state: str = "started") -> dict[str, object]:
    return {
        "id": "e286de3f123456",
        "name": "agent-workstation",
        "state": state,
        "created_at": "2026-09-02T12:00:00Z",
        "config": {
            "metadata": {
                COMPUTER_ID_METADATA: "agent-workstation",
                TEMPORARY_METADATA: "false",
                IMAGE_METADATA: "mcp-virtual-computer-desktop:fly",
                DESKTOP_METADATA: "true",
                NETWORK_METADATA: "true",
                VNC_TOKEN_METADATA: "private-vnc-token",
            }
        },
    }


@pytest.mark.asyncio
async def test_validate_installs_cli_and_creates_default_app(
    monkeypatch, tmp_path
) -> None:
    backend = FlyBackend(
        FlyBackendConfig(computer_id="agent-workstation", app=None, token=None)
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setenv("KILNTAINERS_FLY_STATE_FILE", str(tmp_path / "fly.json"))
    monkeypatch.setattr(
        "kilntainers.backends.fly.ensure_flyctl", lambda command: "/tools/flyctl"
    )

    async def fake_run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("apps", "list"):
            return 0, b"[]", b""
        if args[:2] == ("orgs", "list"):
            return 0, b'{"personal":"account@example.com"}', b""
        if args[:2] == ("apps", "create"):
            return 0, b'{"Name":"generated-desktop-app"}', b""
        if args[:2] == ("machine", "list"):
            return 0, b"[]", b""
        return 0, b"{}", b""

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    await backend.validate()

    assert backend.app == "generated-desktop-app"
    assert json.loads((tmp_path / "fly.json").read_text()) == {
        "apps": {"agent-workstation": "generated-desktop-app"}
    }
    assert ("apps", "create", "--generate-name", "--org", "personal", "--json", "--yes") in calls


@pytest.mark.asyncio
async def test_validate_explains_one_time_login(monkeypatch) -> None:
    backend = FlyBackend(FlyBackendConfig())
    monkeypatch.setattr(
        "kilntainers.backends.fly.ensure_flyctl", lambda command: "/tools/flyctl"
    )

    async def fake_run(*args, **kwargs):
        if args[:2] == ("auth", "whoami"):
            return 1, b"", b"not logged in"
        return 0, b"{}", b""

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    with pytest.raises(BackendError, match="/tools/flyctl auth login"):
        await backend.validate()


@pytest.mark.asyncio
async def test_create_builds_bundled_xfce_as_permanent_public_machine(
    monkeypatch,
) -> None:
    backend = FlyBackend(
        FlyBackendConfig(app="desktop-app", computer_id="agent-workstation")
    )
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    created = False

    async def fake_run(*args, **kwargs):
        nonlocal created
        calls.append((args, kwargs))
        if args[:2] == ("machine", "run"):
            created = True
        return 0, b"", b""

    async def fake_rows():
        return [machine_row()] if created else []

    async def no_op(*args, **kwargs):
        return None

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    monkeypatch.setattr(backend, "_list_machine_rows", fake_rows)
    monkeypatch.setattr(backend, "_ensure_public_ips", no_op)
    monkeypatch.setattr(backend, "_read_runtime_modes", no_op)
    monkeypatch.setattr(FlySandbox, "_verify_readiness", no_op)
    monkeypatch.setattr(FlySandbox, "_verify_desktop_transport", no_op)

    sandbox = await backend._create_sandbox(
        computer_id="agent-workstation", temporary=False
    )

    run_args, run_kwargs = next(item for item in calls if item[0][:2] == ("machine", "run"))
    assert run_args[run_args.index("--rootfs-persist") + 1] == "always"
    assert run_args[run_args.index("--restart") + 1] == "always"
    assert "--rm" not in run_args
    assert "80:6080/tcp:http" in run_args
    assert "443:6080/tcp:http:tls" in run_args
    assert any(str(value).startswith("VNC_PATH_TOKEN=") for value in run_args)
    assert run_args[-1] == "."
    assert Path(str(run_kwargs["cwd"]), "Dockerfile").is_file()
    assert sandbox.temporary is False
    assert sandbox.desktop_url == "wss://desktop-app.fly.dev/private-vnc-token/websockify"


@pytest.mark.asyncio
async def test_public_ip_setup_allocates_only_missing_versions(monkeypatch) -> None:
    backend = FlyBackend(FlyBackendConfig(app="desktop-app"))
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("ips", "list"):
            return 0, b'[{"Version":4,"Type":"shared"}]', b""
        return 0, b"", b""

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    await backend._ensure_public_ips()

    assert not any(call[:2] == ("ips", "allocate-v4") for call in calls)
    assert any(call[:2] == ("ips", "allocate-v6") for call in calls)


@pytest.mark.asyncio
async def test_stopping_handle_leaves_permanent_machine_running(monkeypatch) -> None:
    backend = FlyBackend(FlyBackendConfig(app="desktop-app"))
    sandbox = backend._row_to_sandbox(machine_row())
    calls: list[tuple[str, ...]] = []

    async def fake_run(*args, **kwargs):
        calls.append(args)
        return 0, b"", b""

    monkeypatch.setattr(backend, "_run_fly", fake_run)
    await sandbox.stop()

    assert calls == []


def test_remote_command_wraps_workdir_args_and_stdin_in_shell() -> None:
    backend = FlyBackend(FlyBackendConfig(app="desktop-app"))
    sandbox = backend._row_to_sandbox(machine_row())
    command = sandbox._remote_command(
        ExecRequest(
            args=["cat", "file with spaces"],
            stdin="hello",
            working_directory="/workspace dir",
            timeout=30,
            output_limit=4096,
        )
    )

    argv = shlex.split(command)
    assert argv[:2] == ["/bin/bash", "-lc"]
    assert argv[2].startswith("cd -- '/workspace dir' && printf %s ")
    assert "base64 -d" in argv[2]
    assert "cat 'file with spaces'" in argv[2]


def test_wsproxy_requires_fly_path_token(monkeypatch) -> None:
    monkeypatch.setenv("VNC_PATH_TOKEN", "secret-path")
    module = runpy.run_path(
        str(Path(__file__).parents[1] / "desktop_image" / "wsproxy.py")
    )
    authorize = module["authorized_channel"]

    assert authorize("/secret-path/websockify") == "websockify"
    assert authorize("/secret-path/audio") == "audio"
    assert authorize("/websockify") is None
    assert authorize("/wrong/websockify") is None

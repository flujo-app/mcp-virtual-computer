"""Docker-only virtual computer configuration and desktop integration tests."""

import os
from importlib.resources import files
from typing import cast

import pytest

from kilntainers.backends.base import ExecRequest
from kilntainers.backends.docker import (
    DEFAULT_DESKTOP_IMAGE,
    DESKTOP_IMAGE_VERSION,
    DESKTOP_IMAGE_VERSION_LABEL,
    DESKTOP_LABEL,
    IMAGE_LABEL,
    WORKSPACE_LABEL,
    DockerBackend,
    DockerBackendConfig,
)
from kilntainers.desktop import animate_file_operation
from kilntainers.desktop_control import (
    accessibility_snapshot,
    capture_screen,
    desktop_action,
    visible_terminal_execute,
)
from kilntainers.errors import BackendError
from kilntainers.file_tools import list_directory, read_text_file, write_text_file


def test_desktop_image_owns_complete_user_local_prefix() -> None:
    """User-scoped installers must be able to create commands below ~/.local."""
    image_files = files("kilntainers").joinpath("desktop_image")
    dockerfile = image_files.joinpath("Dockerfile").read_text(encoding="utf-8")
    entrypoint = image_files.joinpath("start-desktop.sh").read_text(encoding="utf-8")

    assert "computer -g computer /home/computer/.local \\" in dockerfile
    assert "computer -g computer /home/computer/.local/bin \\" in dockerfile
    assert "/home/computer/.local/bin \\" in entrypoint
    assert (
        f'LABEL {DESKTOP_IMAGE_VERSION_LABEL}="{DESKTOP_IMAGE_VERSION}"' in dockerfile
    )


def test_headless_computer_is_persistent_and_uses_workspace() -> None:
    backend = DockerBackend(DockerBackendConfig(network_enabled=True))
    command = backend._build_run_command(computer_id="desk", temporary=False)

    assert "--rm" not in command
    assert command[-4:] == ["debian:bookworm-slim", "tail", "-f", "/dev/null"]
    assert command[command.index("--workdir") + 1] == "/workspace"
    assert "127.0.0.1::6080" not in command


def test_desktop_capable_computer_can_start_with_xfce_disabled() -> None:
    backend = DockerBackend(
        DockerBackendConfig(
            image=DEFAULT_DESKTOP_IMAGE,
            desktop_environment=False,
            network_enabled=True,
        )
    )
    command = backend._build_run_command(computer_id="desk", temporary=False)

    assert command[-1] == DEFAULT_DESKTOP_IMAGE
    assert "tail" not in command
    assert "--init" in command
    assert command[command.index("--publish") + 1] == "127.0.0.1::6080"
    assert "DESKTOP_ENVIRONMENT=false" in command
    assert f"{DESKTOP_LABEL}=false" in command


def test_desktop_computer_publishes_only_loopback_websocket() -> None:
    backend = DockerBackend(
        DockerBackendConfig(
            image=DEFAULT_DESKTOP_IMAGE,
            desktop_environment=True,
            network_enabled=True,
        )
    )
    command = backend._build_run_command(computer_id="desk", temporary=False)

    assert command[-1] == DEFAULT_DESKTOP_IMAGE
    assert "tail" not in command
    assert command[command.index("--publish") + 1] == "127.0.0.1::6080"
    assert command[command.index("--shm-size") + 1] == "256m"
    assert "DESKTOP_ENVIRONMENT=true" in command
    assert f"{DESKTOP_LABEL}=true" in command


async def test_desktop_no_network_keeps_local_screen_and_requests_firewall() -> None:
    backend = DockerBackend(
        DockerBackendConfig(
            image=DEFAULT_DESKTOP_IMAGE,
            desktop_environment=True,
            network_enabled=False,
        )
    )

    backend._validated = True
    command = backend._build_run_command(computer_id="desk", temporary=False)

    assert "--network" not in command
    assert command[command.index("--cap-add") + 1] == "NET_ADMIN"
    assert "NETWORK_ACCESS=false" in command


def test_xfce_disabled_no_network_keeps_local_transport_for_runtime_switch() -> None:
    backend = DockerBackend(
        DockerBackendConfig(
            image=DEFAULT_DESKTOP_IMAGE,
            desktop_environment=False,
            network_enabled=False,
        )
    )
    command = backend._build_run_command(computer_id="desk", temporary=False)

    assert "--network" not in command
    assert "127.0.0.1::6080" in command
    assert "NETWORK_ACCESS=false" in command
    assert "DESKTOP_ENVIRONMENT=false" in command


def test_attached_desktop_exposes_browser_websocket_url() -> None:
    backend = DockerBackend(DockerBackendConfig(desktop_environment=True))
    sandbox = backend._sandbox_from_inspect(
        {
            "Id": "a" * 64,
            "Config": {
                "Labels": {
                    "kilntainers.computer-id": "desk",
                    "kilntainers.temporary": "false",
                    IMAGE_LABEL: DEFAULT_DESKTOP_IMAGE,
                    DESKTOP_LABEL: "true",
                    WORKSPACE_LABEL: "/workspace",
                }
            },
            "NetworkSettings": {
                "Ports": {"6080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "49153"}]}
            },
        }
    )

    assert sandbox.computer_id == "desk"
    assert sandbox.temporary is False
    assert sandbox.desktop_url == "ws://127.0.0.1:49153/websockify"


async def test_legacy_headless_computer_is_not_replaced_to_enable_xfce() -> None:
    backend = DockerBackend(DockerBackendConfig())
    sandbox = backend._sandbox_from_inspect(
        {
            "Id": "b" * 64,
            "Config": {
                "Labels": {
                    "kilntainers.computer-id": "legacy",
                    "kilntainers.temporary": "false",
                    IMAGE_LABEL: "debian:bookworm-slim",
                    DESKTOP_LABEL: "false",
                    WORKSPACE_LABEL: "/workspace",
                }
            },
            "NetworkSettings": {"Ports": {}, "Networks": {"bridge": {}}},
        }
    )

    with pytest.raises(BackendError, match="data was left untouched"):
        await backend._set_desktop_mode(sandbox, True)


@pytest.mark.integration
async def test_real_xfce_desktop_replays_and_preserves_file_write() -> None:
    """Build/run the live desktop and verify real UI replay plus reattachment."""
    computer_id = f"virtual-computer-e2e-{os.getpid()}"
    backend = DockerBackend(
        DockerBackendConfig(
            image=DEFAULT_DESKTOP_IMAGE,
            desktop_environment=True,
            network_enabled=True,
        )
    )
    sandbox = await backend.create_sandbox(
        computer_id=computer_id,
        temporary=False,
    )
    try:
        assert sandbox.desktop_url is not None
        screen = await capture_screen(sandbox)
        assert screen.startswith(b"\x89PNG\r\n\x1a\n")
        snapshot = await accessibility_snapshot(sandbox)
        assert snapshot["format"] == "at-spi-snapshot-v1"
        assert snapshot["elements"]
        terminal = await visible_terminal_execute(
            sandbox,
            command="printf 'visible terminal e2e\\n'",
            args=None,
            stdin=None,
            working_directory="/workspace",
            timeout=30,
            output_limit=131_072,
        )
        assert terminal.stdout == "visible terminal e2e\n"
        assert terminal.exit_code == 0
        windows = await desktop_action(sandbox, "list_windows")
        assert any("MCP Terminal" in window["title"] for window in windows["windows"])
        saved = await write_text_file(
            sandbox,
            "desktop-e2e.txt",
            "written through the real desktop\n",
            workspace_directory="/workspace",
            timeout=30,
            output_limit=131_072,
            text_limit=131_072,
        )
        warning = await animate_file_operation(
            sandbox,
            operation="write_file",
            path=saved.path,
            content=saved.content,
            original_content=None,
            old_text=None,
            new_text=None,
            replace_all=False,
            terminal_was_last=False,
            workspace_directory="/workspace",
        )
        assert warning is None
        actual = await read_text_file(
            sandbox,
            saved.path,
            workspace_directory="/workspace",
            timeout=30,
            output_limit=131_072,
            text_limit=131_072,
        )
        assert actual.content == saved.content
        listing = await list_directory(
            sandbox,
            "/workspace",
            workspace_directory="/workspace",
            timeout=30,
            output_limit=131_072,
        )
        assert any(
            entry.name == "desktop-e2e.txt" and entry.kind == "file"
            for entry in listing.entries
        )

        attached = await backend.attach_sandbox(computer_id)
        assert attached is not None
        assert attached.computer_id == computer_id
        assert attached.desktop_url == sandbox.desktop_url
    finally:
        await backend.delete_computer(computer_id)


@pytest.mark.integration
async def test_runtime_switches_preserve_the_full_container() -> None:
    """Exercise dashboard toggles without replacing the Docker computer."""
    computer_id = f"virtual-computer-toggle-e2e-{os.getpid()}"
    backend = DockerBackend(
        DockerBackendConfig(
            image=DEFAULT_DESKTOP_IMAGE,
            desktop_environment=False,
            network_enabled=True,
        )
    )
    sandbox = await backend.create_sandbox(computer_id=computer_id, temporary=False)
    try:
        original_container_id = sandbox.sandbox_id
        assert sandbox.desktop_environment is False
        assert sandbox.desktop_url is None
        await write_text_file(
            sandbox,
            "toggle-preserved.txt",
            "workspace survives both mug faces\n",
            workspace_directory="/workspace",
            timeout=30,
            output_limit=131_072,
            text_limit=131_072,
        )
        outside_workspace = await sandbox.exec(
            ExecRequest(
                command="mkdir -p /opt/mcp-test && printf persistent > /opt/mcp-test/mode",
                timeout=10,
                output_limit=131_072,
            )
        )
        assert outside_workspace.exit_code == 0

        desktop = await backend.switch_desktop_environment(computer_id, True)
        assert desktop is not None
        assert desktop.sandbox_id == original_container_id
        assert desktop.desktop_environment is True
        assert desktop.desktop_url is not None
        assert (await capture_screen(desktop)).startswith(b"\x89PNG\r\n\x1a\n")

        isolated = await backend.set_network_access(computer_id, False)
        assert isolated is not None
        assert isolated.network_access is False
        assert isolated.desktop_url is not None
        blocked = await isolated.exec(
            ExecRequest(
                command=(
                    'python3 -c "import socket; '
                    "socket.getaddrinfo('deb.debian.org', 443); print('online')\""
                ),
                timeout=10,
                output_limit=131_072,
            )
        )
        assert blocked.exit_code != 0

        online = await backend.set_network_access(computer_id, True)
        assert online is not None
        reachable = await online.exec(
            ExecRequest(
                command=(
                    'python3 -c "import socket; '
                    "socket.getaddrinfo('deb.debian.org', 443); print('online')\""
                ),
                timeout=10,
                output_limit=131_072,
            )
        )
        assert reachable.exit_code == 0
        assert reachable.stdout == "online\n"

        headless = await backend.switch_desktop_environment(computer_id, False)
        assert headless is not None
        assert headless.sandbox_id == original_container_id
        assert headless.desktop_environment is False
        assert headless.desktop_url is None
        preserved = await read_text_file(
            headless,
            "toggle-preserved.txt",
            workspace_directory="/workspace",
            timeout=30,
            output_limit=131_072,
            text_limit=131_072,
        )
        assert preserved.content == "workspace survives both mug faces\n"
        persistent = await headless.exec(
            ExecRequest(
                command="cat /opt/mcp-test/mode",
                timeout=10,
                output_limit=131_072,
            )
        )
        assert persistent.exit_code == 0
        assert persistent.stdout == "persistent"

        disconnected = await backend.set_network_access(computer_id, False)
        assert disconnected is not None
        assert disconnected.network_access is False
        inspect = await backend._inspect_computer(computer_id)
        assert inspect is not None
        network_settings = inspect["NetworkSettings"]
        assert isinstance(network_settings, dict)
        network_settings = cast("dict[str, object]", network_settings)
        networks = network_settings.get("Networks")
        assert isinstance(networks, dict)
        assert "none" not in networks

        restored = await backend.switch_desktop_environment(computer_id, True)
        assert restored is not None
        assert restored.sandbox_id == original_container_id
        assert restored.desktop_url is not None
        healed_vnc = await restored.exec(
            ExecRequest(
                command=(
                    "old=$(pgrep -o x11vnc) || exit 1; kill \"$old\"; i=0; "
                    "while [ $i -lt 50 ]; do i=$((i + 1)); "
                    "new=$(pgrep -o x11vnc 2>/dev/null || true); "
                    "if [ -n \"$new\" ] && [ \"$new\" != \"$old\" ]; then "
                    "printf '%s->%s\\n' \"$old\" \"$new\"; exit 0; fi; "
                    "sleep 0.2; done; exit 1"
                ),
                timeout=15,
                output_limit=131_072,
            )
        )
        assert healed_vnc.exit_code == 0
        assert "->" in healed_vnc.stdout
        assert (await capture_screen(restored)).startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        await backend.delete_computer(computer_id)

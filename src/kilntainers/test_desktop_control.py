"""Tests for live Xfce desktop helper requests."""

import base64
import json

import pytest

from kilntainers.backends.base import ExecResult
from kilntainers.backends.test_utils import MockSandbox
from kilntainers.desktop_control import (
    DesktopControlError,
    capture_screen,
    desktop_action,
    visible_terminal_execute,
)


class DesktopSandbox(MockSandbox):
    """Mock sandbox that advertises a live desktop endpoint."""

    @property
    def desktop_url(self) -> str:
        return "ws://127.0.0.1:49153/websockify"


async def test_desktop_action_serializes_payload() -> None:
    sandbox = DesktopSandbox(
        exec_results=[
            ExecResult(
                stdout=json.dumps({"ok": True, "x": 12, "y": 34}),
                stderr="",
                exit_code=0,
                exec_duration_ms=1,
            )
        ]
    )

    response = await desktop_action(sandbox, "click", {"x": 12, "y": 34})

    assert response["ok"] is True
    request = sandbox.exec_calls[0]
    assert request.args == ["python3", "/usr/local/bin/desktop-control"]
    assert json.loads(request.stdin or "{}") == {
        "action": "click",
        "x": 12,
        "y": 34,
    }


async def test_capture_screen_decodes_png() -> None:
    image = b"\x89PNG\r\n\x1a\nmock"
    sandbox = DesktopSandbox(
        exec_results=[
            ExecResult(
                stdout=json.dumps(
                    {
                        "mime_type": "image/png",
                        "image_base64": base64.b64encode(image).decode("ascii"),
                    }
                ),
                stderr="",
                exit_code=0,
                exec_duration_ms=1,
            )
        ]
    )

    assert await capture_screen(sandbox) == image


async def test_visible_terminal_maps_captured_result() -> None:
    sandbox = DesktopSandbox(
        exec_results=[
            ExecResult(
                stdout=json.dumps(
                    {
                        "stdout": "visible\n",
                        "stderr": "",
                        "exit_code": 0,
                        "exec_duration_ms": 22,
                    }
                ),
                stderr="",
                exit_code=0,
                exec_duration_ms=23,
            )
        ]
    )

    result = await visible_terminal_execute(
        sandbox,
        command="echo visible",
        args=None,
        stdin=None,
        working_directory="/workspace",
        timeout=30,
        output_limit=131_072,
    )

    assert result.stdout == "visible\n"
    assert result.exit_code == 0
    request = json.loads(sandbox.exec_calls[0].stdin or "{}")
    assert request["action"] == "terminal_execute"
    assert request["command"] == "echo visible"


async def test_desktop_action_rejects_headless_sandbox() -> None:
    with pytest.raises(DesktopControlError, match="DESKTOP_ENVIRONMENT=true"):
        await desktop_action(MockSandbox(), "snapshot")

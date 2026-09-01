"""Live Xfce framebuffer, accessibility, input, and terminal helpers."""

import base64
import json
from typing import Any

from kilntainers.backends.base import ExecRequest, ExecResult, Sandbox

SCREEN_IMAGE_URI = "computer://screen/current.png"
SCREEN_ACCESSIBILITY_URI = "computer://screen/accessibility.json"


class DesktopControlError(Exception):
    """Raised when the live desktop cannot complete an interaction."""


async def desktop_action(
    sandbox: Sandbox,
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    working_directory: str = "/workspace",
    timeout: int = 30,
    output_limit: int = 2 * 1024 * 1024,
) -> dict[str, Any]:
    """Run one bundled desktop action and decode its JSON response."""
    if sandbox.desktop_url is None:
        raise DesktopControlError("This tool requires DESKTOP_ENVIRONMENT=true.")
    request_payload = {"action": action, **(payload or {})}
    result = await sandbox.exec(
        ExecRequest(
            args=["python3", "/usr/local/bin/desktop-control"],
            stdin=json.dumps(request_payload),
            working_directory=working_directory,
            timeout=timeout,
            output_limit=output_limit,
        )
    )
    if result.exit_code != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise DesktopControlError(message or f"Desktop action '{action}' failed.")
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DesktopControlError(
            f"Desktop action '{action}' returned invalid JSON."
        ) from error
    if not isinstance(response, dict):
        raise DesktopControlError(
            f"Desktop action '{action}' returned an invalid response."
        )
    if response.get("error"):
        raise DesktopControlError(str(response["error"]))
    return response


async def capture_screen(sandbox: Sandbox) -> bytes:
    """Capture the current X11 framebuffer as PNG bytes."""
    response = await desktop_action(sandbox, "screenshot", timeout=15)
    encoded = response.get("image_base64")
    if not isinstance(encoded, str):
        raise DesktopControlError("Desktop screenshot returned no image.")
    try:
        return base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise DesktopControlError("Desktop screenshot returned invalid image data.") from error


async def accessibility_snapshot(sandbox: Sandbox) -> dict[str, Any]:
    """Return a model-friendly AT-SPI snapshot of the visible desktop."""
    return await desktop_action(sandbox, "snapshot", timeout=20)


async def visible_terminal_execute(
    sandbox: Sandbox,
    *,
    command: str | None,
    args: list[str] | None,
    stdin: str | None,
    working_directory: str,
    timeout: int,
    output_limit: int,
) -> ExecResult:
    """Execute exactly once in a visible Xfce terminal and capture the result."""
    response = await desktop_action(
        sandbox,
        "terminal_execute",
        {
            "command": command,
            "args": args,
            "stdin": stdin,
            "working_directory": working_directory,
            "timeout": timeout,
            "output_limit": output_limit,
        },
        working_directory=working_directory,
        timeout=timeout + 15,
        output_limit=max(262_144, output_limit * 3),
    )
    return ExecResult(
        stdout=str(response.get("stdout", "")),
        stderr=str(response.get("stderr", "")),
        exit_code=int(response.get("exit_code", 1)),
        exec_duration_ms=int(response.get("exec_duration_ms", 0)),
    )

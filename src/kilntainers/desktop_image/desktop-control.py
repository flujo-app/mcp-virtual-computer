#!/usr/bin/env python3
"""Control and inspect the real Xfce desktop through X11 and AT-SPI."""

from __future__ import annotations

import base64
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("NO_AT_BRIDGE", "0")
os.environ.setdefault("GTK_MODULES", "atk-bridge")


def run(
    *args: str,
    timeout: float = 10,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=os.environ,
    )


def key(*keys: str) -> None:
    run("xdotool", "key", "--clearmodifiers", *keys)


def type_text(value: str, *, delay: int = 2) -> None:
    lines = value.split("\n")
    for index, line in enumerate(lines):
        if line:
            run(
                "xdotool",
                "type",
                "--clearmodifiers",
                "--delay",
                str(delay),
                "--",
                line,
                timeout=max(10, len(line) * max(delay, 1) / 500 + 5),
            )
        if index < len(lines) - 1:
            key("Return")


def window_id_text(value: int) -> str:
    return f"0x{value:08x}"


def list_windows() -> list[dict[str, Any]]:
    active_output = run(
        "xprop", "-root", "_NET_ACTIVE_WINDOW", check=False
    ).stdout
    active_match = re.search(r"0x[0-9a-fA-F]+", active_output)
    active_id = int(active_match.group(0), 16) if active_match else 0
    output = run("wmctrl", "-lGx", check=False).stdout
    windows: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split(None, 7)
        if len(parts) < 8:
            continue
        raw_id, desktop, x, y, width, height, wm_class, title = parts
        numeric_id = int(raw_id, 16)
        state_output = run(
            "xprop", "-id", raw_id, "_NET_WM_STATE", check=False
        ).stdout
        states = [
            state.removeprefix("_NET_WM_STATE_").lower()
            for state in re.findall(r"_NET_WM_STATE_[A-Z_]+", state_output)
        ]
        windows.append(
            {
                "window_id": window_id_text(numeric_id),
                "desktop": int(desktop),
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
                "wm_class": wm_class,
                "title": title,
                "active": numeric_id == active_id,
                "states": states,
            }
        )
    return windows


def resolve_window(selector: str) -> dict[str, Any]:
    windows = list_windows()
    value = selector.strip()
    try:
        numeric_id = int(value, 0)
    except ValueError:
        numeric_id = -1
    if numeric_id >= 0:
        for window in windows:
            if int(str(window["window_id"]), 16) == numeric_id:
                return window
        raise ValueError(f"Window '{selector}' was not found.")

    folded = value.casefold()
    exact = [
        window
        for window in windows
        if str(window["title"]).casefold() == folded
        or str(window["wm_class"]).casefold() == folded
    ]
    matches = exact or [
        window
        for window in windows
        if folded in str(window["title"]).casefold()
        or folded in str(window["wm_class"]).casefold()
    ]
    if not matches:
        raise ValueError(f"Window '{selector}' was not found.")
    if len(matches) > 1:
        choices = ", ".join(str(window["window_id"]) for window in matches[:8])
        raise ValueError(f"Window selector is ambiguous; use one of: {choices}.")
    return matches[0]


def atspi_desktop() -> Any:
    import pyatspi  # ty: ignore[unresolved-import]

    return pyatspi.Registry.getDesktop(0)


def node_at_ref(ref: str) -> Any:
    if not ref.startswith("atspi:"):
        raise ValueError("Element references must start with 'atspi:'.")
    raw_path = ref.removeprefix("atspi:")
    indices = [int(value) for value in raw_path.split("/") if value != ""]
    node = atspi_desktop()
    for index in indices:
        if index < 0 or index >= node.childCount:
            raise ValueError(f"Accessibility element '{ref}' is no longer available.")
        node = node.getChildAtIndex(index)
    return node


def node_bounds(node: Any) -> dict[str, int] | None:
    try:
        import pyatspi  # ty: ignore[unresolved-import]

        extents = node.queryComponent().getExtents(pyatspi.DESKTOP_COORDS)
        if (
            extents.width <= 0
            or extents.height <= 0
            or extents.x + extents.width <= 0
            or extents.y + extents.height <= 0
            or extents.x >= 1280
            or extents.y >= 800
        ):
            return None
        return {
            "x": int(extents.x),
            "y": int(extents.y),
            "width": int(extents.width),
            "height": int(extents.height),
        }
    except Exception:
        return None


def node_actions(node: Any) -> list[str]:
    try:
        actions = node.queryAction()
        return [actions.getName(index) for index in range(actions.nActions)]
    except Exception:
        return []


def accessibility_snapshot(max_nodes: int = 600) -> dict[str, Any]:
    import pyatspi  # ty: ignore[unresolved-import]

    desktop = atspi_desktop()
    flat: list[dict[str, Any]] = []
    lines: list[str] = []
    truncated = False

    meaningful_roles = {
        "application",
        "frame",
        "dialog",
        "alert",
        "terminal",
        "push button",
        "toggle button",
        "check box",
        "radio button",
        "combo box",
        "entry",
        "text",
        "password text",
        "menu",
        "menu item",
        "page tab",
        "list",
        "list item",
        "table",
        "table cell",
        "tree",
        "tree item",
        "link",
        "icon",
        "label",
    }

    def walk(node: Any, path: list[int], depth: int, parent_ref: str | None) -> None:
        nonlocal truncated
        if len(flat) >= max_nodes:
            truncated = True
            return
        try:
            role = node.getRoleName() or "unknown"
            name = (node.name or "").strip()
            states = node.getState()
            showing = states.contains(pyatspi.STATE_SHOWING)
            focused = states.contains(pyatspi.STATE_FOCUSED)
            focusable = states.contains(pyatspi.STATE_FOCUSABLE)
            enabled = states.contains(pyatspi.STATE_ENABLED)
            bounds = node_bounds(node)
            actions = node_actions(node)
            include = (
                depth <= 1
                or role in meaningful_roles
                or bool(name)
                or bool(actions)
                or focused
            ) and (depth <= 1 or showing or focused)
            current_parent = parent_ref
            if include:
                ref = "atspi:" + "/".join(str(index) for index in path)
                item: dict[str, Any] = {
                    "ref": ref,
                    "parent_ref": parent_ref,
                    "role": role,
                    "name": name,
                    "focused": focused,
                    "focusable": focusable,
                    "enabled": enabled,
                }
                if bounds:
                    item["bounds"] = bounds
                if actions:
                    item["actions"] = actions
                flat.append(item)
                current_parent = ref
                attributes = [f"ref={ref}"]
                if bounds:
                    attributes.append(
                        "bounds="
                        f"{bounds['x']},{bounds['y']},{bounds['width']},{bounds['height']}"
                    )
                if focused:
                    attributes.append("focused")
                if actions:
                    attributes.append("actions=" + ",".join(actions))
                quoted_name = f' "{name}"' if name else ""
                lines.append(
                    f"{'  ' * min(depth, 12)}- {role}{quoted_name} "
                    f"[{' '.join(attributes)}]"
                )
            if depth >= 12:
                return
            child_count = min(int(node.childCount), 200)
            for index in range(child_count):
                if len(flat) >= max_nodes:
                    truncated = True
                    return
                try:
                    child = node.getChildAtIndex(index)
                    walk(child, [*path, index], depth + 1, current_parent)
                except Exception:
                    continue
        except Exception:
            return

    for app_index in range(min(int(desktop.childCount), 80)):
        try:
            walk(desktop.getChildAtIndex(app_index), [app_index], 0, None)
        except Exception:
            continue
    return {
        "format": "at-spi-snapshot-v1",
        "generated_at": time.time(),
        "width": 1280,
        "height": 800,
        "truncated": truncated,
        "snapshot": "\n".join(lines),
        "elements": flat,
    }


def point_from_payload(payload: dict[str, Any]) -> tuple[int, int]:
    element = payload.get("element")
    if element:
        node = node_at_ref(str(element))
        bounds = node_bounds(node)
        if bounds is None:
            raise ValueError(f"Element '{element}' has no clickable screen bounds.")
        return (
            bounds["x"] + bounds["width"] // 2,
            bounds["y"] + bounds["height"] // 2,
        )
    if payload.get("x") is None or payload.get("y") is None:
        raise ValueError("Provide either element or both x and y coordinates.")
    return int(payload["x"]), int(payload["y"])


def screenshot() -> dict[str, Any]:
    import gi  # ty: ignore[unresolved-import]

    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk  # ty: ignore[unresolved-import]

    window = Gdk.get_default_root_window()
    if window is None:
        raise RuntimeError("The X11 root window is unavailable.")
    pixbuf = Gdk.pixbuf_get_from_window(
        window, 0, 0, window.get_width(), window.get_height()
    )
    if pixbuf is None:
        raise RuntimeError("The X11 framebuffer could not be captured.")
    success, data = pixbuf.save_to_bufferv("png", [], [])
    if not success:
        raise RuntimeError("The X11 framebuffer could not be encoded.")
    return {
        "mime_type": "image/png",
        "width": pixbuf.get_width(),
        "height": pixbuf.get_height(),
        "image_base64": base64.b64encode(bytes(data)).decode("ascii"),
    }


def click(payload: dict[str, Any]) -> dict[str, Any]:
    x, y = point_from_payload(payload)
    button_names = {"left": "1", "middle": "2", "right": "3"}
    button = button_names.get(str(payload.get("button", "left")))
    if button is None:
        raise ValueError("button must be left, middle, or right.")
    clicks = max(1, min(3, int(payload.get("clicks", 1))))
    run("xdotool", "mousemove", str(x), str(y))
    run("xdotool", "click", "--repeat", str(clicks), "--delay", "120", button)
    return {"ok": True, "x": x, "y": y, "button": payload.get("button", "left"), "clicks": clicks}


def type_action(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("element"):
        click({"element": payload["element"], "button": "left", "clicks": 1})
    if payload.get("clear"):
        key("ctrl+a")
    value = str(payload.get("text", ""))
    type_text(value, delay=max(0, min(100, int(payload.get("delay_ms", 2)))))
    if payload.get("press_enter"):
        key("Return")
    return {"ok": True, "characters": len(value), "pressed_enter": bool(payload.get("press_enter"))}


def scroll(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("element") or payload.get("x") is not None:
        x, y = point_from_payload(payload)
        run("xdotool", "mousemove", str(x), str(y))
    direction = str(payload.get("direction", "down"))
    buttons = {"up": "4", "down": "5", "left": "6", "right": "7"}
    if direction not in buttons:
        raise ValueError("direction must be up, down, left, or right.")
    amount = max(1, min(50, int(payload.get("amount", 3))))
    run("xdotool", "click", "--repeat", str(amount), "--delay", "45", buttons[direction])
    return {"ok": True, "direction": direction, "amount": amount}


def window_action(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload["action"])
    window = resolve_window(str(payload["window"]))
    window_id = str(window["window_id"])
    if action == "switch_window":
        run("wmctrl", "-ia", window_id)
    elif action == "move_window":
        width = int(payload.get("width") or window["width"])
        height = int(payload.get("height") or window["height"])
        geometry = f"0,{int(payload['x'])},{int(payload['y'])},{width},{height}"
        run("wmctrl", "-ir", window_id, "-e", geometry)
    elif action == "maximize_window":
        run("wmctrl", "-ir", window_id, "-b", "add,maximized_vert,maximized_horz")
    elif action == "restore_window":
        run("wmctrl", "-ir", window_id, "-b", "remove,maximized_vert,maximized_horz")
        run("wmctrl", "-ia", window_id)
    elif action == "minimize_window":
        run("xdotool", "windowminimize", window_id)
    elif action == "close_window":
        run("wmctrl", "-ic", window_id)
    else:
        raise ValueError(f"Unknown window action '{action}'.")
    time.sleep(0.2)
    return {"ok": True, "action": action, "window": window}


def terminal_runner(payload_path: Path) -> int:
    """Run one process inside the visible terminal and atomically record its result."""
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    command = payload.get("command")
    args = payload.get("args")
    if command is not None:
        command_line = str(command)
        process_args: str | list[str] = command_line
        use_shell = True
    elif isinstance(args, list) and args:
        process_args = [str(value) for value in args]
        command_line = shlex.join(process_args)
        use_shell = False
    else:
        raise ValueError("terminal_execute requires command or args.")

    working_directory = str(payload.get("working_directory") or "/workspace")
    if not Path(working_directory).is_dir():
        raise ValueError(f"Working directory '{working_directory}' does not exist.")
    timeout = max(1, int(payload.get("timeout") or 120))
    output_limit = max(1, int(payload.get("output_limit") or 2 * 1024 * 1024))
    result_path = Path(str(payload["result_path"]))
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()

    get_euid = getattr(os, "geteuid", None)
    is_root = get_euid is not None and get_euid() == 0
    username = "root" if is_root else os.environ.get("USER", "computer")
    prompt_marker = "#" if is_root else "$"
    prompt = f"{username}@{socket.gethostname()}:{working_directory}{prompt_marker} "
    sys.stdout.write("\033[1;32m" + prompt + "\033[0m")
    sys.stdout.flush()
    for character in command_line:
        sys.stdout.write(character)
        sys.stdout.flush()
        time.sleep(0.002)
    sys.stdout.write("\n")
    sys.stdout.flush()

    started = time.monotonic()
    process = subprocess.Popen(
        process_args,
        shell=use_shell,
        executable="/bin/bash" if use_shell else None,
        cwd=working_directory,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def pump(source: Any, destination: Any, capture: bytearray) -> None:
        while True:
            chunk = source.read(4096)
            if not chunk:
                return
            destination.write(chunk)
            destination.flush()
            if len(capture) < output_limit:
                capture.extend(chunk[: output_limit - len(capture)])

    stdout_thread = threading.Thread(
        target=pump,
        args=(process.stdout, sys.stdout.buffer, stdout_buffer),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=pump,
        args=(process.stderr, sys.stderr.buffer, stderr_buffer),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        stdin_value = payload.get("stdin")
        if process.stdin is not None:
            if stdin_value is not None:
                process.stdin.write(str(stdin_value).encode("utf-8"))
            process.stdin.close()
        try:
            exit_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            getattr(os, "killpg")(process.pid, 15)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                getattr(os, "killpg")(process.pid, 9)
                process.wait(timeout=3)
            exit_code = 124
        stdout_thread.join(timeout=3)
        stderr_thread.join(timeout=3)
        duration_ms = int((time.monotonic() - started) * 1000)
        stdout = bytes(stdout_buffer).decode("utf-8", errors="replace")
        remaining = max(0, output_limit - len(stdout.encode("utf-8")))
        stderr = bytes(stderr_buffer[:remaining]).decode("utf-8", errors="replace")
        truncated = len(stdout_buffer) >= output_limit or len(stderr_buffer) > remaining
        if truncated:
            stderr += "\n[output truncated by MCP output limit]\n"
        if exit_code == 124:
            stderr += f"\nCommand timed out after {timeout} seconds.\n"
        result_path.write_text(
            json.dumps(
                {
                    "stdout": stdout,
                    "stderr": stderr,
                    "exit_code": exit_code,
                    "exec_duration_ms": duration_ms,
                }
            ),
            encoding="utf-8",
        )
        sys.stdout.write(f"\n[exit {exit_code}]\n")
        sys.stdout.flush()
        os.execv("/bin/bash", ["/bin/bash", "-i"])
        return exit_code  # pragma: no cover
    except BaseException as error:
        result_path.write_text(
            json.dumps(
                {
                    "stdout": "",
                    "stderr": str(error),
                    "exit_code": 1,
                    "exec_duration_ms": int((time.monotonic() - started) * 1000),
                }
            ),
            encoding="utf-8",
        )
        return 1


def terminal_execute(payload: dict[str, Any]) -> dict[str, Any]:
    command = payload.get("command")
    args = payload.get("args")
    if command is None and not (isinstance(args, list) and args):
        raise ValueError("terminal_execute requires command or args.")
    working_directory = str(payload.get("working_directory") or "/workspace")
    if not Path(working_directory).is_dir():
        raise ValueError(f"Working directory '{working_directory}' does not exist.")
    timeout = max(1, int(payload.get("timeout") or 120))

    token = uuid.uuid4().hex
    run_dir = Path(tempfile.mkdtemp(prefix=f"mcp-visible-terminal-{token}-"))
    payload_path = run_dir / "payload.json"
    result_path = run_dir / "result.json"
    payload_path.write_text(
        json.dumps({**payload, "result_path": str(result_path)}),
        encoding="utf-8",
    )
    title = f"MCP Terminal {token[:8]}"
    process = subprocess.Popen(
        [
            "xfce4-terminal",
            "--disable-server",
            f"--title={title}",
            f"--working-directory={working_directory}",
            "--execute",
            "python3",
            str(Path(__file__).resolve()),
            "--terminal-runner",
            str(payload_path),
        ],
        env=os.environ,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        window_id: str | None = None
        window_deadline = time.monotonic() + 12
        while time.monotonic() < window_deadline and window_id is None:
            matches = run(
                "xdotool", "search", "--name", title, check=False
            ).stdout.splitlines()
            if matches:
                window_id = matches[-1]
                break
            if process.poll() is not None and not result_path.exists():
                raise RuntimeError("The visible terminal exited before it was ready.")
            time.sleep(0.05)
        if window_id is None:
            raise RuntimeError("The visible terminal window was not found.")
        run("xdotool", "windowactivate", "--sync", window_id)

        deadline = time.monotonic() + timeout + 5
        while time.monotonic() < deadline and not result_path.exists():
            time.sleep(0.05)
        if not result_path.exists():
            raise RuntimeError("The visible terminal did not return a result.")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["window_id"] = window_id_text(int(window_id))
        return result
    finally:
        for path in (payload_path, result_path):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        try:
            run_dir.rmdir()
        except OSError:
            pass


def dispatch(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action", ""))
    if action == "screenshot":
        return screenshot()
    if action == "snapshot":
        return accessibility_snapshot()
    if action == "click":
        return click(payload)
    if action == "type":
        return type_action(payload)
    if action == "scroll":
        return scroll(payload)
    if action == "list_windows":
        return {"windows": list_windows()}
    if action in {
        "switch_window",
        "move_window",
        "maximize_window",
        "restore_window",
        "minimize_window",
        "close_window",
    }:
        return window_action(payload)
    if action == "terminal_execute":
        return terminal_execute(payload)
    raise ValueError(f"Unknown desktop action '{action}'.")


def main() -> int:
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--terminal-runner":
            return terminal_runner(Path(sys.argv[2]))
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("Desktop action payload must be an object.")
        print(json.dumps(dispatch(payload), ensure_ascii=False))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

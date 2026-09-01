#!/usr/bin/env python3
"""Best-effort Xfce/AT-SPI choreography for a completed file operation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("DISPLAY", ":99")
os.environ.setdefault("NO_AT_BRIDGE", "0")
os.environ.setdefault("GTK_MODULES", "atk-bridge")


def run(*args: str, timeout: float = 8, check: bool = True) -> subprocess.CompletedProcess[str]:
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
    run("xdotool", "type", "--clearmodifiers", "--delay", str(delay), "--", value, timeout=35)


def activate(window_class: str) -> str:
    result = run(
        "xdotool",
        "search",
        "--sync",
        "--onlyvisible",
        "--class",
        window_class,
        timeout=12,
    )
    window_id = result.stdout.splitlines()[-1]
    run("xdotool", "windowactivate", "--sync", window_id)
    return window_id


def accessible_double_click(filename: str) -> bool:
    """Use AT-SPI geometry so the real pointer visibly clicks the file row."""
    try:
        # Thunar's icon view does not expose file names through AT-SPI. Its
        # details view does, so switch views before resolving the target row.
        key("ctrl+2")
        time.sleep(0.35)
        from dogtail import tree  # ty: ignore[unresolved-import]

        app = tree.root.application("thunar")
        node = app.child(name=filename, recursive=True)
        x, y = node.position
        width, height = node.size
        run("xdotool", "mousemove", "--sync", str(x + width // 2), str(y + height // 2))
        run("xdotool", "click", "--repeat", "2", "--delay", "130", "1")
        return True
    except BaseException:
        return False


def restore_if_needed(path: Path, content: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except (OSError, UnicodeError):
        pass
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    fd, temp_name = tempfile.mkstemp(prefix=".visual-save-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def main() -> int:
    payload = json.load(sys.stdin)
    operation = str(payload["operation"])
    path = Path(str(payload["path"]))
    content = payload.get("content")
    original_content = payload.get("original_content")
    old_text = str(payload.get("old_text") or "")
    new_text = str(payload.get("new_text") or "")
    replace_all = bool(payload.get("replace_all"))

    can_replay = isinstance(content, str) and len(content) <= 8000
    try:
        if operation in {"write_file", "edit_file"} and can_replay:
            # Rewind under the sandbox exec lock, then let the visible editor
            # replay the genuine requested change.
            restore_if_needed(
                path,
                original_content if isinstance(original_content, str) else "",
            )

        if payload.get("terminal_was_last"):
            key("alt+Tab")
            time.sleep(0.55)

        subprocess.Popen(
            ["thunar", str(path.parent)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ,
        )
        activate("thunar")
        time.sleep(0.45)
        key("ctrl+l")
        type_text(str(path.parent), delay=5)
        key("Return")
        time.sleep(0.75)

        if not accessible_double_click(path.name):
            key("ctrl+l")
            type_text(str(path), delay=4)
            key("Return")

        activate("mousepad")
        time.sleep(0.45)

        if operation == "read_file":
            for _ in range(4):
                key("Page_Down")
                time.sleep(0.28)
            key("ctrl+Home")
        elif operation == "write_file" and can_replay:
            key("ctrl+a")
            type_text(content, delay=1)
            key("ctrl+s")
            time.sleep(0.45)
        elif operation == "edit_file" and can_replay:
            if (
                old_text
                and "\n" not in old_text
                and len(old_text) <= 500
                and not replace_all
            ):
                key("ctrl+f")
                type_text(old_text, delay=3)
                key("Return")
                key("Escape")
                time.sleep(0.5)
                type_text(new_text, delay=2)
                key("ctrl+s")
            else:
                key("ctrl+a")
                type_text(content, delay=1)
                key("ctrl+s")
            time.sleep(0.45)

        key("ctrl+w")
        time.sleep(0.35)
        print(json.dumps({"ok": True, "operation": operation, "path": str(path)}))
        return 0
    finally:
        # Deterministic file state always wins, including SystemExit raised by
        # third-party accessibility code or an interrupted window sequence.
        if isinstance(content, str):
            restore_if_needed(path, content)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"visual automation failed: {error}", file=sys.stderr)
        raise SystemExit(1)

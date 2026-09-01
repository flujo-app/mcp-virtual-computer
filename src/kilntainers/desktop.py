"""Optional live-desktop choreography invoked after a real file operation."""

import json

from kilntainers.backends.base import ExecRequest, Sandbox


async def animate_file_operation(
    sandbox: Sandbox,
    *,
    operation: str,
    path: str,
    content: str | None,
    original_content: str | None,
    old_text: str | None,
    new_text: str | None,
    replace_all: bool,
    terminal_was_last: bool,
    workspace_directory: str,
) -> str | None:
    """Run bundled Xfce automation and return a non-fatal warning on failure."""
    if sandbox.desktop_url is None:
        return None
    payload = json.dumps(
        {
            "operation": operation,
            "path": path,
            "content": content,
            "original_content": original_content,
            "old_text": old_text,
            "new_text": new_text,
            "replace_all": replace_all,
            "terminal_was_last": terminal_was_last,
        }
    )
    result = await sandbox.exec(
        ExecRequest(
            args=["python3", "/usr/local/bin/visual-action"],
            stdin=payload,
            working_directory=workspace_directory,
            timeout=60,
            output_limit=131_072,
        )
    )
    if result.exit_code == 0:
        return None
    return result.stderr.strip() or "Desktop automation did not complete."

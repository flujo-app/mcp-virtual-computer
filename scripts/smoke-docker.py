"""Smoke-test the installed wheel against one disposable headless Docker computer.

CI runs this after building the wheel with:
uv run --no-project --with dist/<wheel>.whl python scripts/smoke-docker.py

Every MCP connection is a fresh real CLI subprocess. Cleanup can remove only the
unique fixture container whose name, immutable ID, and ownership labels match.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from mcp.client import Client
from mcp.client.stdio import StdioServerParameters

import kilntainers

IMAGE = "debian:bookworm-slim"
SMOKE_LABEL = "kilntainers.smoke-run"
MODERN_PROTOCOL = "2026-07-28"
LEGACY_PROTOCOL = "2025-11-25"


def docker(*arguments: str, timeout: int = 30) -> str:
    """Run a bounded Docker command without a shell."""
    result = subprocess.run(
        ["docker", *arguments],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def inspect_fixture(computer_id: str, run_id: str) -> dict[str, Any] | None:
    """Find only our exact random name and verify ownership before using its ID."""
    if computer_id != f"mcp-smoke-{run_id}" or not re.fullmatch(
        r"[0-9a-f]{32}", run_id
    ):
        raise RuntimeError("Refusing a non-smoke computer identity")
    name = f"kilntainer-{computer_id}"
    ids = docker(
        "container",
        "ls",
        "--all",
        "--quiet",
        "--no-trunc",
        "--filter",
        f"name=^/{name}$",
    ).split()
    if not ids:
        return None
    if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", ids[0]):
        raise RuntimeError("Unexpected fixture container identity")
    records = json.loads(docker("container", "inspect", ids[0]))
    if not isinstance(records, list) or len(records) != 1:
        raise RuntimeError("Unexpected fixture inspection response")
    record = records[0]
    labels = record.get("Config", {}).get("Labels") or {}
    expected_labels = {
        "kilntainers": "true",
        "kilntainers.computer-id": computer_id,
        "kilntainers.temporary": "false",
        SMOKE_LABEL: run_id,
    }
    if (
        record.get("Id") != ids[0]
        or record.get("Name") != f"/{name}"
        or not record["Name"].startswith("/kilntainer-mcp-smoke-")
        or any(labels.get(key) != value for key, value in expected_labels.items())
        or record.get("Config", {}).get("Image") != IMAGE
    ):
        raise RuntimeError("Fixture ownership mismatch; refusing container cleanup")
    return record


def cleanup(computer_id: str, run_id: str) -> None:
    """Remove only the verified immutable ID; never prune or delete by a prefix."""
    record = inspect_fixture(computer_id, run_id)
    if record is None:
        return
    container_id = record["Id"]
    docker("container", "rm", "--force", container_id)
    if inspect_fixture(computer_id, run_id) is not None:
        raise RuntimeError("Fixture remained after cleanup")
    print(json.dumps({"cleanup": "removed", "container_id": container_id}))


async def call(client: Client, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(name, arguments, read_timeout_seconds=90)
    if result.is_error or not isinstance(result.structured_content, dict):
        raise RuntimeError(f"{name} failed: {result.content!r}")
    return result.structured_content


async def exercise(computer_id: str, run_id: str, directory: str) -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "BACKEND": "docker",
            "COMPUTER_ID": computer_id,
            "DESKTOP_ENVIRONMENT": "false",
            "NETWORK_ACCESS": "false",
            "EXPOSE_LIFECYCLE_TOOLS": "false",
        }
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "kilntainers",
            "--backend",
            "docker",
            "--image",
            IMAGE,
            "--timeout",
            "30",
            f"--docker-run-flag=--label={SMOKE_LABEL}={run_id}",
        ],
        env=environment,
        cwd=directory,
    )
    first_container_id: str | None = None
    for mode in ("auto", "legacy"):
        path = f"/workspace/{mode}-persistence.txt"
        content = f"persistent {mode} MCP smoke {run_id}\nUTF-8: café\n"
        expected_protocol = MODERN_PROTOCOL if mode == "auto" else LEGACY_PROTOCOL
        for reconnect in (False, True):
            async with Client(parameters, mode=mode, read_timeout_seconds=90) as client:
                if client.protocol_version != expected_protocol:
                    raise RuntimeError(
                        f"{mode} negotiated unexpected protocol {client.protocol_version}"
                    )
                catalog = await client.list_tools()
                names = {tool.name for tool in catalog.tools}
                if not {"terminal_execute", "write_file", "read_file"} <= names:
                    raise RuntimeError("Installed artifact is missing core tools")
                terminal = await call(
                    client,
                    "terminal_execute",
                    {"command": "printf 'docker-smoke-ok\\n'; pwd", "timeout": 30},
                )
                if terminal.get("exit_code") != 0 or terminal.get("stdout") != (
                    "docker-smoke-ok\n/workspace\n"
                ):
                    raise RuntimeError(f"Unexpected terminal result: {terminal!r}")
                if not reconnect:
                    written = await call(
                        client, "write_file", {"path": path, "content": content}
                    )
                    if (
                        written.get("sha256")
                        != hashlib.sha256(content.encode()).hexdigest()
                    ):
                        raise RuntimeError("write_file did not save the expected bytes")
                read = await call(client, "read_file", {"path": path})
                if read.get("content") != content:
                    raise RuntimeError("File contents did not persist across processes")
            record = inspect_fixture(computer_id, run_id)
            if record is None or not record.get("State", {}).get("Running"):
                raise RuntimeError("Persistent computer did not survive MCP disconnect")
            if first_container_id is None:
                first_container_id = record["Id"]
            elif record["Id"] != first_container_id:
                raise RuntimeError("MCP reconnect replaced the persistent container")
            print(
                json.dumps(
                    {
                        "protocol": expected_protocol,
                        "reconnect": reconnect,
                        "computer_id": computer_id,
                        "container_id": first_container_id,
                        "terminal_and_files": "passed",
                    }
                ),
                flush=True,
            )


async def main() -> None:
    """Exercise only a generated fixture, with cleanup even after failures."""
    repository = Path(__file__).resolve().parents[1]
    module_path = Path(kilntainers.__file__).resolve()
    if module_path.is_relative_to(repository / "src"):
        raise RuntimeError("Run with the built wheel, not the editable source tree")
    run_id = uuid.uuid4().hex
    computer_id = f"mcp-smoke-{run_id}"
    # Preflight is read-only and precedes the cleanup scope. An existing name is
    # never adopted, even in the fantastically unlikely event of a UUID collision.
    docker("info")
    existing = docker(
        "container",
        "ls",
        "--all",
        "--quiet",
        "--filter",
        f"name=^/kilntainer-{computer_id}$",
    ).strip()
    if existing:
        raise RuntimeError(
            "Generated fixture name already exists; refusing to reuse it"
        )
    docker("pull", IMAGE, timeout=180)
    print(
        json.dumps({"installed_module": str(module_path), "computer_id": computer_id})
    )
    try:
        with tempfile.TemporaryDirectory(prefix="mcp-wheel-smoke-") as directory:
            async with asyncio.timeout(240):
                await exercise(computer_id, run_id, directory)
    finally:
        cleanup(computer_id, run_id)


if __name__ == "__main__":
    asyncio.run(main())

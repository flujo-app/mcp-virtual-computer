"""Real CLI signal and cancellation cleanup regressions, without Docker access."""

import json
import os
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
from unittest.mock import AsyncMock

import anyio
import pytest

from kilntainers.backends.test_utils import MockBackend
from kilntainers.config import BackendConfig
from kilntainers.errors import BackendError
from kilntainers.server import SessionContext, VirtualComputerServer, create_lifespan


@pytest.mark.parametrize(
    "transport,fail,interrupt",
    (
        [("stdio", False, signal.CTRL_BREAK_EVENT)]
        if os.name == "nt"
        else [
            ("stdio", False, signal.SIGTERM),
            ("stdio", False, signal.SIGINT),
            ("http", True, signal.SIGTERM),
        ]
    ),
)
def test_real_cli_sigterm_awaits_cleanup(tmp_path, transport, fail, interrupt):
    """A slow cleanup must outlive the former five-second successful-exit timer."""
    marker = tmp_path / "cleanup.jsonl"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    source = r"""
import asyncio, json, sys
from pathlib import Path
from kilntainers import cli
from kilntainers.backends.test_utils import MockBackend
from kilntainers.errors import BackendError
from kilntainers.server import SessionContext
marker = Path(sys.argv.pop(1))
fail = sys.argv.pop(1) == "true"
original = SessionContext.cleanup
async def cleanup(self):
    with marker.open("a") as f:
        f.write(json.dumps({"event": "begin"}) + "\n")
    if fail:
        raise BackendError("Simulated cleanup failure")
    await asyncio.sleep(6.25)
    await original(self)
    with marker.open("a") as f:
        f.write(json.dumps({"event": "complete"}) + "\n")
SessionContext.cleanup = cleanup
cli.get_backend_class = lambda name: MockBackend
cli.main()
"""
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            source,
            str(marker),
            str(fail).lower(),
            "--transport",
            transport,
            "--port",
            str(port),
            "--auth-token",
            "shutdown-test-owner-token-32-characters",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        env={
            **os.environ,
            "COMPUTER_ID": "shutdown-fixture",
            "DESKTOP_ENVIRONMENT": "false",
        },
    )
    assert process.stdin is not None and process.stdout is not None
    output = queue.Queue()

    def read_stdout():
        for line in process.stdout:
            output.put(line)

    reader = threading.Thread(target=read_stdout, daemon=True)
    reader.start()
    try:
        if transport == "stdio":
            process.stdin.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "server/discover",
                        "params": {
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                "io.modelcontextprotocol/clientCapabilities": {},
                            }
                        },
                    }
                )
                + "\n"
            )
            process.stdin.flush()
            assert "result" in json.loads(output.get(timeout=15))
        else:
            deadline = time.monotonic() + 15
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    assert process.poll() is None, "HTTP server exited before readiness"
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
        started = time.monotonic()
        process.send_signal(interrupt)
        process.wait(timeout=35)
        events = [json.loads(line)["event"] for line in marker.read_text().splitlines()]
        if fail:
            assert process.returncode != 0, "Failed cleanup must not report success"
        else:
            assert process.returncode == 0, process.stderr.read()
            assert time.monotonic() - started >= 6.25
            assert events.count("begin") >= 1
            assert events.count("complete") == events.count("begin"), events
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()
        assert process.stderr is not None
        diagnostics = process.stderr.read()
        process.stderr.close()
        assert "ResourceWarning" not in diagnostics


async def test_lifespan_cleanup_survives_cancelled_anyio_scope(monkeypatch):
    completed = []

    async def cleanup(self):
        await anyio.sleep(0.01)
        completed.append(True)

    monkeypatch.setattr(SessionContext, "cleanup", cleanup)
    backend = MockBackend(BackendConfig())
    server = VirtualComputerServer("cleanup-test")
    with anyio.CancelScope() as scope:
        async with create_lifespan(backend, "http")(server):
            scope.cancel()
    assert completed == [True]


async def test_lifespan_failure_is_observable_after_http_server_returns(monkeypatch):
    monkeypatch.setattr(
        SessionContext, "cleanup", AsyncMock(side_effect=BackendError("failed"))
    )
    server = VirtualComputerServer("cleanup-test")
    with pytest.raises(BackendError):
        async with create_lifespan(MockBackend(BackendConfig()), "http")(server):
            pass
    assert server.cleanup_failed is True


def test_stdio_input_preserves_utf8_and_diverts_child_stdin():
    """Real pipes retain split UTF-8 input while children receive EOF."""
    source = r"""
import json, subprocess, sys
from kilntainers.stdio_input import interruptible_stdin
with interruptible_stdin(True):
    child = subprocess.check_output([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"])
    data = sys.stdin.read()
sys.stdout.write(json.dumps({"data": data, "child": child.decode()}))
"""
    payload = "split UTF-8: café 漢字\n" * 8000
    result = subprocess.run(
        [sys.executable, "-c", source],
        input=payload,
        text=True,
        capture_output=True,
        check=True,
        timeout=15,
        encoding="utf-8",
    )
    assert json.loads(result.stdout) == {"data": payload, "child": ""}

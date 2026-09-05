"""Tests for the MCP server implementation."""

import asyncio
import json
import os
import queue
import signal
import subprocess
import sys
import threading
from typing import cast
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import certifi
import pytest
from mcp import Client
from starlette.testclient import TestClient
from websockets.typing import Subprotocol

from kilntainers.backends.base import ExecResult
from kilntainers.backends.test_utils import MockBackend, MockSandbox
from kilntainers.config import BackendConfig, ServerConfig
from kilntainers.errors import BackendError
from kilntainers.server import (
    SessionContext,
    _activity_snapshot,
    _computer_desktop_proxy_url,
    _computer_ui_url,
    _connect_desktop_websocket,
    _create_handler,
    assemble_tool_description,
    create_http_app,
    create_lifespan,
    create_server,
)

# --- Test Configuration ---


def test_computer_ui_url_uses_standalone_dashboard_for_stdio() -> None:
    config = ServerConfig()
    url = _computer_ui_url(config)
    assert (
        urlsplit(url)._replace(query="").geturl()
        == "http://127.0.0.1:8435/dashboard.html"
    )
    assert parse_qs(urlsplit(url).query)["computer_access"] == [
        config.companion_access.token
    ]


def test_computer_ui_url_uses_reachable_http_dashboard() -> None:
    config = ServerConfig(
        transport="http",
        host="0.0.0.0",
        port=4173,
    )

    assert (
        urlsplit(_computer_ui_url(config))._replace(query="").geturl()
        == "http://127.0.0.1:4173/dashboard.html"
    )


def test_computer_ui_url_brackets_ipv6_hosts() -> None:
    config = ServerConfig(transport="http", host="::1", port=4173)

    assert (
        urlsplit(_computer_ui_url(config))._replace(query="").geturl()
        == "http://[::1]:4173/dashboard.html"
    )


def test_computer_desktop_proxy_url_uses_exact_server_port() -> None:
    config = ServerConfig(transport="stdio", port=43123)

    assert (
        urlsplit(_computer_desktop_proxy_url(config))._replace(query="").geturl()
        == "ws://127.0.0.1:43123/desktop/websockify"
    )


def test_desktop_websocket_uses_certifi_for_remote_tls(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected_connection = object()
    expected_context = object()

    def fake_create_default_context(*, cafile: str) -> object:
        captured["cafile"] = cafile
        return expected_context

    def fake_connect(target: str, **options: object) -> object:
        captured["target"] = target
        captured["options"] = options
        return expected_connection

    monkeypatch.setattr(
        "kilntainers.server.ssl.create_default_context",
        fake_create_default_context,
    )
    monkeypatch.setattr("kilntainers.server.connect_websocket", fake_connect)

    connection = _connect_desktop_websocket(
        "wss://desktop.fly.dev/private/websockify",
        [cast(Subprotocol, "binary")],
    )

    assert connection is expected_connection
    assert captured["cafile"] == certifi.where()
    assert captured["target"] == "wss://desktop.fly.dev/private/websockify"
    assert captured["options"] == {
        "ssl": expected_context,
        "subprotocols": ["binary"],
        "max_size": None,
    }


def test_desktop_websocket_keeps_local_connection_without_tls(monkeypatch) -> None:
    captured: dict[str, object] = {}
    expected_connection = object()

    def fake_connect(target: str, **options: object) -> object:
        captured["target"] = target
        captured["options"] = options
        return expected_connection

    monkeypatch.setattr("kilntainers.server.connect_websocket", fake_connect)

    connection = _connect_desktop_websocket(
        "ws://127.0.0.1:49152/websockify",
        [],
    )

    assert connection is expected_connection
    assert captured["target"] == "ws://127.0.0.1:49152/websockify"
    assert captured["options"] == {
        "ssl": None,
        "subprotocols": None,
        "max_size": None,
    }


@pytest.fixture
def server_config() -> ServerConfig:
    """Return a default server config for testing."""
    return ServerConfig()


@pytest.fixture
def mock_backend() -> MockBackend:
    """Return a mock backend for testing."""
    return MockBackend(BackendConfig(), tool_instructions="A Debian Linux bash shell")


@pytest.fixture
async def mock_context(mock_backend: MockBackend) -> MagicMock:
    """Return a mock MCPServer Context for testing.

    Pre-creates the sandbox so handler tests can configure exec results
    before calling the handler.
    """
    ctx = MagicMock()
    # Use a no-op death callback to prevent SIGTERM during tests
    session_ctx = SessionContext(
        backend=mock_backend,
        transport="stdio",
        death_callback=lambda: None,
    )
    # Pre-create sandbox so handler tests can configure exec results
    await session_ctx.get_or_create_sandbox()
    ctx.request_context.lifespan_context = session_ctx
    return ctx


# --- Tool Description Assembly Tests ---


def test_assemble_tool_description_override(mock_backend: MockBackend) -> None:
    """Override provided returns override, ignores backend."""
    result = assemble_tool_description(
        mock_backend,
        override="Custom description",
        extended=None,
    )
    assert result == "Custom description"


def test_assemble_tool_description_backend_only(mock_backend: MockBackend) -> None:
    """Backend provides instructions returns backend text."""
    result = assemble_tool_description(mock_backend, override=None, extended=None)
    assert result == "A Debian Linux bash shell"


def test_assemble_tool_description_backend_with_extended(
    mock_backend: MockBackend,
) -> None:
    """Backend provides instructions + extended concatenated with \\n\\n."""
    result = assemble_tool_description(
        mock_backend,
        override=None,
        extended="With additional info.",
    )
    assert result == "A Debian Linux bash shell\n\nWith additional info."


def test_assemble_tool_description_no_backend_no_override() -> None:
    """No backend instructions and no override raises BackendError."""
    backend = MockBackend(BackendConfig(), tool_instructions=None)
    with pytest.raises(BackendError) as exc_info:
        assemble_tool_description(backend, override=None, extended=None)
    assert "does not provide tool instructions" in str(exc_info.value)
    assert "--tool-instruction-override" in str(exc_info.value)


def test_assemble_tool_description_empty_backend_no_override() -> None:
    """Backend returns empty string, no override raises BackendError."""
    backend = MockBackend(BackendConfig(), tool_instructions="")
    with pytest.raises(BackendError) as exc_info:
        assemble_tool_description(backend, override=None, extended=None)
    assert "does not provide tool instructions" in str(exc_info.value)


def test_assemble_tool_description_both_override_and_extended() -> None:
    """Both override and extended raises BackendError."""
    backend = MockBackend(BackendConfig(), tool_instructions="test")
    with pytest.raises(BackendError) as exc_info:
        assemble_tool_description(
            backend,
            override="Override",
            extended="Extended",
        )
    assert "Cannot use both" in str(exc_info.value)


# --- Input Validation Tests ---


@pytest.mark.parametrize(
    ("command", "args", "stdin", "working_dir", "timeout", "expected_error"),
    [
        # Both command and args
        ("ls", ["/bin/ls"], None, None, None, "Cannot provide both"),
        # Neither command nor args
        (None, None, None, None, None, "Must provide either"),
        # Relative working_directory
        ("ls", None, None, "relative/path", None, "absolute path"),
        # timeout < 1
        ("ls", None, None, None, 0, "at least 1 second"),
        ("ls", None, None, None, -5, "at least 1 second"),
        # stdin exceeds 2 MiB
        (
            "ls",
            None,
            "x" * (2 * 1024 * 1024 + 1),
            None,
            None,
            "exceeds the 2 MiB limit",
        ),
    ],
    ids=[
        "both-command-and-args",
        "missing-command-and-args",
        "relative-working-directory",
        "timeout-zero",
        "timeout-negative",
        "stdin-over-2mib",
    ],
)
def test_validate_inputs_invalid(
    command: str | None,
    args: list[str] | None,
    stdin: str | None,
    working_dir: str | None,
    timeout: int | None,
    expected_error: str,
) -> None:
    """Various invalid inputs return error messages."""
    from kilntainers.server import _validate_inputs

    error = _validate_inputs(command, args, stdin, working_dir, timeout)
    assert error is not None
    assert expected_error in error


def test_validate_inputs_valid_command_only() -> None:
    """Valid command only passes."""
    from kilntainers.server import _validate_inputs

    error = _validate_inputs("ls -la", None, None, None, None)
    assert error is None


def test_validate_inputs_valid_args_only() -> None:
    """Valid args only passes."""
    from kilntainers.server import _validate_inputs

    error = _validate_inputs(None, ["/bin/ls", "-la"], None, None, None)
    assert error is None


def test_validate_inputs_all_optional_params() -> None:
    """All optional params populated passes."""
    from kilntainers.server import _validate_inputs

    error = _validate_inputs(
        command="ls",
        args=None,
        stdin="input",
        working_directory="/tmp",
        timeout=30,
    )
    assert error is None


def test_validate_inputs_stdin_at_exactly_2mib() -> None:
    """stdin at exactly 2 MiB passes."""
    from kilntainers.server import _validate_inputs

    stdin_content = "x" * (2 * 1024 * 1024)
    error = _validate_inputs("cat", None, stdin_content, None, None)
    assert error is None


# --- Handler Normal Response Tests ---


async def test_handler_success_command(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """Successful command returns isError=False, exit_code 0."""
    handler = _create_handler(server_config)  # Get handler from factory

    # Configure mock to return success
    mock_context.request_context.lifespan_context.sandbox.exec_results.append(
        ExecResult(stdout="hello\n", stderr="", exit_code=0, exec_duration_ms=10)
    )

    result = await handler(command="echo hello", ctx=mock_context)

    assert result.is_error is False
    content = result.content[0]
    assert content.type == "text"

    response_json = json.loads(content.text)
    assert response_json["stdout"] == "hello\n"
    assert response_json["stderr"] == ""
    assert response_json["exit_code"] == 0
    assert response_json["exec_duration_ms"] == 10


async def test_handler_failed_command(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """Failed command returns isError=False, non-zero exit_code."""
    handler = _create_handler(server_config)

    mock_context.request_context.lifespan_context.sandbox.exec_results.append(
        ExecResult(
            stdout="",
            stderr="command not found\n",
            exit_code=127,
            exec_duration_ms=5,
        )
    )

    result = await handler(command="nonexistent", ctx=mock_context)

    assert result.is_error is False
    response_json = json.loads(result.content[0].text)
    assert response_json["exit_code"] == 127
    assert response_json["stderr"] == "command not found\n"


async def test_handler_timeout_result(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """Timeout result returns isError=False, exit_code 124."""
    handler = _create_handler(server_config)

    mock_context.request_context.lifespan_context.sandbox.exec_results.append(
        ExecResult(stdout="", stderr="", exit_code=124, exec_duration_ms=120000)
    )

    result = await handler(command="sleep 300", ctx=mock_context)

    assert result.is_error is False
    response_json = json.loads(result.content[0].text)
    assert response_json["exit_code"] == 124


async def test_handler_output_limit_result(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """Output limit result returns isError=False, exit_code 1."""
    handler = _create_handler(server_config)

    mock_context.request_context.lifespan_context.sandbox.exec_results.append(
        ExecResult(stdout="truncated...", stderr="", exit_code=1, exec_duration_ms=50)
    )

    result = await handler(command="yes", ctx=mock_context)

    assert result.is_error is False
    response_json = json.loads(result.content[0].text)
    assert response_json["exit_code"] == 1


async def test_response_json_contains_all_fields(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """Response JSON contains execution and computer identity fields."""
    handler = _create_handler(server_config)

    mock_context.request_context.lifespan_context.sandbox.exec_results.append(
        ExecResult(
            stdout="out",
            stderr="err",
            exit_code=0,
            exec_duration_ms=42,
        )
    )

    result = await handler(command="test", ctx=mock_context)
    response_json = json.loads(result.content[0].text)

    assert set(response_json.keys()) == {
        "computer_id",
        "operation",
        "desktop_environment",
        "desktop_url",
        "stdout",
        "stderr",
        "exit_code",
        "exec_duration_ms",
        "network_access",
    }
    assert response_json["stdout"] == "out"
    assert response_json["stderr"] == "err"
    assert response_json["exit_code"] == 0
    assert response_json["exec_duration_ms"] == 42
    assert isinstance(response_json["computer_id"], str)
    assert response_json["operation"] == "terminal_execute"
    assert response_json["desktop_environment"] is False


# --- Handler Error Response Tests ---


async def test_handler_invalid_inputs(server_config: ServerConfig) -> None:
    """Invalid inputs returns CallToolResult with isError=True."""
    handler = _create_handler(server_config)

    result = await handler(command="ls", args=["/bin/ls"], ctx=None)

    assert result.is_error is True
    assert "Cannot provide both" in result.content[0].text


async def test_handler_sandbox_died_error(
    mock_context: MagicMock, server_config: ServerConfig
) -> None:
    """SandboxDiedError returns isError=True with descriptive message."""
    handler = _create_handler(server_config)

    # Configure mock to raise SandboxDiedError
    mock_context.request_context.lifespan_context.sandbox.exec_results.append(
        ExecResult(stdout="", stderr="", exit_code=0, exec_duration_ms=1)
    )
    mock_context.request_context.lifespan_context.sandbox._death_event.set()

    result = await handler(command="test", ctx=mock_context)

    assert result.is_error is True
    assert "died" in result.content[0].text.lower()


async def test_handler_no_context_error(server_config: ServerConfig) -> None:
    """Handler with None context returns isError=True."""
    handler = _create_handler(server_config)

    result = await handler(command="ls", ctx=None)

    assert result.is_error is True
    assert "no context provided" in result.content[0].text


# --- ExecRequest Construction Tests ---


async def test_request_construction_command_mode(
    mock_context: MagicMock, server_config: ServerConfig
) -> None:
    """command mode creates ExecRequest with command, args is None."""
    handler = _create_handler(server_config)

    await handler(command="ls -la", ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.command == "ls -la"
    assert request.args is None


async def test_request_construction_args_mode(
    mock_context: MagicMock, server_config: ServerConfig
) -> None:
    """args mode creates ExecRequest with args, command is None."""
    handler = _create_handler(server_config)

    await handler(args=["/bin/ls", "-la"], ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.args == ["/bin/ls", "-la"]
    assert request.command is None


async def test_request_construction_timeout_provided(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """timeout provided uses provided value."""
    handler = _create_handler(server_config)

    await handler(command="test", timeout=60, ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.timeout == 60


async def test_request_construction_timeout_default(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """timeout not provided uses server default."""
    handler = _create_handler(server_config)

    await handler(command="test", ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.timeout == server_config.default_timeout


async def test_request_construction_output_limit_always_from_config(
    mock_context: MagicMock,
    server_config: ServerConfig,
) -> None:
    """output_limit always from server config."""
    handler = _create_handler(server_config)

    await handler(command="test", ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.output_limit == server_config.output_limit


async def test_request_construction_stdin_passed(
    mock_context: MagicMock, server_config: ServerConfig
) -> None:
    """stdin passed through."""
    handler = _create_handler(server_config)

    await handler(command="cat", stdin="input data", ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.stdin == "input data"


async def test_request_construction_working_directory_passed(
    mock_context: MagicMock, server_config: ServerConfig
) -> None:
    """working_directory passed through."""
    handler = _create_handler(server_config)

    await handler(command="pwd", working_directory="/tmp", ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.working_directory == "/tmp"


async def test_request_construction_working_directory_none_when_not_provided(
    mock_context: MagicMock, server_config: ServerConfig
) -> None:
    """working_directory is None when not provided."""
    handler = _create_handler(server_config)

    await handler(command="pwd", ctx=mock_context)

    request = mock_context.request_context.lifespan_context.sandbox.exec_calls[0]
    assert request.working_directory is None


# --- Lifespan Tests ---


async def test_lifespan_yields_session_context_with_no_sandbox(
    mock_backend: MockBackend,
) -> None:
    """Lifespan yields a SessionContext with sandbox=None initially (lazy creation)."""
    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        assert isinstance(ctx, SessionContext)
        assert ctx.sandbox is None
        assert ctx.death_task is None


async def test_lifespan_creates_sandbox_lazily(mock_backend: MockBackend) -> None:
    """Sandbox is created lazily via get_or_create_sandbox()."""
    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        # Initially no sandbox
        assert ctx.sandbox is None
        # Create sandbox lazily
        sandbox = await ctx.get_or_create_sandbox()
        assert sandbox is not None
        assert sandbox.sandbox_id == "mock-sandbox-001"
        # Now the property returns the sandbox
        assert ctx.sandbox is sandbox


async def test_lifespan_cancels_death_task_on_exit(mock_backend: MockBackend) -> None:
    """On exit, death_task is cancelled (after sandbox is created)."""
    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        # Create sandbox to start death task
        await ctx.get_or_create_sandbox()
        death_task = ctx.death_task
        assert death_task is not None
        assert not death_task.cancelled()

    # After exit, death task should be cancelled
    assert death_task.cancelled()


async def test_lifespan_keeps_persistent_sandbox_on_exit(
    mock_backend: MockBackend,
) -> None:
    """Session cleanup releases but does not stop the persistent computer."""
    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        # Create sandbox
        sandbox = cast(MockSandbox, await ctx.get_or_create_sandbox())
        assert not sandbox.is_stopped()

    assert not sandbox.is_stopped()


async def test_lifespan_keeps_persistent_sandbox_if_exception_raised(
    mock_backend: MockBackend,
) -> None:
    """An MCP session error does not delete the persistent computer."""
    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    sandbox = None

    with pytest.raises(ValueError):
        async with lifespan_fn(mock_server) as ctx:
            # Create sandbox first
            sandbox = cast(MockSandbox, await ctx.get_or_create_sandbox())
            raise ValueError("test error")

    assert sandbox is not None
    assert not sandbox.is_stopped()


async def test_death_triggers_sigterm_stdio(
    mock_backend: MockBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox death triggers SIGTERM in stdio mode."""
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        # Create sandbox first to start death monitoring
        sandbox = cast(MockSandbox, await ctx.get_or_create_sandbox())
        # Simulate sandbox death
        sandbox.simulate_death()
        # Give death task time to process
        await asyncio.sleep(0.1)

    assert len(kill_calls) == 1
    assert kill_calls[0][1] == signal.SIGTERM


async def test_death_does_not_trigger_sigterm_http(
    mock_backend: MockBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sandbox death does NOT trigger SIGTERM in HTTP mode."""
    kill_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

    lifespan_fn = create_lifespan(mock_backend, "http")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        # Create sandbox first to start death monitoring
        sandbox = cast(MockSandbox, await ctx.get_or_create_sandbox())
        # Simulate sandbox death
        sandbox.simulate_death()
        # Give death task time to process
        await asyncio.sleep(0.1)

    # No SIGTERM should be sent in HTTP mode
    assert len(kill_calls) == 0


async def test_lifespan_creates_sandbox_lazily_for_http(
    mock_backend: MockBackend,
) -> None:
    """Lifespan creates a sandbox lazily for HTTP transport as well."""
    lifespan_fn = create_lifespan(mock_backend, "http")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        assert ctx.sandbox is None
        sandbox = await ctx.get_or_create_sandbox()
        assert sandbox is not None
        assert sandbox.sandbox_id == "mock-sandbox-001"


# --- SessionContext Lazy Creation Tests ---


async def test_session_context_lazy_creation(mock_backend: MockBackend) -> None:
    """SessionContext starts with sandbox=None. After get_or_create_sandbox(), sandbox is not None."""
    ctx = SessionContext(backend=mock_backend, transport="stdio")

    assert ctx.sandbox is None
    assert ctx.death_task is None

    sandbox = await ctx.get_or_create_sandbox()
    assert sandbox is not None
    assert ctx.sandbox is sandbox


async def test_session_context_returns_same_sandbox(mock_backend: MockBackend) -> None:
    """Two calls to get_or_create_sandbox() return the same sandbox instance."""
    ctx = SessionContext(backend=mock_backend, transport="stdio")

    sandbox1 = await ctx.get_or_create_sandbox()
    sandbox2 = await ctx.get_or_create_sandbox()

    assert sandbox1 is sandbox2
    assert mock_backend.create_count == 1


async def test_session_context_concurrent_creation(mock_backend: MockBackend) -> None:
    """Concurrent get_or_create_sandbox() creates exactly one sandbox."""
    ctx = SessionContext(backend=mock_backend, transport="stdio")

    # Launch multiple concurrent calls
    results = await asyncio.gather(
        ctx.get_or_create_sandbox(),
        ctx.get_or_create_sandbox(),
        ctx.get_or_create_sandbox(),
    )

    # All should return the same sandbox
    assert results[0] is results[1] is results[2]
    # Only one sandbox should have been created
    assert mock_backend.create_count == 1


async def test_session_context_cleanup_without_sandbox(
    mock_backend: MockBackend,
) -> None:
    """Call cleanup() without ever creating a sandbox. Should be a no-op."""
    ctx = SessionContext(backend=mock_backend, transport="stdio")

    # No sandbox created
    assert ctx.sandbox is None

    # Cleanup should not raise
    await ctx.cleanup()


async def test_session_context_cleanup_with_sandbox(mock_backend: MockBackend) -> None:
    """Cleanup cancels monitoring but preserves the persistent sandbox."""
    ctx = SessionContext(backend=mock_backend, transport="stdio")

    sandbox = cast(MockSandbox, await ctx.get_or_create_sandbox())
    death_task = ctx.death_task

    assert death_task is not None
    assert not death_task.cancelled()
    assert not sandbox.is_stopped()

    await ctx.cleanup()

    assert death_task.cancelled()
    assert not sandbox.is_stopped()


async def test_session_context_retry_on_creation_failure(
    mock_backend: MockBackend,
) -> None:
    """First get_or_create_sandbox() fails, second call succeeds."""
    ctx = SessionContext(backend=mock_backend, transport="stdio")

    # Configure first call to fail
    mock_backend.fail_next_create = True

    with pytest.raises(BackendError) as exc_info:
        await ctx.get_or_create_sandbox()
    assert "mock creation failure" in str(exc_info.value)

    # Sandbox should still be None
    assert ctx.sandbox is None

    # Second call should succeed
    sandbox = await ctx.get_or_create_sandbox()
    assert sandbox is not None
    assert ctx.sandbox is sandbox


async def test_handler_backend_error_on_lazy_creation(
    mock_backend: MockBackend,
    server_config: ServerConfig,
) -> None:
    """Handler with a backend that fails to create sandbox returns isError=True."""
    handler = _create_handler(server_config)

    # Create SessionContext that will fail on first creation
    session_ctx = SessionContext(backend=mock_backend, transport="stdio")
    mock_backend.fail_next_create = True

    ctx = MagicMock()
    ctx.request_context.lifespan_context = session_ctx

    result = await handler(command="test", ctx=ctx)

    assert result.is_error is True
    assert "mock creation failure" in result.content[0].text


async def test_session_context_death_monitor_starts_after_creation(
    mock_backend: MockBackend,
) -> None:
    """death_task is None before get_or_create_sandbox(), not None after."""
    ctx = SessionContext(backend=mock_backend, transport="stdio")

    assert ctx.death_task is None

    await ctx.get_or_create_sandbox()

    assert ctx.death_task is not None
    assert isinstance(ctx.death_task, asyncio.Task)


# --- Server Factory Tests ---


def test_create_server_returns_mcpserver(mock_backend: MockBackend) -> None:
    """create_server() returns a MCPServer instance."""
    config = ServerConfig()
    server = create_server(mock_backend, config)

    # Just verify it's a MCPServer instance with expected attributes
    assert hasattr(server, "name")
    assert server.name == "MCP Virtual Computer"


def test_create_server_with_lifespan(mock_backend: MockBackend) -> None:
    """create_server() creates MCPServer instance with lifespan configured."""
    config = ServerConfig(transport="stdio")
    server = create_server(mock_backend, config)

    # Verify a MCPServer instance was created
    assert server.name == "MCP Virtual Computer"


async def test_create_server_with_override_description(
    mock_backend: MockBackend,
) -> None:
    """Tool description uses override when provided."""
    config = ServerConfig(tool_instruction_override="Custom override")
    server = create_server(mock_backend, config)

    tools = {tool.name: tool for tool in await server.list_tools()}
    assert tools["terminal_execute"].description == "Custom override"


def test_create_server_raises_on_empty_description() -> None:
    """create_server() raises BackendError if tool description assembly fails."""
    backend = MockBackend(BackendConfig(), tool_instructions=None)
    config = ServerConfig()

    with pytest.raises(BackendError) as exc_info:
        create_server(backend, config)
    assert "does not provide tool instructions" in str(exc_info.value)


def test_create_server_with_extended_description(mock_backend: MockBackend) -> None:
    """Tool description combines backend instructions with extended."""
    config = ServerConfig(extended_tool_instruction="With extra info")
    server = create_server(mock_backend, config)

    # Server should be created successfully
    assert server.name == "MCP Virtual Computer"


async def test_public_tool_schemas_have_no_lifecycle_selector(
    mock_backend: MockBackend,
) -> None:
    """Computer identity and persistence are startup configuration only."""
    server = create_server(
        mock_backend,
        ServerConfig(
            computer_id="fixed-computer",
            desktop_environment=False,
            expose_lifecycle_tools=True,
        ),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert set(tools) == {
        "terminal_execute",
        "computer_ui",
        "list_directory",
        "read_file",
        "write_file",
        "edit_file",
        "set_network_access",
        "set_desktop_environment",
        "runtime_status",
    }
    for tool in tools.values():
        properties = tool.input_schema.get("properties", {})
        assert "computer_id" not in properties
        assert "temporary" not in properties


async def test_desktop_mode_exposes_screen_and_interaction_surface(
    mock_backend: MockBackend,
) -> None:
    """Xfce-only inspection and interaction capabilities are conditionally public."""
    server = create_server(
        mock_backend,
        ServerConfig(computer_id="fixed-computer", desktop_environment=True),
    )
    tools = {tool.name for tool in await server.list_tools()}
    resources = {str(resource.uri) for resource in await server.list_resources()}

    assert {
        "look_at_screen",
        "click",
        "type",
        "scroll",
        "list_windows",
        "switch_window",
        "move_window",
        "maximize_window",
        "restore_window",
        "minimize_window",
        "close_window",
    } <= tools
    assert "computer://screen/current.png" in resources
    assert "computer://screen/accessibility.json" in resources


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_sdk_v2_serves_both_protocol_eras(mode: str) -> None:
    """The same public tools work after modern discovery or legacy initialize."""
    backend = MockBackend(BackendConfig())
    server = create_server(
        backend, ServerConfig(transport="http", desktop_environment=False)
    )
    async with Client(server, mode=mode) as client:
        expected = "2026-07-28" if mode == "auto" else "2025-11-25"
        assert client.protocol_version == expected
        catalog = await client.list_tools()
        assert {tool.name for tool in catalog.tools} >= {
            "terminal_execute",
            "read_file",
        }
        assert backend.create_count == 0
        invalid = await client.call_tool("terminal_execute", {})
        assert invalid.is_error
        success = await client.call_tool(
            "terminal_execute",
            {"command": "echo ready", "stdin": None, "working_directory": None},
        )
        assert not success.is_error
        assert success.structured_content["exit_code"] == 0
        assert backend.create_count == 1


def test_activity_snapshot_does_not_retain_secrets() -> None:
    secret = "not-for-dashboard"
    event = _activity_snapshot(
        "result",
        "write_file",
        {
            "path": "/workspace/note.txt",
            "command": secret,
            "args": [secret],
            "stdin": secret,
            "content": secret,
            "old_text": secret,
            "new_text": secret,
        },
        {
            "path": "/workspace/note.txt",
            "content": secret,
            "stdout": secret,
            "stderr": secret,
            "desktop_url": "ws://localhost/?computer_access=" + secret,
            "dashboard_url": "http://localhost/?computer_access=" + secret,
            "error": secret,
            "exit_code": 1,
        },
    )
    assert secret not in json.dumps(event)
    assert event["arguments"] == {"path": "/workspace/note.txt"}
    assert event["payload"]["stdout_bytes"] == len(secret)
    assert event["payload"]["status"] == "error"


def _rpc_response(response):
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("application/json"):
        return response.json()
    return [
        json.loads(line[5:].strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ][-1]


def test_http_modern_discovery_security_and_activity() -> None:
    """Exercise actual ASGI wire fields, security rejection and activity retention."""
    backend = MockBackend(BackendConfig())
    config = ServerConfig(transport="http", desktop_environment=False)
    app = create_http_app(create_server(backend, config), config)
    meta = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
    }

    def rpc(client, method, params=None, extra_headers=None):
        params = dict(params or {})
        params["_meta"] = meta
        headers = {
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": method,
            "Accept": "application/json, text/event-stream",
        }
        if "name" in params:
            headers["Mcp-Name"] = params["name"]
        headers.update(extra_headers or {})
        return client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )

    with TestClient(app, base_url=f"http://127.0.0.1:{config.port}") as client:
        discover = _rpc_response(rpc(client, "server/discover"))
        assert discover["result"]["resultType"] == "complete"
        assert "Mcp-Session-Id" not in rpc(client, "server/discover").headers
        tools = _rpc_response(rpc(client, "tools/list"))["result"]
        assert tools["resultType"] == "complete"
        assert "ttlMs" in tools and "cacheScope" in tools
        assert backend.create_count == 0
        hostile = rpc(
            client, "server/discover", extra_headers={"Origin": "https://evil.invalid"}
        )
        assert hostile.status_code == 403
        mismatch = rpc(
            client, "server/discover", extra_headers={"Mcp-Method": "tools/list"}
        )
        assert mismatch.status_code == 400
        secret = "command-and-stdin-secret"
        result = _rpc_response(
            rpc(
                client,
                "tools/call",
                {
                    "name": "terminal_execute",
                    "arguments": {"command": secret, "args": [secret], "stdin": secret},
                },
            )
        )
        assert result["result"]["isError"] is True
        events = client.get("/activity").json()["events"]
        assert len(events) == 2
        assert secret not in json.dumps(events)
        assert events[-1]["operation"] == "terminal_execute"


async def test_headless_resource_reads_fail_before_computer_start() -> None:
    from mcp.server.mcpserver.exceptions import ResourceNotFoundError

    backend = MockBackend(BackendConfig())
    server = create_server(backend, ServerConfig(desktop_environment=False))
    with pytest.raises(ResourceNotFoundError):
        await server.read_resource("computer://screen/current.png")
    assert backend.create_count == 0


@pytest.mark.parametrize("protocol", ["2026-07-28", "2025-11-25"])
def test_stdio_process_is_jsonrpc_clean_and_exits_on_eof(protocol: str) -> None:
    """Exercise real OS pipes, both opening exchanges and graceful stdin closure."""
    source = "\n".join(
        [
            "from kilntainers.server import create_server",
            "from kilntainers.backends.test_utils import MockBackend",
            "from kilntainers.config import BackendConfig, ServerConfig",
            "server = create_server(MockBackend(BackendConfig()), ServerConfig(desktop_environment=False))",
            "server.run()",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", source],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    stdin = process.stdin
    stdout = process.stdout
    lines: queue.Queue[str] = queue.Queue()

    def read_lines():
        for line in stdout:
            lines.put(line)

    reader = threading.Thread(target=read_lines, daemon=True)
    reader.start()

    def request(method, params, request_id):
        if protocol == "2026-07-28":
            params["_meta"] = {
                "io.modelcontextprotocol/protocolVersion": protocol,
                "io.modelcontextprotocol/clientCapabilities": {},
            }
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()
        while True:
            response = json.loads(lines.get(timeout=15))
            assert response["jsonrpc"] == "2.0"
            if response.get("id") == request_id:
                return response

    try:
        if protocol == "2026-07-28":
            opening = request("server/discover", {}, 1)
            assert opening["result"]["resultType"] == "complete"
        else:
            opening = request(
                "initialize",
                {
                    "protocolVersion": protocol,
                    "capabilities": {},
                    "clientInfo": {"name": "regression-test", "version": "1"},
                },
                1,
            )
            assert opening["result"]["protocolVersion"] == protocol
            stdin.write(
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
                + "\n"
            )
            stdin.flush()
        catalog = request("tools/list", {}, 2)
        assert "terminal_execute" in {
            tool["name"] for tool in catalog["result"]["tools"]
        }
        failure = request(
            "tools/call", {"name": "terminal_execute", "arguments": {}}, 3
        )
        assert failure["result"]["isError"] is True
        stdin.close()
        assert process.wait(timeout=15) == 0
        reader.join(timeout=2)
        while not lines.empty():
            assert json.loads(lines.get_nowait())["jsonrpc"] == "2.0"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

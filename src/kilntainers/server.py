"""MCP server implementation."""

import asyncio
import base64
import json
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncContextManager

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from kilntainers.backends.base import Backend, ExecRequest, Sandbox
from kilntainers.computers import ComputerRegistry, random_computer_id
from kilntainers.config import ServerConfig
from kilntainers.dashboard import (
    DASHBOARD_MIME_TYPE,
    DASHBOARD_RESOURCE_META,
    DASHBOARD_URI,
    dashboard_html,
)
from kilntainers.desktop import animate_file_operation
from kilntainers.desktop_control import (
    SCREEN_ACCESSIBILITY_URI,
    SCREEN_IMAGE_URI,
    DesktopControlError,
    accessibility_snapshot,
    capture_screen,
    desktop_action,
    visible_terminal_execute,
)
from kilntainers.errors import BackendError, SandboxDiedError
from kilntainers.file_tools import (
    FileToolError,
    edit_text_file,
    read_text_file,
    write_text_file,
)
from kilntainers.file_tools import (
    list_directory as list_text_directory,
)
from kilntainers.windows_docker import DockerRuntimeProgress

# Constants
STDIN_LIMIT = 2 * 1024 * 1024  # 2 MiB (D32)


# --- Session Context ---


class SessionContext:
    """Per-session state, available to tool handlers via Context.

    Supports lazy sandbox creation — the sandbox is only created on
    the first call to get_or_create_sandbox(). This allows the MCP
    server to respond to non-exec requests (tools/list, etc.) without
    waiting for container startup.
    """

    def __init__(
        self,
        backend: Backend,
        transport: str,
        death_callback: Callable[[], None] | None = None,
        registry: ComputerRegistry | None = None,
        computer_id: str = "virtual-computer",
    ) -> None:
        """Initialize the session context.

        Args:
            backend: The backend to use for sandbox creation.
            transport: The transport mode ("stdio" or "http").
            death_callback: Optional callback for sandbox death in stdio mode.
        """
        self._backend = backend
        self._registry = registry or ComputerRegistry(backend)
        self._transport = transport
        self._death_callback = death_callback
        self._configured_computer_id = computer_id
        self._default_computer_id: str | None = None
        self._current_computer_id: str | None = None
        self._owned_computers: dict[str, bool] = {}
        self._death_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self.last_surface = "desktop"

    @property
    def sandbox(self) -> Sandbox | None:
        """The most recently selected sandbox, or None before first use."""
        if self._current_computer_id is None:
            return None
        return self._registry.peek(self._current_computer_id)

    @property
    def death_task(self) -> asyncio.Task[None] | None:
        """The current computer's death monitor, if one has been created."""
        if self._current_computer_id is None:
            return None
        return self._death_tasks.get(self._current_computer_id)

    @property
    def current_computer_id(self) -> str | None:
        """Stable ID of the computer most recently used by this session."""
        return self._current_computer_id

    @property
    def registry(self) -> ComputerRegistry:
        """Shared computer registry used by management tool handlers."""
        return self._registry

    async def get_or_create_sandbox(
        self,
        computer_id: str | None = None,
        *,
        temporary: bool = False,
        progress: Callable[[DockerRuntimeProgress], Awaitable[None]] | None = None,
    ) -> Sandbox:
        """Get the sandbox, creating it lazily on first call.

        Concurrency-safe: uses asyncio.Lock to ensure only one sandbox
        is created even if multiple calls arrive simultaneously.

        Returns:
            The sandbox instance.

        Raises:
            BackendError: If sandbox creation fails. The next call
                will retry creation.
        """
        async with self._lock:
            await self._backend.ensure_runtime(progress)
            if computer_id is not None and computer_id != self._configured_computer_id:
                raise BackendError(
                    "This server owns one computer selected by COMPUTER_ID; "
                    "tool calls cannot select another computer."
                )
            if temporary:
                raise BackendError("Temporary computers are disabled.")
            target_id = self._configured_computer_id

            if target_id is not None and target_id in self._owned_computers:
                existing = await self._registry.get_owned(target_id)
                if existing is not None:
                    if self._owned_computers[target_id] != temporary:
                        raise BackendError(
                            f"Computer '{target_id}' is already attached with "
                            f"temporary={str(self._owned_computers[target_id]).lower()}."
                        )
                    self._current_computer_id = target_id
                    return existing

            assigned_id, sandbox = await self._registry.acquire(
                target_id,
                temporary=temporary,
                add_owner=True,
            )
            self._owned_computers[assigned_id] = sandbox.temporary
            self._current_computer_id = assigned_id
            self._default_computer_id = assigned_id
            self._start_death_monitor(assigned_id, sandbox)
            return sandbox

    async def refresh_sandbox(self, computer_id: str | None = None) -> Sandbox:
        """Refresh provider-owned state that another MCP process may change."""
        target_id = computer_id or self._current_computer_id
        if target_id is None:
            return await self.get_or_create_sandbox()
        previous = self._registry.peek(target_id)
        sandbox = await self._registry.refresh(target_id)
        if sandbox is None:
            return await self.get_or_create_sandbox(target_id)
        if previous is not sandbox and target_id in self._owned_computers:
            await self._cancel_death_monitor(target_id)
            self._start_death_monitor(target_id, sandbox)
        self._current_computer_id = target_id
        return sandbox

    def _start_death_monitor(self, computer_id: str, sandbox: Sandbox) -> None:
        """Start monitoring sandbox for unexpected death."""

        async def _monitor_death() -> None:
            try:
                await sandbox.wait_for_death()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Unexpected error monitoring sandbox — treat as death
                pass

            # Sandbox died (or monitoring failed)
            if self._transport == "stdio":
                if self._death_callback is not None:
                    self._death_callback()
                else:
                    os.kill(os.getpid(), signal.SIGTERM)

        self._death_tasks[computer_id] = asyncio.create_task(_monitor_death())

    async def _cancel_death_monitor(self, computer_id: str) -> None:
        task = self._death_tasks.pop(computer_id, None)
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def restart_computer(self, computer_id: str) -> Sandbox:
        """Restart through the registry and refresh any session death monitor."""
        await self._cancel_death_monitor(computer_id)
        sandbox = await self._registry.restart(computer_id)
        if computer_id in self._owned_computers:
            self._start_death_monitor(computer_id, sandbox)
        self._current_computer_id = computer_id
        return sandbox

    async def factory_reset_computer(self, computer_id: str) -> Sandbox:
        """Factory-reset through the registry and refresh monitoring."""
        await self._cancel_death_monitor(computer_id)
        sandbox = await self._registry.factory_reset(computer_id)
        if computer_id in self._owned_computers:
            self._start_death_monitor(computer_id, sandbox)
        self._current_computer_id = computer_id
        return sandbox

    async def set_network_access(self, computer_id: str, enabled: bool) -> Sandbox:
        """Change network access and refresh the session's death monitor."""
        await self._cancel_death_monitor(computer_id)
        sandbox = await self._registry.set_network_access(computer_id, enabled)
        if computer_id in self._owned_computers:
            self._start_death_monitor(computer_id, sandbox)
        self._current_computer_id = computer_id
        return sandbox

    async def switch_desktop_environment(
        self,
        computer_id: str,
        enabled: bool,
    ) -> Sandbox:
        """Switch real/virtual desktop mode and refresh death monitoring."""
        await self._cancel_death_monitor(computer_id)
        sandbox = await self._registry.switch_desktop_environment(
            computer_id,
            enabled,
        )
        if computer_id in self._owned_computers:
            self._start_death_monitor(computer_id, sandbox)
        self._current_computer_id = computer_id
        return sandbox

    async def delete_computer(self, computer_id: str) -> None:
        """Delete through the registry and detach it from this session."""
        await self._cancel_death_monitor(computer_id)
        await self._registry.delete(computer_id)
        self._owned_computers.pop(computer_id, None)
        if self._current_computer_id == computer_id:
            self._current_computer_id = None
        if self._default_computer_id == computer_id:
            self._default_computer_id = None

    async def cleanup(self) -> None:
        """Clean up resources. Called by lifespan on exit.

        Safe to call even if no sandbox was ever created (no-op).
        """
        for task in self._death_tasks.values():
            task.cancel()
        for task in self._death_tasks.values():
            try:
                await task
            except asyncio.CancelledError:
                pass
        for computer_id in list(self._owned_computers):
            await self._registry.release(computer_id)


def _result(
    payload: dict[str, Any],
    *,
    is_error: bool = False,
) -> CallToolResult:
    """Create an MCP result with JSON fallback and structured app data."""
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(payload))],
        isError=is_error,
        structuredContent=payload,
    )


def _lifecycle_payload(config: ServerConfig) -> dict[str, bool]:
    """Describe App control availability separately from model visibility."""
    return {
        "lifecycle_tools_exposed": True,
        "lifecycle_tools_model_visible": config.expose_lifecycle_tools,
    }


def _computer_ui_url(config: ServerConfig) -> str:
    """Return the standalone dashboard URL, or the MCP App URI for stdio."""
    if config.transport != "http":
        return DASHBOARD_URI
    host = config.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.port}/dashboard.html"


def _session_from_context(
    ctx: Context[ServerSession, SessionContext] | None,
) -> SessionContext | None:
    if ctx is None:
        return None
    return ctx.request_context.lifespan_context


# --- Tool Description Assembly ---


def assemble_tool_description(
    backend: Backend,
    override: str | None,
    extended: str | None,
) -> str:
    """Assemble the terminal_execute tool description.

    Raises BackendError if the result would be empty.

    Args:
        backend: The backend instance to query for tool instructions.
        override: User-provided description that replaces everything.
        extended: User-provided text to append to backend instructions.

    Returns:
        The assembled tool description text.

    Raises:
        BackendError: If both override and extended are provided, or if
            the result would be empty.
    """
    # Rule 4: Both override and extended is an error
    if override is not None and extended is not None:
        raise BackendError(
            "Cannot use both --tool-instruction-override and "
            "--extended-tool-instruction. Use override to replace "
            "the description entirely, or extended to append to "
            "the backend default."
        )

    # Rule 1: Override replaces everything
    if override is not None:
        return override

    # Rule 2: Backend instructions, optionally extended
    backend_instructions = backend.tool_instructions()

    if not backend_instructions:
        # Rule 3: No backend instructions and no override
        raise BackendError(
            "Backend does not provide tool instructions describing "
            "the sandbox. Supply --tool-instruction-override to "
            "describe the capabilities of this sandbox (example "
            "'a Debian Linux bash shell' or 'A minimal BusyBox "
            "shell with the following commands: ...')."
        )

    if extended is not None:
        return f"{backend_instructions}\n\n{extended}"

    return backend_instructions


# --- Lifespan Factory ---


def create_lifespan(
    backend: Backend,
    transport: str,
    *,
    death_callback: Callable[[], None] | None = None,
    registry: ComputerRegistry | None = None,
    computer_id: str = "virtual-computer",
) -> Callable[[FastMCP], AsyncContextManager[SessionContext]]:
    """Create a lifespan context manager for the given transport.

    The returned context manager creates a SessionContext that supports
    lazy sandbox creation. The sandbox is not created until the first
    terminal_execute call.

    Args:
        backend: The backend to use for creating sandboxes.
        transport: The transport mode ("stdio" or "http").
        death_callback: Optional callback for sandbox death in stdio mode.
            If None, sends SIGTERM to current process. For testing, pass
            a custom callback to capture death notifications.

    Returns:
        An async context manager function compatible with FastMCP.
    """

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[SessionContext]:
        """Create a SessionContext for this session and clean up on exit."""
        ctx = SessionContext(
            backend=backend,
            transport=transport,
            death_callback=death_callback,
            registry=registry,
            computer_id=computer_id,
        )
        try:
            yield ctx
        finally:
            await ctx.cleanup()

    return lifespan


# --- Input Validation ---


def _validate_inputs(
    command: str | None,
    args: list[str] | None,
    stdin: str | None,
    working_directory: str | None,
    timeout: int | None,
) -> str | None:
    """Validate tool inputs.

    Returns error message or None if valid.

    Args:
        command: The shell command string, if using command mode.
        args: The list of arguments, if using args mode.
        stdin: The stdin content to pipe to the command.
        working_directory: The working directory for the command.
        timeout: The timeout in seconds.

    Returns:
        An error message string if validation fails, None otherwise.
    """
    # Exactly one of command or args
    if command is not None and args is not None:
        return "Cannot provide both 'command' and 'args'. Use 'command' for shell commands or 'args' for direct execution."
    if command is None and args is None:
        return "Must provide either 'command' or 'args'."

    # working_directory must be absolute
    if working_directory is not None and not working_directory.startswith("/"):
        return f"working_directory must be an absolute path, got: {working_directory}"

    # timeout must be positive
    if timeout is not None and timeout < 1:
        return "timeout must be at least 1 second."

    # stdin size limit (D32)
    if stdin is not None and len(stdin.encode("utf-8")) > STDIN_LIMIT:
        return (
            f"stdin content exceeds the 2 MiB limit "
            f"({len(stdin.encode('utf-8'))} bytes). "
            f"Split into smaller chunks or use a different approach."
        )

    return None


# --- Tool Handler ---


def _create_handler(config: ServerConfig) -> Callable[..., Any]:
    """Create the terminal_execute handler with server config bound via closure.

    Args:
        config: The server configuration containing defaults.

    Returns:
        An async handler function for the terminal_execute tool.
    """

    async def terminal_execute_handler(
        command: str | None = None,
        args: list[str] | None = None,
        stdin: str | None = None,
        working_directory: str | None = None,
        timeout: int | None = None,
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Handle a terminal_execute tool call.

        Args:
            command: Shell command string (mutually exclusive with args).
            args: List of arguments for direct execution (mutually exclusive with command).
            stdin: Content to pipe to stdin.
            working_directory: Working directory for the command (must be absolute).
            timeout: Timeout in seconds (defaults to server config).
            ctx: FastMCP context object (injected automatically).

        Returns:
            A CallToolResult with the execution result or error.
        """
        def error_result(message: str) -> CallToolResult:
            return _result(
                {
                    "operation": "terminal_execute",
                    "computer_id": config.computer_id,
                    "desktop_environment": config.desktop_environment,
                    "error": message,
                },
                is_error=True,
            )

        # --- Input sanitization ---
        if args is not None and len(args) == 0:
            args = None
        if command is not None and len(command) == 0:
            command = None
        if working_directory is not None and len(working_directory) == 0:
            working_directory = None
        if stdin is not None and len(stdin) == 0:
            stdin = None

        # --- Input validation ---
        error = _validate_inputs(command, args, stdin, working_directory, timeout)
        if error is not None:
            return error_result(error)

        # --- Get sandbox from context ---
        # ctx should always be provided by FastMCP, but handle None for safety
        if ctx is None:
            return error_result("Internal error: no context provided")

        session_context = ctx.request_context.lifespan_context

        async def report_runtime(update: DockerRuntimeProgress) -> None:
            await ctx.report_progress(
                update.progress,
                total=update.total,
                message=update.message,
            )

        # --- Lazy sandbox creation ---
        try:
            sandbox = await session_context.get_or_create_sandbox(
                computer_id=config.computer_id,
                temporary=False,
                progress=report_runtime,
            )
        except BackendError as e:
            return error_result(str(e))

        # --- Construct ExecRequest ---
        # --- Execute ---
        try:
            resolved_timeout = timeout if timeout is not None else config.default_timeout
            if sandbox.desktop_environment and sandbox.desktop_url is not None:
                result = await visible_terminal_execute(
                    sandbox,
                    command=command,
                    args=args,
                    stdin=stdin,
                    working_directory=(
                        working_directory or config.workspace_directory
                    ),
                    timeout=resolved_timeout,
                    output_limit=config.output_limit,
                )
            else:
                request = ExecRequest(
                    command=command,
                    args=args,
                    stdin=stdin,
                    working_directory=working_directory,
                    timeout=resolved_timeout,
                    output_limit=config.output_limit,
                )
                result = await sandbox.exec(request)
        except (DesktopControlError, SandboxDiedError) as e:
            return error_result(str(e))
        session_context.last_surface = "terminal"

        # --- Format response ---
        response = {
            "computer_id": session_context.current_computer_id,
            "operation": "terminal_execute",
            "desktop_environment": sandbox.desktop_environment,
            "network_access": sandbox.network_access,
            "desktop_url": sandbox.desktop_url,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.exit_code,
            "exec_duration_ms": result.exec_duration_ms,
        }
        response_json = json.dumps(response)

        return CallToolResult(
            content=[TextContent(type="text", text=response_json)],
            isError=False,
            structuredContent=response,
        )

    return terminal_execute_handler


def _computer_ui_meta(*, launcher: bool = False) -> dict[str, Any]:
    return {
        "ui": {
            "visibility": ["model", "app"],
            "resourceUri": DASHBOARD_URI,
        },
        "openai/outputTemplate": DASHBOARD_URI,
        "openai/widgetAccessible": True,
    }


def _app_only_meta() -> dict[str, Any]:
    """Hide manual UI plumbing from the model while exposing it to the App."""
    return {"ui": {"visibility": ["app"]}}


def _lifecycle_meta(*, model_visible: bool) -> dict[str, Any]:
    """Lifecycle controls always belong to the App and optionally the model."""
    return {
        "ui": {
            "visibility": ["model", "app"] if model_visible else ["app"],
        }
    }


def _register_computer_tools(mcp: FastMCP, config: ServerConfig) -> None:
    """Register provider-neutral lifecycle tools used by models and the App."""

    async def inventory(session: SessionContext) -> dict[str, Any]:
        computers = await session.registry.list()
        return {
            "computers": [computer.to_dict() for computer in computers],
            "count": len(computers),
        }

    async def computer_dashboard(
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Open the interactive sandbox computer dashboard."""
        session = _session_from_context(ctx)
        if session is None:
            return _result(
                {"error": "Internal error: no context provided"}, is_error=True
            )
        try:
            return _result(await inventory(session))
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)

    async def computer_list(
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """List temporary and permanent computers managed by this backend."""
        session = _session_from_context(ctx)
        if session is None:
            return _result(
                {"error": "Internal error: no context provided"}, is_error=True
            )
        try:
            return _result(await inventory(session))
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)

    async def computer_create(
        computer_id: Annotated[
            str,  # noqa: RUF013
            Field(
                description=(
                    "Optional lowercase slug. Omit to generate a readable random ID."
                )
            ),
        ] = None,  # type: ignore
        temporary: Annotated[
            bool,
            Field(
                description=(
                    "Remove on MCP session shutdown when true; persist and allow "
                    "reattachment by ID when false."
                )
            ),
        ] = True,
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Create or attach to a named sandbox computer."""
        session = _session_from_context(ctx)
        if session is None:
            return _result(
                {"error": "Internal error: no context provided"}, is_error=True
            )
        try:
            requested_id = computer_id or random_computer_id()
            sandbox = await session.get_or_create_sandbox(
                computer_id=requested_id,
                temporary=temporary,
            )
            return _result(
                {
                    "ok": True,
                    "computer_id": session.current_computer_id,
                    "sandbox_id": sandbox.sandbox_id,
                    "temporary": sandbox.temporary,
                }
            )
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)

    async def computer_restart(
        computer_id: Annotated[str, Field(description="Computer slug to restart")],
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Restart a computer while preserving its writable filesystem."""
        session = _session_from_context(ctx)
        if session is None:
            return _result(
                {"error": "Internal error: no context provided"}, is_error=True
            )
        try:
            sandbox = await session.restart_computer(computer_id)
            return _result(
                {
                    "ok": True,
                    "computer_id": computer_id,
                    "sandbox_id": sandbox.sandbox_id,
                    "temporary": sandbox.temporary,
                }
            )
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)

    async def computer_factory_reset(
        computer_id: Annotated[
            str,
            Field(description="Computer slug whose writable state will be erased"),
        ],
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Erase a computer's writable state and recreate it from its base image."""
        session = _session_from_context(ctx)
        if session is None:
            return _result(
                {"error": "Internal error: no context provided"}, is_error=True
            )
        try:
            sandbox = await session.factory_reset_computer(computer_id)
            return _result(
                {
                    "ok": True,
                    "computer_id": computer_id,
                    "sandbox_id": sandbox.sandbox_id,
                    "temporary": sandbox.temporary,
                }
            )
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)

    async def computer_delete(
        computer_id: Annotated[
            str,
            Field(description="Computer slug to permanently delete"),
        ],
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Permanently delete a computer and its writable filesystem."""
        session = _session_from_context(ctx)
        if session is None:
            return _result(
                {"error": "Internal error: no context provided"}, is_error=True
            )
        try:
            await session.delete_computer(computer_id)
            return _result({"ok": True, "computer_id": computer_id, "deleted": True})
        except BackendError as error:
            return _result({"error": str(error)}, is_error=True)

    mcp.add_tool(
        computer_dashboard,
        name="computer_dashboard",
        title="Sandbox Computer Dashboard",
        description=(
            "Open the interactive MCP App dashboard for listing computers, "
            "running terminal commands, restarting, factory-resetting, and deleting."
        ),
        meta=_computer_ui_meta(launcher=True),
    )
    mcp.add_tool(
        computer_list,
        name="computer_list",
        description="List all sandbox computers managed by the selected backend.",
        meta=_computer_ui_meta(),
    )
    mcp.add_tool(
        computer_create,
        name="computer_create",
        description=(
            "Create a temporary or permanent sandbox computer. If computer_id is "
            "omitted, a readable random slug is returned."
        ),
        meta=_computer_ui_meta(),
    )
    mcp.add_tool(
        computer_restart,
        name="computer_restart",
        description="Restart a computer without erasing its writable filesystem.",
        meta=_computer_ui_meta(),
    )
    mcp.add_tool(
        computer_factory_reset,
        name="computer_factory_reset",
        description=(
            "Erase a computer's writable filesystem and recreate it from the base image."
        ),
        meta=_computer_ui_meta(),
    )
    mcp.add_tool(
        computer_delete,
        name="computer_delete",
        description="Permanently delete a computer and all of its writable state.",
        meta=_computer_ui_meta(),
    )

    @mcp.resource(
        DASHBOARD_URI,
        name="Sandbox Computer Dashboard",
        title="Sandbox Computer Dashboard",
        description="Interactive lifecycle and terminal dashboard for sandbox computers.",
        mime_type=DASHBOARD_MIME_TYPE,
        meta=DASHBOARD_RESOURCE_META,
    )
    def computer_dashboard_resource() -> str:
        return dashboard_html()


def _enable_mcp_apps_capability(mcp: FastMCP) -> None:
    """Advertise the stable MCP Apps extension missing from MCP SDK 1.x types."""
    from mcp.types import ServerCapabilities

    low_level_server = mcp._mcp_server
    original = low_level_server.get_capabilities

    def get_capabilities_with_apps(
        notification_options: Any,
        experimental_capabilities: dict[str, dict[str, Any]],
    ) -> ServerCapabilities:
        capabilities = original(notification_options, experimental_capabilities)
        payload = capabilities.model_dump(by_alias=True, exclude_none=True)
        payload["extensions"] = {
            "io.modelcontextprotocol/ui": {"mimeTypes": [DASHBOARD_MIME_TYPE]}
        }
        return ServerCapabilities.model_validate(payload)

    setattr(low_level_server, "get_capabilities", get_capabilities_with_apps)


# --- Server Factory ---


def create_server(
    backend: Backend,
    config: ServerConfig,
) -> FastMCP:
    """Create and configure the MCP server.

    Args:
        backend: Validated backend instance.
        config: Server configuration (transport, host, port, timeouts, etc.).

    Returns:
        Configured FastMCP instance ready to run.

    Raises:
        BackendError: If tool description assembly fails.
    """
    # Assemble tool description
    description = assemble_tool_description(
        backend,
        override=config.tool_instruction_override,
        extended=config.extended_tool_instruction,
    )

    # Create lifespan that captures the backend and transport
    registry = ComputerRegistry(backend)
    lifespan = create_lifespan(
        backend,
        config.transport,
        registry=registry,
        computer_id=config.computer_id,
    )

    # Create server
    mcp = FastMCP(
        name="MCP Virtual Computer",
        lifespan=lifespan,
        host=config.host,
        port=config.port,
    )

    activity_revision = 0
    activity_events: list[dict[str, Any]] = []

    def publish_activity(
        phase: str,
        operation: str,
        arguments: dict[str, Any],
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Expose genuine MCP tool lifecycle events to the local dashboard."""
        nonlocal activity_revision
        activity_revision += 1
        activity_events.append(
            {
                "revision": activity_revision,
                "phase": phase,
                "operation": operation,
                "arguments": arguments,
                "payload": payload,
            }
        )
        del activity_events[:-64]

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/activity", methods=["GET"], include_in_schema=False)
    async def activity(request: Request) -> JSONResponse:
        try:
            after = max(0, int(request.query_params.get("after", "0")))
        except ValueError:
            after = 0
        return JSONResponse(
            {
                "revision": activity_revision,
                "events": [event for event in activity_events if event["revision"] > after],
            },
            headers={"Cache-Control": "no-store"},
        )

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def service_info(request: Request) -> JSONResponse:
        live_sandbox = registry.peek(config.computer_id)
        payload = {
            "name": "mcp-virtual-computer",
            "mcp_endpoint": "/mcp",
            "health": "/healthz",
            "computer_id": config.computer_id,
            **_lifecycle_payload(config),
            "desktop_environment": (
                live_sandbox.desktop_environment
                if live_sandbox is not None
                else config.desktop_environment
            ),
            "network_access": (
                live_sandbox.network_access
                if live_sandbox is not None
                else config.network_access
            ),
            "app_resource": DASHBOARD_URI,
        }
        return JSONResponse(payload)

    @mcp.custom_route("/dashboard.html", methods=["GET"], include_in_schema=False)
    async def dashboard_page(request: Request) -> HTMLResponse:
        return HTMLResponse(dashboard_html())

    handler = _create_handler(config)

    # Wrapper closure for better MCP type hinting
    # type ignore and noqa needed to get the right type hints. Type hinting doesn't work for Optional[str] so str but assign None as default.
    async def terminal_execute(
        command: Annotated[
            str,  # noqa: RUF013
            Field(description="Shell command string (mutually exclusive with args)."),
        ] = None,  # type: ignore
        args: Annotated[
            list[str],  # noqa: RUF013
            Field(
                description="List of arguments for direct execution (mutually exclusive with command)."
            ),
        ] = None,  # type: ignore
        stdin: Annotated[str, Field(description="Content to pipe to stdin.")] = None,  # type: ignore # noqa: RUF013
        working_directory: Annotated[
            str,  # noqa: RUF013
            Field(description="Working directory for the command (must be absolute)."),
        ] = None,  # type: ignore
        timeout: Annotated[
            int,  # noqa: RUF013
            Field(description="Timeout in seconds (defaults to server config)."),
        ] = None,  # type: ignore
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        arguments = {
            key: value
            for key, value in {
                "command": command,
                "args": args,
                "stdin": stdin,
                "working_directory": working_directory,
                "timeout": timeout,
            }.items()
            if value is not None
        }
        publish_activity("request", "terminal_execute", arguments)
        result = await handler(
            command=command,
            args=args,
            stdin=stdin,
            working_directory=working_directory,
            timeout=timeout,
            ctx=ctx,
        )
        payload = dict(result.structuredContent or {})
        publish_activity("result", "terminal_execute", arguments, payload)
        return result

    mcp.add_tool(
        terminal_execute,
        name="terminal_execute",
        description=description,
    )

    async def computer_ui(
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Open the Three.js virtual computer."""
        computer_url = _computer_ui_url(config)
        session = _session_from_context(ctx)
        if session is None:
            return _result(
                {
                    "url": computer_url,
                    "resource_uri": DASHBOARD_URI,
                    "error": "Internal error: no context provided",
                },
                is_error=True,
            )
        backend.start_runtime_preparation()
        runtime = backend.runtime_status()
        if runtime.get("runtime_state") != "ready":
            return _result(
                {
                    "operation": "idle",
                    "url": computer_url,
                    "resource_uri": DASHBOARD_URI,
                    "computer_id": config.computer_id,
                    **_lifecycle_payload(config),
                    "computer_attached": False,
                    "desktop_environment": config.desktop_environment,
                    "network_access": config.network_access,
                    "workspace_directory": config.workspace_directory,
                    **runtime,
                }
            )
        try:
            await session.get_or_create_sandbox()
            sandbox = await session.refresh_sandbox(config.computer_id)
        except (BackendError, SandboxDiedError) as error:
            return _result(
                {
                    "url": computer_url,
                    "resource_uri": DASHBOARD_URI,
                    "error": str(error),
                },
                is_error=True,
            )
        capabilities_changed = False
        if desktop_capability_sync is not None:
            capabilities_changed = desktop_capability_sync(
                sandbox.desktop_environment
            )
        if capabilities_changed and ctx is not None:
            await ctx.session.send_tool_list_changed()
            await ctx.session.send_resource_list_changed()
        return _result(
            {
                "operation": "idle",
                "url": computer_url,
                "resource_uri": DASHBOARD_URI,
                "computer_id": config.computer_id,
                **_lifecycle_payload(config),
                "computer_attached": True,
                "desktop_environment": sandbox.desktop_environment,
                "network_access": sandbox.network_access,
                "desktop_url": sandbox.desktop_url,
                "workspace_directory": config.workspace_directory,
                **backend.runtime_status(),
            }
        )

    mcp.add_tool(
        computer_ui,
        name="computer_ui",
        title="Open Virtual Computer",
        description="Open the interactive Three.js laptop and its computer screen.",
        meta=_computer_ui_meta(launcher=True),
    )

    async def runtime_status(
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Return lazy host-runtime setup state for the computer App."""
        session = _session_from_context(ctx)
        backend.start_runtime_preparation()
        runtime = backend.runtime_status()
        sandbox = registry.peek(config.computer_id)
        if session is not None and runtime.get("runtime_state") == "ready":
            try:
                if sandbox is None:
                    sandbox = await session.get_or_create_sandbox(config.computer_id)
                else:
                    sandbox = await session.refresh_sandbox(config.computer_id)
            except (BackendError, SandboxDiedError) as error:
                return _result(
                    {
                        "operation": "idle",
                        "computer_id": config.computer_id,
                        **_lifecycle_payload(config),
                        "computer_attached": False,
                        "desktop_environment": config.desktop_environment,
                        "network_access": config.network_access,
                        "desktop_url": None,
                        "workspace_directory": config.workspace_directory,
                        **runtime,
                        "error": str(error),
                    },
                    is_error=True,
                )
        capabilities_changed = False
        if sandbox is not None and desktop_capability_sync is not None:
            capabilities_changed = desktop_capability_sync(
                sandbox.desktop_environment
            )
        if capabilities_changed and ctx is not None:
            await ctx.session.send_tool_list_changed()
            await ctx.session.send_resource_list_changed()
        return _result(
            {
                "operation": "idle",
                "computer_id": config.computer_id,
                **_lifecycle_payload(config),
                "computer_attached": sandbox is not None,
                "desktop_environment": (
                    sandbox.desktop_environment
                    if sandbox is not None
                    else config.desktop_environment
                ),
                "network_access": (
                    sandbox.network_access
                    if sandbox is not None
                    else config.network_access
                ),
                "desktop_url": sandbox.desktop_url if sandbox is not None else None,
                "workspace_directory": config.workspace_directory,
                **runtime,
            }
        )

    runtime_switch_lock = asyncio.Lock()
    desktop_capability_sync: Callable[[bool], bool] | None = None

    async def set_network_access(
        enabled: Annotated[
            bool,
            Field(description="Whether the Docker computer may access the network."),
        ],
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Plug in or unplug the virtual computer's real network connection."""
        session = _session_from_context(ctx)
        if session is None:
            return _result({"error": "Internal error: no context provided"}, is_error=True)
        try:
            async with runtime_switch_lock:
                await session.get_or_create_sandbox()
                sandbox = await session.set_network_access(config.computer_id, enabled)
            return _result(
                {
                    "operation": "idle",
                    "computer_id": config.computer_id,
                    **_lifecycle_payload(config),
                    "desktop_environment": sandbox.desktop_environment,
                    "desktop_url": sandbox.desktop_url,
                    "network_access": sandbox.network_access,
                    "workspace_directory": config.workspace_directory,
                }
            )
        except (BackendError, SandboxDiedError) as error:
            return _result({"error": str(error)}, is_error=True)

    async def set_desktop_environment(
        enabled: Annotated[
            bool,
            Field(description="Use a real Xfce desktop instead of the virtual desktop."),
        ],
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Switch the same Docker computer between virtual and real desktops."""
        session = _session_from_context(ctx)
        if session is None:
            return _result({"error": "Internal error: no context provided"}, is_error=True)
        try:
            async with runtime_switch_lock:
                await session.get_or_create_sandbox()
                sandbox = await session.switch_desktop_environment(
                    config.computer_id,
                    enabled,
                )
                if desktop_capability_sync is not None:
                    desktop_capability_sync(sandbox.desktop_environment)
            if ctx is not None:
                await ctx.session.send_tool_list_changed()
                await ctx.session.send_resource_list_changed()
            return _result(
                {
                    "operation": "idle",
                    "computer_id": config.computer_id,
                    **_lifecycle_payload(config),
                    "desktop_environment": sandbox.desktop_environment,
                    "desktop_url": sandbox.desktop_url,
                    "network_access": sandbox.network_access,
                    "workspace_directory": config.workspace_directory,
                }
            )
        except (BackendError, SandboxDiedError) as error:
            return _result({"error": str(error)}, is_error=True)

    lifecycle_meta = _lifecycle_meta(model_visible=config.expose_lifecycle_tools)
    mcp.add_tool(
        runtime_status,
        name="runtime_status",
        description="Read genuine lazy container-runtime setup progress.",
        meta=lifecycle_meta,
    )
    mcp.add_tool(
        set_network_access,
        name="set_network_access",
        description="Change the running Docker computer's real network access.",
        meta=lifecycle_meta,
    )
    mcp.add_tool(
        set_desktop_environment,
        name="set_desktop_environment",
        description="Switch the running computer between virtual and Xfce desktops.",
        meta=lifecycle_meta,
    )

    async def _sandbox_for_file_tool(
        ctx: Context[ServerSession, SessionContext] | None,
    ) -> tuple[SessionContext, Sandbox]:
        session = _session_from_context(ctx)
        if session is None:
            raise FileToolError("Internal error: no context provided")

        async def report_runtime(update: DockerRuntimeProgress) -> None:
            assert ctx is not None
            await ctx.report_progress(
                update.progress,
                total=update.total,
                message=update.message,
            )

        return session, await session.get_or_create_sandbox(progress=report_runtime)

    async def list_directory(
        path: Annotated[
            str,
            Field(description="Directory path, absolute or relative to /workspace."),
        ] = ".",
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """List a real Docker directory for manual Explorer interaction."""
        try:
            _session, sandbox = await _sandbox_for_file_tool(ctx)
            listing = await list_text_directory(
                sandbox,
                path,
                workspace_directory=config.workspace_directory,
                timeout=config.default_timeout,
                output_limit=config.output_limit,
            )
            return _result(
                {
                    "computer_id": config.computer_id,
                    "workspace_directory": config.workspace_directory,
                    "path": listing.path,
                    "entries": [
                        {
                            "name": entry.name,
                            "path": entry.path,
                            "kind": entry.kind,
                            "size_bytes": entry.size_bytes,
                        }
                        for entry in listing.entries
                    ],
                }
            )
        except (BackendError, SandboxDiedError) as error:
            return _result({"error": str(error)}, is_error=True)

    mcp.add_tool(
        list_directory,
        name="list_directory",
        description="List a Docker directory for the interactive virtual Explorer.",
        meta=_app_only_meta(),
    )

    def file_payload(
        *,
        operation: str,
        sandbox: Sandbox,
        path: str,
        content: str,
        size_bytes: int,
        sha256: str,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "operation": operation,
            "computer_id": config.computer_id,
            "desktop_environment": sandbox.desktop_environment,
            "network_access": sandbox.network_access,
            "desktop_url": sandbox.desktop_url,
            "workspace_directory": config.workspace_directory,
            "path": path,
            "content": content,
            "size_bytes": size_bytes,
            "sha256": sha256,
            **extra,
        }

    async def read_file(
        path: Annotated[
            str,
            Field(description="UTF-8 text file path, absolute or relative to /workspace."),
        ],
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Read a UTF-8 text file while the virtual computer opens and scrolls it."""
        arguments = {"path": path}
        publish_activity("request", "read_file", arguments)
        try:
            session, sandbox = await _sandbox_for_file_tool(ctx)
            file = await read_text_file(
                sandbox,
                path,
                workspace_directory=config.workspace_directory,
                timeout=config.default_timeout,
                output_limit=config.output_limit,
                text_limit=config.file_text_limit,
            )
            warning = await animate_file_operation(
                sandbox,
                operation="read_file",
                path=file.path,
                content=None,
                original_content=None,
                old_text=None,
                new_text=None,
                replace_all=False,
                terminal_was_last=session.last_surface == "terminal",
                workspace_directory=config.workspace_directory,
            )
            session.last_surface = "file"
            payload = file_payload(
                operation="read_file",
                sandbox=sandbox,
                path=file.path,
                content=file.content,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                visualization_warning=warning,
            )
            publish_activity("result", "read_file", arguments, payload)
            return _result(payload)
        except (BackendError, SandboxDiedError) as error:
            payload = {"error": str(error), "operation": "read_file"}
            publish_activity("result", "read_file", arguments, payload)
            return _result(payload, is_error=True)

    async def write_file(
        path: Annotated[
            str,
            Field(description="UTF-8 text file path, absolute or relative to /workspace."),
        ],
        content: Annotated[str, Field(description="Complete UTF-8 text to save.")],
        create_parent_directories: Annotated[
            bool,
            Field(description="Create missing parent folders before saving."),
        ] = True,
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Atomically write a UTF-8 file while the virtual editor types it."""
        arguments = {
            "path": path,
            "content": content,
            "create_parent_directories": create_parent_directories,
        }
        publish_activity("request", "write_file", arguments)
        try:
            session, sandbox = await _sandbox_for_file_tool(ctx)
            original_content: str | None = None
            if sandbox.desktop_url is not None:
                try:
                    original = await read_text_file(
                        sandbox,
                        path,
                        workspace_directory=config.workspace_directory,
                        timeout=config.default_timeout,
                        output_limit=config.output_limit,
                        text_limit=config.file_text_limit,
                    )
                    original_content = original.content
                except FileToolError:
                    pass
            file = await write_text_file(
                sandbox,
                path,
                content,
                workspace_directory=config.workspace_directory,
                timeout=config.default_timeout,
                output_limit=config.output_limit,
                text_limit=config.file_text_limit,
                create_parent_directories=create_parent_directories,
            )
            warning = await animate_file_operation(
                sandbox,
                operation="write_file",
                path=file.path,
                content=file.content,
                original_content=original_content,
                old_text=None,
                new_text=None,
                replace_all=False,
                terminal_was_last=session.last_surface == "terminal",
                workspace_directory=config.workspace_directory,
            )
            session.last_surface = "file"
            payload = file_payload(
                operation="write_file",
                sandbox=sandbox,
                path=file.path,
                content=file.content,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                created_parent_directories=create_parent_directories,
                visualization_warning=warning,
            )
            publish_activity("result", "write_file", arguments, payload)
            return _result(payload)
        except (BackendError, SandboxDiedError) as error:
            payload = {"error": str(error), "operation": "write_file"}
            publish_activity("result", "write_file", arguments, payload)
            return _result(payload, is_error=True)

    async def edit_file(
        path: Annotated[
            str,
            Field(description="UTF-8 text file path, absolute or relative to /workspace."),
        ],
        old_text: Annotated[
            str,
            Field(description="Exact text to select and replace; include context if ambiguous."),
        ],
        new_text: Annotated[str, Field(description="Replacement UTF-8 text.")],
        replace_all: Annotated[
            bool,
            Field(description="Replace every exact match instead of requiring one match."),
        ] = False,
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        """Replace exact text while the virtual editor selects and overwrites it."""
        arguments = {
            "path": path,
            "old_text": old_text,
            "new_text": new_text,
            "replace_all": replace_all,
        }
        publish_activity("request", "edit_file", arguments)
        try:
            session, sandbox = await _sandbox_for_file_tool(ctx)
            file, replacements, original_content = await edit_text_file(
                sandbox,
                path,
                old_text,
                new_text,
                replace_all=replace_all,
                workspace_directory=config.workspace_directory,
                timeout=config.default_timeout,
                output_limit=config.output_limit,
                text_limit=config.file_text_limit,
            )
            warning = await animate_file_operation(
                sandbox,
                operation="edit_file",
                path=file.path,
                content=file.content,
                original_content=original_content,
                old_text=old_text,
                new_text=new_text,
                replace_all=replace_all,
                terminal_was_last=session.last_surface == "terminal",
                workspace_directory=config.workspace_directory,
            )
            session.last_surface = "file"
            payload = file_payload(
                operation="edit_file",
                sandbox=sandbox,
                path=file.path,
                content=file.content,
                size_bytes=file.size_bytes,
                sha256=file.sha256,
                old_text=old_text,
                new_text=new_text,
                replacements=replacements,
                visualization_warning=warning,
            )
            publish_activity("result", "edit_file", arguments, payload)
            return _result(payload)
        except (BackendError, SandboxDiedError) as error:
            payload = {"error": str(error), "operation": "edit_file"}
            publish_activity("result", "edit_file", arguments, payload)
            return _result(payload, is_error=True)

    for tool, name, tool_description in (
        (read_file, "read_file", "Read a UTF-8 text file and show it being opened and scrolled on the virtual computer."),
        (write_file, "write_file", "Write a UTF-8 text file and show it being typed and saved on the virtual computer."),
        (edit_file, "edit_file", "Replace exact UTF-8 text and show it being selected, overwritten, and saved."),
    ):
        mcp.add_tool(tool, name=name, description=tool_description)

    # Define desktop capabilities once, then add/remove them when the mug changes
    # the real runtime mode. This keeps tools/list honest in headless mode.
    if True:

        async def _live_desktop(
            ctx: Context[ServerSession, SessionContext] | None,
        ) -> Sandbox:
            _session, sandbox = await _sandbox_for_file_tool(ctx)
            if sandbox.desktop_url is None:
                raise DesktopControlError(
                    "The configured computer has no live Xfce desktop."
                )
            return sandbox

        async def _resource_desktop() -> Sandbox:
            _computer_id, sandbox = await registry.acquire(
                config.computer_id,
                temporary=False,
                add_owner=False,
            )
            if sandbox.desktop_url is None:
                raise DesktopControlError(
                    "The configured computer has no live Xfce desktop."
                )
            return sandbox

        @mcp.resource(
            SCREEN_IMAGE_URI,
            name="Current Computer Screen",
            title="Current Computer Screen",
            description="Current PNG capture of the live Xfce framebuffer.",
            mime_type="image/png",
        )
        async def current_screen_resource() -> bytes:
            return await capture_screen(await _resource_desktop())

        @mcp.resource(
            SCREEN_ACCESSIBILITY_URI,
            name="Current Screen Accessibility Snapshot",
            title="Current Screen Accessibility Snapshot",
            description=(
                "Current AT-SPI element tree with stable references and screen bounds."
            ),
            mime_type="application/json",
        )
        async def current_accessibility_resource() -> str:
            snapshot = await accessibility_snapshot(await _resource_desktop())
            return json.dumps(snapshot, ensure_ascii=False, indent=2)

        async def look_at_screen(
            include_image: Annotated[
                bool,
                Field(description="Include the current PNG framebuffer capture."),
            ] = True,
            include_accessibility: Annotated[
                bool,
                Field(description="Include the current AT-SPI accessibility snapshot."),
            ] = True,
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Look at the live desktop as pixels, accessible elements, or both."""
            if not include_image and not include_accessibility:
                return _result(
                    {"error": "Enable include_image, include_accessibility, or both."},
                    is_error=True,
                )
            try:
                sandbox = await _live_desktop(ctx)
                content: list[Any] = []
                payload: dict[str, Any] = {
                    "computer_id": config.computer_id,
                    "screen_image_uri": SCREEN_IMAGE_URI,
                    "accessibility_uri": SCREEN_ACCESSIBILITY_URI,
                }
                if include_accessibility:
                    snapshot = await accessibility_snapshot(sandbox)
                    payload["accessibility"] = snapshot
                    content.append(
                        TextContent(
                            type="text",
                            text=str(snapshot.get("snapshot", "")),
                        )
                    )
                if include_image:
                    image = await capture_screen(sandbox)
                    content.append(
                        ImageContent(
                            type="image",
                            data=base64.b64encode(image).decode("ascii"),
                            mimeType="image/png",
                        )
                    )
                return CallToolResult(
                    content=content,
                    isError=False,
                    structuredContent=payload,
                )
            except (BackendError, DesktopControlError, SandboxDiedError) as error:
                return _result({"error": str(error)}, is_error=True)

        async def _run_desktop_tool(
            ctx: Context[ServerSession, SessionContext] | None,
            action: str,
            payload: dict[str, Any],
        ) -> CallToolResult:
            try:
                sandbox = await _live_desktop(ctx)
                response = await desktop_action(sandbox, action, payload)
                return _result(
                    {
                        "computer_id": config.computer_id,
                        "desktop_url": sandbox.desktop_url,
                        **response,
                    }
                )
            except (BackendError, DesktopControlError, SandboxDiedError) as error:
                return _result({"error": str(error)}, is_error=True)

        async def click(
            element: Annotated[
                str,  # noqa: RUF013
                Field(description="AT-SPI ref from look_at_screen (for example atspi:8/0/2)."),
            ] = None,  # type: ignore
            x: Annotated[int, Field(description="Desktop X coordinate.")] = None,  # type: ignore # noqa: RUF013
            y: Annotated[int, Field(description="Desktop Y coordinate.")] = None,  # type: ignore # noqa: RUF013
            button: Annotated[
                str,
                Field(description="Mouse button: left, middle, or right."),
            ] = "left",
            clicks: Annotated[
                int,
                Field(description="Click count from 1 to 3.", ge=1, le=3),
            ] = 1,
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Click a live accessibility element or desktop coordinates."""
            return await _run_desktop_tool(
                ctx,
                "click",
                {"element": element, "x": x, "y": y, "button": button, "clicks": clicks},
            )

        async def type_on_screen(
            text: Annotated[str, Field(description="Text to type into the active control.")],
            element: Annotated[
                str,  # noqa: RUF013
                Field(description="Optional AT-SPI ref to focus before typing."),
            ] = None,  # type: ignore
            clear: Annotated[
                bool,
                Field(description="Select existing content with Ctrl+A before typing."),
            ] = False,
            press_enter: Annotated[
                bool,
                Field(description="Press Enter after typing."),
            ] = False,
            delay_ms: Annotated[
                int,
                Field(description="Delay between keystrokes in milliseconds.", ge=0, le=100),
            ] = 2,
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Type into the active Xfce control, optionally focusing it first."""
            return await _run_desktop_tool(
                ctx,
                "type",
                {
                    "text": text,
                    "element": element,
                    "clear": clear,
                    "press_enter": press_enter,
                    "delay_ms": delay_ms,
                },
            )

        async def scroll(
            direction: Annotated[
                str,
                Field(description="Scroll direction: up, down, left, or right."),
            ] = "down",
            amount: Annotated[
                int,
                Field(description="Number of wheel steps.", ge=1, le=50),
            ] = 3,
            element: Annotated[
                str,  # noqa: RUF013
                Field(description="Optional AT-SPI ref to scroll over."),
            ] = None,  # type: ignore
            x: Annotated[int, Field(description="Optional desktop X coordinate.")] = None,  # type: ignore # noqa: RUF013
            y: Annotated[int, Field(description="Optional desktop Y coordinate.")] = None,  # type: ignore # noqa: RUF013
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Scroll the live desktop over an element, coordinates, or current pointer."""
            return await _run_desktop_tool(
                ctx,
                "scroll",
                {"direction": direction, "amount": amount, "element": element, "x": x, "y": y},
            )

        async def list_windows(
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """List live Xfce windows with IDs, geometry, class, title, and state."""
            return await _run_desktop_tool(ctx, "list_windows", {})

        async def switch_window(
            window: Annotated[
                str,
                Field(description="Window ID, exact title/class, or an unambiguous substring."),
            ],
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Activate and raise a live Xfce window."""
            return await _run_desktop_tool(ctx, "switch_window", {"window": window})

        async def move_window(
            window: Annotated[str, Field(description="Window ID, title, or class.")],
            x: Annotated[int, Field(description="New desktop X coordinate.")],
            y: Annotated[int, Field(description="New desktop Y coordinate.")],
            width: Annotated[int, Field(description="Optional new width.", ge=1)] = None,  # type: ignore # noqa: RUF013
            height: Annotated[int, Field(description="Optional new height.", ge=1)] = None,  # type: ignore # noqa: RUF013
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Move and optionally resize a live Xfce window."""
            return await _run_desktop_tool(
                ctx,
                "move_window",
                {"window": window, "x": x, "y": y, "width": width, "height": height},
            )

        async def _window_tool(
            ctx: Context[ServerSession, SessionContext] | None,
            action: str,
            window: str,
        ) -> CallToolResult:
            return await _run_desktop_tool(ctx, action, {"window": window})

        async def maximize_window(
            window: Annotated[str, Field(description="Window ID, title, or class.")],
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Maximize a live Xfce window."""
            return await _window_tool(ctx, "maximize_window", window)

        async def restore_window(
            window: Annotated[str, Field(description="Window ID, title, or class.")],
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Restore and activate a minimized or maximized Xfce window."""
            return await _window_tool(ctx, "restore_window", window)

        async def minimize_window(
            window: Annotated[str, Field(description="Window ID, title, or class.")],
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Minimize a live Xfce window."""
            return await _window_tool(ctx, "minimize_window", window)

        async def close_window(
            window: Annotated[str, Field(description="Window ID, title, or class.")],
            ctx: Context[ServerSession, SessionContext] | None = None,
        ) -> CallToolResult:
            """Close a live Xfce window."""
            return await _window_tool(ctx, "close_window", window)

        desktop_tool_specs: list[
            tuple[Callable[..., Any], str, str, dict[str, Any] | None]
        ] = [
            (
                look_at_screen,
                "look_at_screen",
                "Return the live Xfce screen as PNG pixels and/or an AT-SPI snapshot.",
                None,
            ),
            (click, "click", "Click an AT-SPI element reference or screen coordinates.", None),
            (type_on_screen, "type", "Type into the active Xfce control.", None),
            (scroll, "scroll", "Scroll the live Xfce desktop.", None),
            (list_windows, "list_windows", "List live Xfce windows.", None),
            (switch_window, "switch_window", "Activate and raise an Xfce window.", None),
            (move_window, "move_window", "Move or resize an Xfce window.", None),
            (maximize_window, "maximize_window", "Maximize an Xfce window.", None),
            (restore_window, "restore_window", "Restore an Xfce window.", None),
            (minimize_window, "minimize_window", "Minimize an Xfce window.", None),
            (close_window, "close_window", "Close an Xfce window.", None),
        ]
        for tool, name, tool_description, meta in desktop_tool_specs:
            mcp.add_tool(tool, name=name, description=tool_description, meta=meta)

        desktop_resource_objects = {
            uri: mcp._resource_manager._resources[uri]
            for uri in (SCREEN_IMAGE_URI, SCREEN_ACCESSIBILITY_URI)
        }
        desktop_capabilities_enabled = True

        def sync_desktop_capabilities(enabled: bool) -> bool:
            nonlocal desktop_capabilities_enabled
            if desktop_capabilities_enabled == enabled:
                return False
            if enabled:
                for tool, name, tool_description, meta in desktop_tool_specs:
                    mcp.add_tool(
                        tool,
                        name=name,
                        description=tool_description,
                        meta=meta,
                    )
                for resource in desktop_resource_objects.values():
                    mcp.add_resource(resource)
            else:
                for _tool, name, _description, _meta in desktop_tool_specs:
                    mcp.remove_tool(name)
                for uri in desktop_resource_objects:
                    mcp._resource_manager._resources.pop(uri, None)
            desktop_capabilities_enabled = enabled
            return True

        desktop_capability_sync = sync_desktop_capabilities
        desktop_capability_sync(config.desktop_environment)

    @mcp.resource(
        DASHBOARD_URI,
        name="Virtual Computer",
        title="Virtual Computer",
        description="Three.js laptop-on-desk view for terminal and file operations.",
        mime_type=DASHBOARD_MIME_TYPE,
        meta=DASHBOARD_RESOURCE_META,
    )
    def virtual_computer_resource() -> str:
        return dashboard_html()

    _enable_mcp_apps_capability(mcp)

    return mcp

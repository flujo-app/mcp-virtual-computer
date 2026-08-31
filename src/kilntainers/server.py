"""MCP server implementation."""

import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncContextManager

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from kilntainers.backends.base import Backend, ExecRequest, Sandbox
from kilntainers.computers import ComputerRegistry, random_computer_id
from kilntainers.config import ServerConfig
from kilntainers.dashboard import (
    DASHBOARD_MIME_TYPE,
    DASHBOARD_RESOURCE_META,
    DASHBOARD_URI,
    dashboard_html,
)
from kilntainers.errors import BackendError, SandboxDiedError

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
        self._default_computer_id: str | None = None
        self._current_computer_id: str | None = None
        self._owned_computers: dict[str, bool] = {}
        self._death_tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

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
        temporary: bool = True,
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
            target_id = computer_id
            if target_id is None and self._default_computer_id is not None:
                target_id = self._default_computer_id
                temporary = self._owned_computers[target_id]

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
            if computer_id is None and self._default_computer_id is None:
                self._default_computer_id = assigned_id
            self._start_death_monitor(assigned_id, sandbox)
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
        computer_id: str | None = None,
        temporary: bool = True,
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
            return CallToolResult(
                content=[TextContent(type="text", text=error)],
                isError=True,
            )

        # --- Get sandbox from context ---
        # ctx should always be provided by FastMCP, but handle None for safety
        if ctx is None:
            return CallToolResult(
                content=[
                    TextContent(type="text", text="Internal error: no context provided")
                ],
                isError=True,
            )

        session_context = ctx.request_context.lifespan_context

        # --- Lazy sandbox creation ---
        try:
            sandbox = await session_context.get_or_create_sandbox(
                computer_id=computer_id,
                temporary=temporary,
            )
        except BackendError as e:
            return CallToolResult(
                content=[TextContent(type="text", text=str(e))],
                isError=True,
            )

        # --- Construct ExecRequest ---
        request = ExecRequest(
            command=command,
            args=args,
            stdin=stdin,
            working_directory=working_directory,
            timeout=timeout if timeout is not None else config.default_timeout,
            output_limit=config.output_limit,
        )

        # --- Execute ---
        try:
            result = await sandbox.exec(request)
        except SandboxDiedError as e:
            return CallToolResult(
                content=[TextContent(type="text", text=str(e))],
                isError=True,
            )

        # --- Format response ---
        response = {
            "computer_id": session_context.current_computer_id,
            "temporary": sandbox.temporary,
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
    ui: dict[str, Any] = {"visibility": ["model", "app"]}
    if launcher:
        ui["resourceUri"] = DASHBOARD_URI
    return {"ui": ui}


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
    lifespan = create_lifespan(backend, config.transport, registry=registry)

    # Create server
    mcp = FastMCP(
        name="Kilntainers",
        lifespan=lifespan,
        host=config.host,
        port=config.port,
    )

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/", methods=["GET"], include_in_schema=False)
    async def service_info(request: Request) -> JSONResponse:
        payload = {
            "name": "mcp-sandbox-computer-vm-for-ai",
            "mcp_endpoint": "/mcp",
            "health": "/healthz",
        }
        if config.enable_lifecycle_tools:
            payload["dashboard_resource"] = DASHBOARD_URI
        return JSONResponse(payload)

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
        computer_id: Annotated[
            str,  # noqa: RUF013
            Field(
                description=(
                    "Stable computer slug. Omit to create and select a readable "
                    "random ID for this MCP session."
                )
            ),
        ] = None,  # type: ignore
        temporary: Annotated[
            bool,
            Field(
                description=(
                    "Remove the computer when its MCP session shuts down. Set "
                    "false to keep it provider-side and reconnect by computer_id."
                )
            ),
        ] = True,
        ctx: Context[ServerSession, SessionContext] | None = None,
    ) -> CallToolResult:
        return await handler(
            command=command,
            args=args,
            stdin=stdin,
            working_directory=working_directory,
            timeout=timeout,
            computer_id=computer_id,
            temporary=temporary,
            ctx=ctx,
        )

    mcp.add_tool(
        terminal_execute,
        name="terminal_execute",
        description=description,
        meta=_computer_ui_meta(),
    )

    if config.enable_lifecycle_tools:
        _register_computer_tools(mcp, config)
        _enable_mcp_apps_capability(mcp)

    return mcp

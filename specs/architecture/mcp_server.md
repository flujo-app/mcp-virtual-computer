# Architecture: MCP Server & Tool Layer

**Phase 4** of the architecture specification. Defines how the MCP server is structured: library choice, tool registration, request validation, response formatting, tool description assembly, and how the server delegates to the backend. Covers both stdio and Streamable HTTP transport wiring.

**References:** D8 (transports), D9 (tool name), D16 (tool description assembly), D17 (MCP library), D22 (response schema), D30 (stdin), D31 (no logging), Functional spec §2–§7. Phase 2 (backend abstraction), Phase 3 (Docker backend).

---

## 1. Library Evaluation

### 1.1 The MCP Python Ecosystem

The MCP Python ecosystem has two relevant libraries:

| Library | Package | Description |
|---|---|---|
| **Official MCP SDK** | `mcp` (PyPI) | From `modelcontextprotocol/python-sdk`. Includes a low-level server (`mcp.server.lowlevel.server.Server`) and a high-level convenience layer (`mcp.server.fastmcp.FastMCP`). Supports stdio, SSE, and Streamable HTTP. |
| **FastMCP (separate project)** | `fastmcp` (PyPI) | Third-party project at `gofastmcp.com`. Different codebase, different API. Adds features like providers, transforms, and composition. |

**These are different packages** despite the name overlap. The official `mcp` SDK includes its own `FastMCP` class at `mcp.server.fastmcp.FastMCP`.

### 1.2 Decision: Official `mcp` SDK with FastMCP Layer

**Use the official `mcp` package, specifically its built-in FastMCP convenience layer** (`mcp.server.fastmcp.FastMCP`). (D17)

**Why FastMCP over the low-level Server:**

- **Transport management** — `FastMCP.run(transport="stdio")` and `FastMCP.run(transport="streamable-http")` handle all transport wiring, including the `StreamableHTTPSessionManager` for HTTP sessions. The low-level server requires manual transport setup.
- **Lifespan support** — FastMCP's `lifespan` context manager maps directly to our sandbox lifecycle (per-session sandbox creation and cleanup).
- **Decorator/programmatic tool registration** — `mcp.add_tool()` supports `name`, `description`, and auto-generates input schema from function signatures.
- **Context injection** — Tool functions receive a `Context` object with access to the lifespan context (our sandbox) via type annotation.
- **`CallToolResult` passthrough** — Tool functions can return `CallToolResult` directly for full control over `isError` and response content.

**Why NOT the separate `fastmcp` package:**

- The separate project adds complexity (providers, transforms) we don't need.
- It's a different codebase with a different release cycle and different maintainers.
- The official SDK's FastMCP is maintained by the MCP team and is the canonical implementation.

### 1.3 SDK Version

**Target: `mcp` v1.x** (the current production-recommended version).

The SDK's `main` branch contains v2 (pre-alpha as of early 2026), with `MCPServer` replacing `FastMCP`. v2's stable release is anticipated in Q1 2026. When v2 stabilizes, migration should be mechanical — `MCPServer` has the same API patterns as `FastMCP` with renamed imports. v1.x will receive bug fixes for at least 6 months after v2 ships.

**Dependency specification:**

```toml
dependencies = [
    "mcp>=1.0,<2.0",
]
```

---

## 2. Server Architecture

### 2.1 Core Components

The MCP server layer lives in `src/kilntainers/server.py` and orchestrates:

1. **Tool registration** — A single `terminal_execute` tool with assembled description.
2. **Request validation** — Input validation before delegating to the backend.
3. **Response formatting** — Converting `ExecResult` to MCP tool response.
4. **Error mapping** — Translating backend exceptions to appropriate MCP error responses.
5. **Sandbox access** — Providing the per-session sandbox to the tool handler via lifespan context.

```
┌─────────────────────────────────────────┐
│  MCP Client (LLM / IDE / Agent)         │
└────────────────┬────────────────────────┘
                 │ tools/call "terminal_execute"
                 ▼
┌─────────────────────────────────────────┐
│  FastMCP (transport + protocol)         │
│  ├── stdio or Streamable HTTP           │
│  └── session management                 │
└────────────────┬────────────────────────┘
                 │ dispatches to tool handler
                 ▼
┌─────────────────────────────────────────┐
│  server.py: terminal_execute handler          │
│  ├── validate inputs                    │
│  ├── construct ExecRequest              │
│  ├── call sandbox.exec()                │
│  └── format response / handle errors    │
└────────────────┬────────────────────────┘
                 │ ExecRequest
                 ▼
┌─────────────────────────────────────────┐
│  Backend Abstraction (Phase 2)          │
│  └── DockerSandbox.exec() (Phase 3)    │
└─────────────────────────────────────────┘
```

### 2.2 Lifespan Context

The lifespan context manager is the bridge between the MCP server and the backend. It runs once per session — for stdio, that's the process lifetime; for HTTP, it's per `Mcp-Session-Id`.

**Sandbox creation is lazy.** The lifespan does not create a sandbox on entry. Instead, it yields a `SessionContext` that creates the sandbox on the first `terminal_execute` call. This allows the server to respond to `tools/list`, `initialize`, and other non-exec requests immediately.

```python
class SessionContext:
    """Per-session state, available to tool handlers via Context.

    Supports lazy sandbox creation via get_or_create_sandbox().
    See connection_lifecycle.md §8.1 for the complete design.
    """
    def __init__(self, backend, transport, death_callback=None): ...

    async def get_or_create_sandbox(self) -> Sandbox: ...
    async def cleanup(self) -> None: ...

@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[SessionContext]:
    ctx = SessionContext(backend=backend, transport=transport)
    try:
        yield ctx
    finally:
        await ctx.cleanup()
```

**How this maps to session lifecycles:**

| Transport | Lifespan scope | Effect |
|---|---|---|
| stdio | One per process | One sandbox for the server's lifetime, created on first `terminal_execute`. |
| Streamable HTTP | One per session | Each client session gets its own sandbox. Created on first `terminal_execute`, torn down on disconnect/idle timeout. |

The `backend` object is created once during startup (before `FastMCP` is instantiated) and shared across all sessions. It's safe for concurrent `create_sandbox()` calls (Phase 2 §8). The complete `SessionContext` design (lazy creation, concurrency safety, death monitoring) is defined in `connection_lifecycle.md` §8.1.

### 2.3 FastMCP Instance

```python
mcp = FastMCP(
    name="Kilntainers",
    lifespan=app_lifespan,
    host=config.host,           # for HTTP mode
    port=config.port,           # for HTTP mode
)
```

The FastMCP instance is created during startup after CLI parsing, backend validation, and tool description assembly (Phase 5 covers the full startup sequence). Host/port are only relevant for HTTP transport but are always configured — FastMCP ignores them in stdio mode.

---

## 3. Tool: `terminal_execute`

### 3.1 Registration

The tool is registered programmatically (not via decorator) because the description is assembled dynamically at startup from backend instructions and user overrides.

```python
mcp.add_tool(
    terminal_execute_handler,
    name="terminal_execute",
    description=assembled_description,
)
```

`assembled_description` is the result of tool description assembly (§4). `terminal_execute_handler` is the async function that handles tool calls (§3.3).

### 3.2 Input Schema

FastMCP auto-generates the JSON schema from the handler function's type annotations:

```python
async def terminal_execute_handler(
    command: str | None = None,
    args: list[str] | None = None,
    stdin: str | None = None,
    working_directory: str | None = None,
    timeout: int | None = None,
    ctx: Context[ServerSession, SessionContext] = ...,
) -> CallToolResult:
    ...
```

The `Context` parameter is excluded from the schema (FastMCP recognizes it and injects it automatically). The generated schema has all parameters as optional with correct types.

**Schema tradeoff:** The functional spec defines a `oneOf` constraint (exactly one of `command` or `args` required). FastMCP's auto-generated schema cannot express `oneOf` — all parameters appear as optional. This is an accepted tradeoff:

- The **tool description text** clearly explains the mutual exclusivity. LLMs read the description, not the JSON schema constraints.
- **Server-side validation** (§3.3) enforces the constraint and returns clear error messages.
- The `oneOf` constraint in JSON schema is defense-in-depth that few MCP clients enforce.
- Using FastMCP's auto-generation keeps the implementation simple and maintainable.

If precise schema control becomes important in the future, the tool can be registered on the low-level server (`mcp._mcp_server`) with a hand-crafted `inputSchema`. This is a localized change.

### 3.3 Handler Implementation

The handler validates inputs, lazily creates or retrieves the sandbox, constructs an `ExecRequest`, delegates to the sandbox, and formats the response.

```python
from mcp.server.fastmcp import Context
from mcp.server.session import ServerSession
from mcp.types import CallToolResult, TextContent

# Constants
STDIN_LIMIT = 2 * 1024 * 1024  # 2 MiB (D32)


async def terminal_execute_handler(
    command: str | None = None,
    args: list[str] | None = None,
    stdin: str | None = None,
    working_directory: str | None = None,
    timeout: int | None = None,
    ctx: Context[ServerSession, SessionContext] = ...,
) -> CallToolResult:
    """Handle a terminal_execute tool call."""

    # --- Input validation ---
    error = _validate_inputs(command, args, stdin, working_directory, timeout)
    if error is not None:
        return CallToolResult(
            content=[TextContent(type="text", text=error)],
            isError=True,
        )

    # --- Get or create sandbox (lazy creation) ---
    session_context = ctx.request_context.lifespan_context

    try:
        sandbox = await session_context.get_or_create_sandbox()
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
        timeout=timeout if timeout is not None else server_config.default_timeout,
        output_limit=server_config.output_limit,
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
    response_json = json.dumps({
        "stdout": result.stdout,
        "stderr": result.stderr,
        "exit_code": result.exit_code,
        "exec_duration_ms": result.exec_duration_ms,
    })

    return CallToolResult(
        content=[TextContent(type="text", text=response_json)],
        isError=False,
    )
```

**Lazy creation note:** The `get_or_create_sandbox()` call is the trigger for sandbox creation on the first `terminal_execute`. Subsequent calls return the existing sandbox immediately. The command timeout in `ExecRequest` applies only to `sandbox.exec()`, not to sandbox creation — creation has its own internal timeouts.

### 3.4 Input Validation

Validation runs before the ExecRequest is constructed. Errors are returned as `isError: true` MCP responses.

```python
def _validate_inputs(
    command: str | None,
    args: list[str] | None,
    stdin: str | None,
    working_directory: str | None,
    timeout: int | None,
) -> str | None:
    """Validate tool inputs. Returns error message or None if valid."""

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
```

**Validation ordering:** Cheap checks (mutual exclusivity, type constraints) run first. The stdin size check encodes to bytes, which is the most expensive validation — it runs last.

**Error messages are actionable:** Each error tells the caller what went wrong and what to do about it. These messages are read by LLMs, so clarity matters.

### 3.5 isError Mapping

The handler maps conditions to `isError` following the functional spec §2.5:

| Condition | `isError` | How |
|---|---|---|
| Command succeeds/fails | `false` | `sandbox.exec()` returns `ExecResult` |
| Timeout | `false` | `sandbox.exec()` returns `ExecResult` with exit_code 124 |
| Output limit exceeded | `false` | `sandbox.exec()` returns `ExecResult` with exit_code 1 |
| Invalid parameters | `true` | `_validate_inputs()` returns error message |
| Stdin too large | `true` | `_validate_inputs()` returns error message |
| Sandbox creation failed | `true` | `get_or_create_sandbox()` raises `BackendError` |
| Sandbox died | `true` | `sandbox.exec()` raises `SandboxDiedError` |

**Key insight:** The backend (Phase 3) produces `ExecResult` for all normal outcomes including timeout and output limit. These are wrapped in `CallToolResult(isError=False)`. Only infrastructure failures (validation errors, sandbox creation failure, sandbox death) produce `isError=True`. This keeps the handler logic simple — any ExecResult is a successful tool call.

### 3.6 Response Format

All successful tool calls (including timeout and output-limit conditions) return a JSON text block with the standard response schema:

```json
{
  "stdout": "hello world\n",
  "stderr": "",
  "exit_code": 0,
  "exec_duration_ms": 45
}
```

The JSON is serialized as a single `TextContent` block in the `CallToolResult`. The MCP client receives this as a text content block — the LLM sees the JSON directly and can parse the fields.

**Why JSON text, not structured output:** The MCP protocol's structured output feature (`structuredContent`) is a newer addition. Using a JSON text block is more widely compatible across MCP clients and keeps the implementation simple. The response schema is simple enough that LLMs parse it reliably as text.

---

## 4. Tool Description Assembly

Tool description assembly happens once at startup, before the FastMCP instance is created. The logic implements the rules from Functional spec §6.

```python
def assemble_tool_description(
    backend: Backend,
    override: str | None,
    extended: str | None,
) -> str:
    """Assemble the terminal_execute tool description.

    Raises BackendError if the result would be empty.
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
```

This function is called during startup and its result is passed to `mcp.add_tool()`. The description is static for the server's lifetime — it doesn't change between sessions or tool calls.

---

## 5. Transport Configuration

### 5.1 stdio

```python
mcp.run(transport="stdio")
```

FastMCP handles all stdio wiring internally:
- Creates `stdio_server()` context manager (reads from stdin, writes to stdout).
- Runs the low-level server's `run()` method with the stdio streams.
- One session for the process lifetime.
- The lifespan runs once, creating one sandbox.

**stdin/stdout ownership:** The MCP protocol uses stdin/stdout for message passing. Kilntainers' own output (startup errors, image pull progress) must go to **stderr** only (D31). The backend's image pull already outputs to stderr (Phase 3 §4.2).

### 5.2 Streamable HTTP

```python
mcp.run(transport="streamable-http")
```

FastMCP handles HTTP wiring internally:
- Creates a Starlette ASGI application.
- Sets up `StreamableHTTPSessionManager` for session lifecycle.
- Listens on `host:port` (from config).
- Each client session identified by `Mcp-Session-Id` header gets its own server `run()` call, with its own lifespan (and therefore its own sandbox).

**Session timeout:** FastMCP's `StreamableHTTPSessionManager` handles idle session cleanup. The `--session-timeout` config value needs to be passed through. FastMCP doesn't expose this directly via constructor — the integration point is covered in Phase 6 (Connection Lifecycle).

### 5.3 Transport Selection

The CLI `--transport` argument (`stdio` or `http`) determines which transport to use. This is a simple dispatch:

```python
if config.transport == "stdio":
    mcp.run(transport="stdio")
elif config.transport == "http":
    mcp.run(transport="streamable-http")
```

Phase 5 (CLI & configuration) covers argument validation, including rejecting HTTP-only parameters in stdio mode.

---

## 6. Request Flow

### 6.1 Full Request Lifecycle

**Tool call request (`tools/call`):**

```
1. MCP client sends tools/call { name: "terminal_execute", arguments: {...} }
2. MCP SDK deserializes, routes to call_tool handler
3. FastMCP resolves the tool, injects Context, calls terminal_execute_handler()
4. Handler validates inputs
   ├── Invalid → return CallToolResult(isError=true, message)
   └── Valid → continue
5. Handler calls get_or_create_sandbox() (lazy creation)
   ├── First call: validate backend → create sandbox → start death monitor
   ├── Subsequent calls: return existing sandbox immediately
   ├── BackendError → return CallToolResult(isError=true, message)
   └── Sandbox ready → continue
6. Handler constructs ExecRequest (resolves defaults from server config)
7. Handler calls sandbox.exec(request)
   ├── SandboxDiedError → return CallToolResult(isError=true, message)
   │                       (Phase 6 handles connection drop)
   └── ExecResult → continue
8. Handler serializes ExecResult to JSON
9. Handler returns CallToolResult(isError=false, content=[TextContent(json)])
10. MCP SDK serializes response and sends to client
```

**Tool list request (`tools/list`):**

```
1. MCP client sends tools/list
2. FastMCP returns the registered tool with name, description, and schema
3. MCP SDK serializes and sends to client
```

**Note:** `tools/list` does not trigger sandbox creation. It responds immediately regardless of sandbox state. This is the key benefit of lazy loading.

### 6.2 Default Resolution

The handler resolves default values before constructing the ExecRequest:

| Parameter | Source of default | How |
|---|---|---|
| `timeout` | `--timeout` CLI arg (default 120) | `timeout if timeout is not None else server_config.default_timeout` |
| `output_limit` | `--output-limit` CLI arg (default 2 MiB) | Always from config; not a per-call parameter |
| `working_directory` | Container's WORKDIR | `None` → backend uses container default |

The ExecRequest always has concrete `timeout` and `output_limit` values. The backend never needs to know about defaults.

### 6.3 Server Config Access

The tool handler needs access to server configuration (default timeout, output limit) and the per-session sandbox. These are accessed through different mechanisms:

| Data | Access mechanism | Scope |
|---|---|---|
| Sandbox | `ctx.request_context.lifespan_context.sandbox` | Per-session |
| Default timeout | Module-level or closure variable | Per-server (set at startup) |
| Output limit | Module-level or closure variable | Per-server (set at startup) |

Server config is set once at startup and doesn't change. The simplest approach is to bind it via closure when registering the tool handler, or store it as a module-level variable set during startup. Phase 5 covers the exact wiring.

---

## 7. Death Detection Integration

When a sandbox dies unexpectedly, two things must happen:
1. **During an exec call:** The handler catches `SandboxDiedError`, returns `isError: true`.
2. **Between exec calls:** The connection must be dropped.

**During exec** is handled in the tool handler (§3.3) — straightforward exception handling.

**Between exec** requires the death monitor background task started in the lifespan (§2.2). When `wait_for_death()` returns (sandbox died unexpectedly), the session must be terminated. The exact mechanism for session termination — cancelling the server's message loop, closing the transport, or signaling the session manager — is transport-dependent and covered in **Phase 6 (Connection & Session Lifecycle)**.

For Phase 4, the contract is:
- The lifespan starts the death monitor task.
- The lifespan cleans up (cancels death task, stops sandbox) on exit.
- The tool handler catches `SandboxDiedError` for in-flight exec.
- Phase 6 defines how death triggers session termination.

---

## 8. Startup Sequence

The full startup sequence, from process launch to accepting connections. Phase 5 covers argument parsing; this section covers the server-side steps.

```
1. Parse CLI arguments → config objects        (Phase 5)
2. Create backend with config (no validation)  (Phase 2, 3)
3. Assemble tool description                   (§4)
   └── backend.tool_instructions() + overrides. Fail if empty.
4. Create FastMCP instance                     (§2.3)
   └── With lifespan, host, port.
5. Register terminal_execute tool                  (§3.1)
   └── mcp.add_tool() with assembled description.
6. Run transport                               (§5)
   └── mcp.run(transport=...) — blocks until shutdown.
```

**No eager backend validation.** Backend prerequisites (Docker daemon reachable, etc.) are validated lazily on the first `terminal_execute` call, as part of `backend.create_sandbox()`. This allows the server to start and respond to `tools/list` immediately. See `connection_lifecycle.md` §8 for the lazy creation design.

Steps 2–3 happen before FastMCP is created. If any step fails, the process exits with a clear error message on stderr and a non-zero exit code.

Step 6 (`mcp.run()`) blocks — it runs the event loop and handles connections until the process is terminated. For stdio, it reads from stdin until EOF or SIGTERM. For HTTP, it listens on the configured port until SIGTERM.

---

## 9. Module Structure

All MCP server logic lives in `src/kilntainers/server.py`. This module exports:

| Component | Purpose |
|---|---|
| `create_server()` | Factory function: creates and configures the FastMCP instance with tool registered. Called by CLI startup. |
| `terminal_execute_handler()` | The tool handler function. |
| `assemble_tool_description()` | Tool description assembly logic. |
| `SessionContext` | Dataclass for per-session lifespan context. |
| `app_lifespan()` | Lifespan context manager. |

**`create_server()` sketch:**

```python
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

    # Create lifespan that captures the backend
    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[SessionContext]:
        sandbox = await backend.create_sandbox()
        death_task = asyncio.create_task(sandbox.wait_for_death())
        try:
            yield SessionContext(sandbox=sandbox, death_task=death_task)
        finally:
            death_task.cancel()
            try:
                await death_task
            except asyncio.CancelledError:
                pass
            await sandbox.stop()

    # Create server
    mcp = FastMCP(
        name="Kilntainers",
        lifespan=lifespan,
        host=config.host,
        port=config.port,
    )

    # Bind server config for the handler via closure
    handler = _create_handler(config)
    mcp.add_tool(
        handler,
        name="terminal_execute",
        description=description,
    )

    return mcp
```

The `_create_handler` function creates the `terminal_execute_handler` with server config bound via closure, avoiding global state.

---

## 10. Testing

### 10.1 Unit Tests (`tests/unit/test_server.py`)

Unit tests use the `MockBackend` and `MockSandbox` from Phase 2 §11 to test the server layer without Docker.

#### Tool Description Assembly

- Override provided → returns override, ignores backend and extended.
- Backend provides instructions → returns backend text.
- Backend provides instructions + extended → concatenated with `\n\n`.
- Backend returns None, no override → raises `BackendError` with helpful message.
- Both override and extended → raises `BackendError`.
- Backend returns empty string, no override → raises `BackendError`.

#### Input Validation

- Both `command` and `args` → error message.
- Neither `command` nor `args` → error message.
- `working_directory` is relative → error message.
- `timeout` < 1 → error message.
- `stdin` exceeds 2 MiB → error message.
- `stdin` at exactly 2 MiB → passes.
- Valid `command` only → passes.
- Valid `args` only → passes.
- All optional params populated → passes.

#### Tool Handler — Normal Responses

Using `MockSandbox` with preconfigured `ExecResult`s:

- Successful command → `CallToolResult` with `isError=False`, JSON body with exit_code 0.
- Failed command → `isError=False`, JSON body with non-zero exit_code.
- Timeout result → `isError=False`, JSON body with exit_code 124.
- Output limit result → `isError=False`, JSON body with exit_code 1.
- Response JSON contains all four fields (stdout, stderr, exit_code, exec_duration_ms).

#### Tool Handler — Error Responses

- Invalid inputs → `CallToolResult` with `isError=True`.
- `SandboxDiedError` → `isError=True` with descriptive message.

#### Tool Handler — ExecRequest Construction

Using `MockSandbox` to capture the `ExecRequest` passed to `exec()`:

- `command` mode → ExecRequest has `command`, `args` is None.
- `args` mode → ExecRequest has `args`, `command` is None.
- `timeout` provided → ExecRequest uses provided value.
- `timeout` not provided → ExecRequest uses server default.
- `output_limit` → always from server config.
- `stdin` → passed through.
- `working_directory` → passed through, or None if not provided.

#### Server Factory

- `create_server()` returns a FastMCP instance with one tool named "terminal_execute" by default. When `ServerConfig.enable_lifecycle_tools` is true, it also registers the six `computer_*` lifecycle tools and dashboard resource.
- Tool has the assembled description.

### 10.2 Integration with Backend Tests

The MCP server tests use mock backends exclusively. Testing the full flow (MCP → server → Docker backend → container) is covered by integration tests in Phase 3. The boundary is clean:

- **Server tests** verify request validation, response formatting, error mapping — backend is mocked.
- **Backend tests** verify Docker interaction, timeout/limit enforcement — MCP layer is not involved.
- **End-to-end tests** (future) could use the MCP client SDK to connect to a real server, but this is not required for v1.

### 10.3 Testing Approach

Server unit tests create a `FastMCP` instance with a mock backend and simulate tool calls programmatically:

```python
@pytest.fixture
async def server_with_mock():
    """Create a server with a mock backend."""
    mock_backend = MockBackend()
    config = ServerConfig(...)  # test defaults
    mcp = create_server(mock_backend, config)
    return mcp, mock_backend

async def test_terminal_execute_command_mode(server_with_mock):
    mcp, mock_backend = server_with_mock
    # Configure mock to return a specific ExecResult
    mock_backend.mock_sandbox.exec_results.append(
        ExecResult(stdout="hello\n", stderr="", exit_code=0, exec_duration_ms=10)
    )

    # Call the tool handler directly
    result = await mcp._tool_manager.call_tool(
        "terminal_execute",
        {"command": "echo hello"},
        context=mcp.get_context(),
    )
    # Assert response format and content
    ...
```

Alternatively, tests can call `terminal_execute_handler()` directly with a mock Context, which is simpler and more focused. The `_tool_manager` approach tests the full FastMCP dispatch path. Both approaches have value; the direct approach is preferred for validation and formatting tests, the manager approach for integration-level tests.

---

## 11. Implementation Notes

### 11.1 anyio vs asyncio

The MCP SDK uses **anyio** (an async abstraction that works with both asyncio and trio). FastMCP uses `anyio.run()` at the top level. Our backend code uses `asyncio` APIs (subprocess, locks, tasks).

This is compatible — anyio runs on top of asyncio by default. When anyio runs with the asyncio backend (the default), `asyncio.create_subprocess_exec`, `asyncio.Lock`, `asyncio.create_task`, etc. all work as expected.

The key rule: **use asyncio APIs in our code** (backend, server logic), and let the MCP SDK handle anyio at the transport level. Don't mix anyio and asyncio APIs for the same concern.

### 11.2 JSON Serialization

The tool response is serialized with `json.dumps()`. The ExecResult fields are all simple types (str, int), so no custom serialization is needed. The `json.dumps()` call does not use `indent` or sorting — compact JSON is preferred for tool responses (less token usage).

### 11.3 Error Message Quality

Error messages returned via `isError: true` are read by LLMs. They must be:
- **Actionable** — tell the agent what to do differently.
- **Specific** — include the actual value that was wrong.
- **Concise** — LLMs process shorter messages better.

Examples:
- *"Cannot provide both 'command' and 'args'. Use 'command' for shell commands or 'args' for direct execution."*
- *"working_directory must be an absolute path, got: relative/path"*
- *"stdin content exceeds the 2 MiB limit (2,500,000 bytes). Split into smaller chunks or use a different approach."*

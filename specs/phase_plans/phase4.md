# Phase 4: MCP Server & Tool Layer — Implementation Plan

Implement the MCP server using the official `mcp` SDK with FastMCP. Register the `terminal_execute` tool, handle requests, and format responses. stdio transport only (HTTP deferred to Phase 7).

**Architecture references:**
- [mcp_server.md](../architecture/mcp_server.md) §1–§9
- [connection_lifecycle.md](../architecture/connection_lifecycle.md) §8 (lifespan factory with stdio death propagation)
- [error_handling.md](../architecture/error_handling.md) §5 (runtime error propagation)

---

## Build Order

### Step 1: Add MCP SDK dependency

**File:** `pyproject.toml`

Add `mcp>=1.0,<2.0` to dependencies.

**Reference:** mcp_server.md §1.3

---

### Step 2: Core server structures

**File:** `src/kilntainers/server.py`

Create the foundational structures for the MCP server:

1. **`SessionContext` dataclass** — Per-session state (sandbox, death_task)
2. **`create_lifespan()` factory** — Creates lifespan context manager with:
   - Sandbox creation via `backend.create_sandbox()`
   - Death monitor task that triggers SIGTERM on sandbox death (stdio)
   - Cleanup: cancel death task, stop sandbox
3. **`assemble_tool_description()` function** — Implements tool description assembly rules:
   - Rule 1: Override replaces everything
   - Rule 2: Backend instructions, optionally extended
   - Rule 3: No backend instructions + no override → raises `BackendError`
   - Rule 4: Both override and extended → raises `BackendError`

**Reference:** mcp_server.md §2.2, §4; connection_lifecycle.md §8.2

---

### Step 3: Tool handler implementation

**File:** `src/kilntainers/server.py`

Create the tool handler and supporting functions:

1. **`_validate_inputs()` function** — Input validation:
   - Exactly one of `command` or `args` required
   - `working_directory` must be absolute
   - `timeout` must be ≥ 1
   - `stdin` ≤ 2 MiB
   - Returns error message or `None`

2. **`_create_handler()` function** — Creates handler with server config bound via closure

3. **`terminal_execute_handler()` async function** — The actual tool handler:
   - Validate inputs
   - Get sandbox from context
   - Construct `ExecRequest` (resolve defaults)
   - Call `sandbox.exec()`
   - Catch `SandboxDiedError`
   - Format response as JSON `CallToolResult`

**Constants:**
- `STDIN_LIMIT = 2 * 1024 * 1024` (2 MiB)

**Reference:** mcp_server.md §3.3, §3.4, §3.5

---

### Step 4: Server factory

**File:** `src/kilntainers/server.py`

Create the `create_server()` factory function:

1. Assemble tool description
2. Create lifespan via `create_lifespan()`
3. Create `FastMCP` instance with lifespan, host, port
4. Create handler via `_create_handler()`
5. Register tool with `mcp.add_tool()`
6. Return configured `FastMCP` instance

**Reference:** mcp_server.md §9

---

### Step 5: Unit tests

**File:** `src/kilntainers/test_server.py`

Create comprehensive unit tests using `MockBackend` and `MockSandbox`:

1. **Tool description assembly tests:**
   - Override provided → returns override
   - Backend instructions only → returns backend text
   - Backend + extended → concatenated with `\n\n`
   - No backend, no override → raises `BackendError`
   - Both override and extended → raises `BackendError`
   - Backend returns empty string → raises `BackendError`

2. **Input validation tests:**
   - Both `command` and `args` → error
   - Neither `command` nor `args` → error
   - Relative `working_directory` → error
   - `timeout` < 1 → error
   - `stdin` exceeds 2 MiB → error
   - `stdin` at exactly 2 MiB → passes
   - Valid inputs → passes

3. **Handler normal response tests:**
   - Successful command → `isError=False`, exit_code 0
   - Failed command → `isError=False`, non-zero exit_code
   - Timeout result → `isError=False`, exit_code 124
   - Output limit result → `isError=False`, exit_code 1
   - Response JSON contains all four fields

4. **Handler error response tests:**
   - Invalid inputs → `isError=True`
   - `SandboxDiedError` → `isError=True`
   - Unexpected exception → `isError=True`

5. **ExecRequest construction tests:**
   - `command` mode → ExecRequest has `command`, `args` is None
   - `args` mode → ExecRequest has `args`, `command` is None
   - `timeout` provided → uses provided value
   - `timeout` not provided → uses server default
   - `output_limit` → always from server config

6. **Lifespan tests:**
   - Creates sandbox on entry
   - Yields SessionContext with sandbox and death_task
   - Cancels death_task and calls sandbox.stop() on exit
   - Handles sandbox death (sends SIGTERM in stdio mode)

7. **Server factory tests:**
   - Returns configured `FastMCP` instance
   - Tool is registered with correct name and description
   - Lifespan captures backend and transport correctly

**Reference:** mcp_server.md §10

---

## Key Technical Specs

### Imports needed
```python
import asyncio
import json
import os
import signal
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.session import ServerSession
from mcp.types import CallToolResult, TextContent

from kilntainers.backends.base import Backend, ExecRequest, ExecResult, Sandbox
from kilntainers.config import ServerConfig
from kilntainers.errors import BackendError, SandboxDiedError
```

### SessionContext dataclass
```python
@dataclass
class SessionContext:
    """Per-session state, available to tool handlers via Context."""
    sandbox: Sandbox
    death_task: asyncio.Task[None]
```

### Key implementation notes

1. **Closure-based config binding:** Use `_create_handler(config)` to bind server config to the handler, avoiding global state

2. **Death propagation (stdio):** The lifespan's death monitor sends `os.kill(os.getpid(), signal.SIGTERM)` when sandbox dies

3. **Input validation ordering:** Cheap checks first, stdin size check last (requires encoding)

4. **Error messages:** Must be actionable and concise — read by LLMs

5. **JSON serialization:** Use `json.dumps()` without indent or sorting for compact output

---

## Testing Approach

- Use `MockBackend` and `MockSandbox` from `backends/test_utils.py`
- Tests call `terminal_execute_handler()` directly with mock `Context` for focused testing
- For integration-level tests, use `mcp._tool_manager.call_tool()` to test full dispatch
- Mock `os.kill` and `os.getpid` for death propagation tests to avoid actually sending signals
- Verify `CallToolResult` structure: `isError` boolean, `content` list with `TextContent`

---

## Success Criteria

1. `uv run pytest src/kilntainers/test_server.py` passes all tests
2. `uv run ./checks.sh` passes (format, lint, typecheck, all tests)
3. `create_server()` returns a `FastMCP` instance ready to run
4. Tool handler correctly validates, executes, and formats responses
5. Lifespan properly manages sandbox lifecycle and death detection

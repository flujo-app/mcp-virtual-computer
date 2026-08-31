# Architecture: Connection & Session Lifecycle

**Phase 6** of the architecture specification. Defines how stdio and Streamable HTTP transports map to sandbox lifecycles, session tracking for HTTP (creation, idle timeout, teardown), sandbox ownership, graceful shutdown orchestration, and sandbox death propagation to the MCP layer.

**References:** D8 (transports), D23 (sandbox death), D28 (multi-sandbox), D31 (no logging), Functional spec §4, §5.4, §9.2. Phase 2 (backend abstraction — `wait_for_death()`), Phase 3 (Docker backend — `stop()`, death detection), Phase 4 (MCP server — lifespan, `SessionContext`), Phase 5 (CLI — `--session-timeout`, `--transport`).

---

## 1. Overview

The connection lifecycle is the bridge between the MCP transport layer and the backend's sandbox lifecycle. It answers: when is a sandbox created, who owns it, how long does it live, and what happens when things go wrong?

Two transports, two models:

| Transport | Sandbox scope | Sessions | Creation trigger | Teardown trigger |
|---|---|---|---|---|
| **stdio** | Process lifetime | 1 (implicit) | Server startup | stdin closes / SIGTERM |
| **Streamable HTTP** | Per MCP session | Many concurrent | `initialize` request | Client close / idle timeout / SIGTERM |

Both models use the same underlying mechanism: FastMCP's **lifespan context manager** (Phase 4 §2.2). The lifespan creates a sandbox on entry and destroys it on exit. The difference is scope — stdio runs the lifespan once for the process; HTTP runs it once per session.

---

## 2. stdio Lifecycle

### 2.1 Full Flow

```
Process starts
  │
  ├── 1. Parse CLI args, validate config          (Phase 5)
  ├── 2. Create backend (no validation yet)       (Phase 5)
  ├── 3. Assemble tool description                (Phase 4)
  ├── 4. Create FastMCP server                    (Phase 4)
  │
  ├── 5. mcp.run(transport="stdio")               ← blocks
  │       │
  │       ├── Lifespan enters
  │       │     └── Yield SessionContext (no sandbox yet — lazy creation)
  │       │
  │       ├── Accept MCP messages (tools/list, tools/call)
  │       │     ├── tools/list → responds immediately (no sandbox needed)
  │       │     └── First tools/call terminal_execute:
  │       │           ├── get_or_create_sandbox()
  │       │           │     ├── backend.validate() (cached after first success)
  │       │           │     ├── Create sandbox (pull → run → readiness check)
  │       │           │     └── Start death monitor task
  │       │           └── sandbox.exec()
  │       │     └── Subsequent tools/call → sandbox.exec() (sandbox already exists)
  │       │
  │       └── Shutdown trigger (stdin EOF / SIGTERM / sandbox death)
  │             ├── Lifespan exits
  │             │     └── SessionContext.cleanup()
  │             │           ├── Cancel death monitor task (if started)
  │             │           └── sandbox.stop() (if sandbox was created)
  │             └── Return from mcp.run()
  │
  └── Process exits
```

### 2.2 One Sandbox, One Session

In stdio mode, there is exactly one sandbox for the entire server process. The lifespan enters when `mcp.run()` starts and exits when the transport shuts down. However, **sandbox creation is lazy** — the sandbox is not created until the first `terminal_execute` call.

This means:
- **`tools/list` responds immediately.** No sandbox creation is needed for non-exec requests. The server accepts MCP connections as soon as the lifespan enters.
- **Image pull blocks the first `terminal_execute`.** The first `terminal_execute` call creates the sandbox, which may include an image pull. Image pull progress goes to stderr, which the MCP client can display to the user.
- **No session ID.** The MCP stdio transport does not have session identifiers — there's one implicit session.
- **Sandbox creation failure = exec error, not process exit.** If the sandbox can't be created (image pull failure, Docker not running), the `terminal_execute` call returns an MCP error (`isError: true`). The session remains alive — subsequent `terminal_execute` calls will retry creation. Once a sandbox is created successfully, it is used for all future calls.
- **No sandbox if no exec.** If the session ends without any `terminal_execute` calls, no sandbox is ever created and no container resources are consumed.

### 2.3 Shutdown Triggers

| Trigger | Source | Mechanism |
|---|---|---|
| **stdin EOF** | Client disconnects (closes its stdout, which is our stdin) | FastMCP's stdio reader detects EOF, exits the message loop |
| **SIGTERM** | Process manager, MCP client, user | asyncio handles cancellation, triggers lifespan exit |
| **SIGINT** (Ctrl+C) | User terminal | Same as SIGTERM — asyncio's KeyboardInterrupt handling |
| **Sandbox death** | Container OOM, external kill, daemon crash | Death monitor task triggers process-level shutdown (§6) |

All triggers converge on the same cleanup path: lifespan exit → cancel death task → `sandbox.stop()`.

---

## 3. Streamable HTTP Lifecycle

### 3.1 Server vs Session

Streamable HTTP has two levels of lifecycle:

1. **Server lifecycle** — the HTTP server process. Starts on `mcp.run()`, listens for connections, stops on SIGTERM.
2. **Session lifecycle** — one per connected MCP client. Created on `initialize`, destroyed on close/timeout/death.

```
Server starts
  │
  ├── Parse CLI args, validate config, create backend
  ├── Create FastMCP server
  ├── mcp.run(transport="streamable-http")
  │     │
  │     ├── Start HTTP server on host:port
  │     │
  │     ├── Session A: initialize → lifespan A enters → sandbox A created
  │     │     ├── tools/call → sandbox A.exec()
  │     │     ├── tools/call → sandbox A.exec()
  │     │     └── close/timeout → lifespan A exits → sandbox A.stop()
  │     │
  │     ├── Session B: initialize → lifespan B enters → sandbox B created
  │     │     ├── tools/call → sandbox B.exec()
  │     │     └── sandbox B dies → death propagation → session B terminated
  │     │
  │     └── SIGTERM → all sessions torn down → server exits
  │
  └── Process exits
```

### 3.2 Session Creation

When an MCP client sends an `initialize` request over HTTP, the SDK's `StreamableHTTPSessionManager` creates a new session:

1. A new `Mcp-Session-Id` is generated and returned in the response headers.
2. A new lifespan context is entered for this session.
3. The lifespan yields a `SessionContext` with lazy sandbox creation (no sandbox created yet).
4. The session is ready to accept tool calls.

**`initialize` completes immediately.** No sandbox is created during session initialization. The client receives the session ID without waiting for container startup.

**Sandbox creation happens on first `terminal_execute`.** When the first `terminal_execute` tool call arrives for the session, `SessionContext.get_or_create_sandbox()` creates the sandbox (validate backend, pull image, start container, readiness check). The sandbox is then used for all subsequent `terminal_execute` calls in the session.

**Concurrent session creation.** Multiple clients can connect simultaneously. Each session has its own lazy `SessionContext`. Sandbox creation within each session is concurrency-safe — if multiple `terminal_execute` calls arrive simultaneously, only one sandbox is created per session.

### 3.3 Session Identification

Sessions are identified by the `Mcp-Session-Id` HTTP header, as defined by the MCP Streamable HTTP transport specification. The SDK handles session routing — incoming requests are dispatched to the correct session's server instance based on this header.

Each session owns exactly one sandbox. The mapping is:

```
Mcp-Session-Id  →  Lifespan Context  →  SessionContext  →  Sandbox
```

There is no sharing of sandboxes between sessions. Each session is fully independent — different sandbox, different state, different lifecycle.

### 3.4 Request Routing

For each incoming HTTP request:
1. The SDK extracts the `Mcp-Session-Id` header.
2. The SDK looks up the session in the `StreamableHTTPSessionManager`.
3. If the session exists: the request is dispatched to that session's server instance.
4. If the session doesn't exist (expired, terminated, never created): the SDK returns an HTTP error.
5. The tool handler receives the per-session `SessionContext` (including the sandbox) via `ctx.request_context.lifespan_context`.

This routing is handled entirely by the SDK. The Kilntainers code only interacts with the sandbox through the `SessionContext`.

---

## 4. Session Timeout (HTTP Idle Timeout)

### 4.1 Behavior

When no requests are received for a session within `--session-timeout` seconds (default: 300, i.e. 5 minutes), the session is terminated:

1. The `StreamableHTTPSessionManager` detects the idle session.
2. The session's lifespan context exits.
3. The death monitor task is cancelled.
4. `sandbox.stop()` is called — the container is stopped and removed.
5. Subsequent requests with this session's `Mcp-Session-Id` receive an error (session not found).

The client can create a new session by sending a new `initialize` request, which creates a fresh sandbox.

### 4.2 SDK Integration

The `StreamableHTTPSessionManager` in the MCP SDK manages session idle detection. Kilntainers needs to pass the configured `--session-timeout` value to this component.

**Integration approach:** FastMCP's internal creation of the `StreamableHTTPSessionManager` may not directly expose a session timeout parameter through its constructor. The integration requires one of these approaches, determined during implementation:

1. **FastMCP constructor parameter** — If FastMCP accepts a session timeout or similar parameter, pass `config.session_timeout` directly. This is the simplest approach and should be checked first.

2. **Subclass or configure post-creation** — Create the FastMCP instance and then configure the session manager's timeout before calling `run()`. This requires accessing FastMCP's internal session manager.

3. **Lower-level transport setup** — Instead of `mcp.run(transport="streamable-http")`, construct the Starlette ASGI app and `StreamableHTTPSessionManager` manually with the desired timeout, then run the ASGI app with uvicorn. This gives full control but is more code.

```python
# Approach 1 (preferred, if supported):
mcp = FastMCP(
    name="Kilntainers",
    lifespan=lifespan,
    host=config.host,
    port=config.port,
    session_timeout=timedelta(seconds=config.session_timeout),  # if available
)

# Approach 3 (fallback — manual transport setup):
from mcp.server.streamable_http import StreamableHTTPSessionManager

session_manager = StreamableHTTPSessionManager(
    app=mcp._mcp_server,  # low-level server
    event_store_class=...,
    session_timeout=timedelta(seconds=config.session_timeout),
)
# Build Starlette app with session_manager and run via uvicorn
```

**Recommendation:** Start with approach 1. If FastMCP doesn't expose the parameter, proceed to approach 3. The manual approach is more code but gives precise control over session timeout behavior, which is a core feature.

### 4.3 Timeout Scope

The idle timeout tracks time since the **last request completed** for a session. Active requests (e.g., a long-running exec) keep the session alive. The timeout only fires when the session has been completely idle.

**Interaction with exec timeout:** A command running for 300 seconds (at the exec timeout) does not trigger the idle session timeout. The session is active while processing a request. The idle timeout starts counting after the response is sent.

---

## 5. Sandbox Ownership Model

### 5.1 One Sandbox per Session

Every session owns exactly one sandbox. This is a strict 1:1 mapping — no pooling, no sharing, no reuse.

```
Session             Sandbox             Container
─────────           ───────             ─────────
stdio session  ──→  sandbox A  ──→     container A
HTTP session 1 ──→  sandbox B  ──→     container B
HTTP session 2 ──→  sandbox C  ──→     container C
```

**Why no pooling:** Sandboxes are stateful (files created, packages installed) and security-sensitive. Sharing a sandbox between sessions would leak state and violate isolation. Creating a sandbox per session provides clean isolation and simple lifecycle management.

### 5.2 Sandbox Lifetime

A sandbox's lifetime is bounded by its session's lifespan context, but creation is lazy:

- **Created:** On the first `terminal_execute` call within the session (lazy, not on session initialization).
- **Alive:** From creation until the session ends. Accepts exec calls.
- **Destroyed:** When the lifespan context exits (session teardown), via `SessionContext.cleanup()` → `sandbox.stop()`. If no sandbox was ever created, cleanup is a no-op.

A session can exist without a sandbox (before the first `terminal_execute` or if no `terminal_execute` is ever called). Once a sandbox is created, it belongs exclusively to that session.

### 5.3 Backend Lifetime

The `Backend` object (e.g., `DockerBackend`) is created once during startup and lives for the server process lifetime. It is shared across all sessions — its only per-session interaction is `create_sandbox()`, which is safe for concurrent calls (Phase 2 §8).

```
Process lifetime:   ├──────── Backend ─────────────────────────┤
Session A:               ├── Sandbox A ──┤
Session B:          ├─────── Sandbox B ──────────────┤
Session C:                      ├── Sandbox C ──────────┤
```

---

## 6. Sandbox Death Propagation

When a sandbox dies unexpectedly (OOM, external kill, Docker daemon crash), the MCP layer must terminate the session. The behavior is defined in Functional spec §4.5; this section defines the architecture of how death is detected and propagated.

### 6.1 Detection Mechanism

Death detection uses the background task started in the lifespan (Phase 4 §2.2):

```python
death_task = asyncio.create_task(sandbox.wait_for_death())
```

`wait_for_death()` blocks until the sandbox dies unexpectedly. For Docker, this wraps `docker wait` (Phase 3 §5.7). When the sandbox dies, the task completes (returns normally, not via exception).

### 6.2 Two Death Scenarios

| Scenario | Detection | Handler |
|---|---|---|
| **During exec** | `sandbox.exec()` raises `SandboxDiedError` | Tool handler catches it, returns `isError: true` |
| **Between exec calls** | `death_task` completes | Death propagation logic terminates the session |

The "during exec" path is handled by the tool handler (Phase 4 §3.3). The "between exec calls" path requires the death propagation mechanism described below.

### 6.3 Death Propagation: stdio

When the death_task completes in stdio mode, the entire process must shut down. The death task is started by `SessionContext._start_death_monitor()` when the sandbox is lazily created (see §8.1). If no sandbox is ever created, there is no death task and no death propagation.

The death monitor within `SessionContext._start_death_monitor()` sends SIGTERM to the current process:

```python
def _start_death_monitor(self, sandbox: Sandbox) -> None:
    async def _monitor_death() -> None:
        try:
            await sandbox.wait_for_death()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        if self._transport == "stdio":
            if self._death_callback is not None:
                self._death_callback()
            else:
                os.kill(os.getpid(), signal.SIGTERM)

    self._death_task = asyncio.create_task(_monitor_death())
```

**Why SIGTERM:** Sending SIGTERM to the process itself reuses the existing graceful shutdown path — asyncio cancels all tasks, the lifespan's `finally` block runs, `SessionContext.cleanup()` is called. This avoids duplicating shutdown logic or fighting with the SDK's event loop. It's the same signal that an MCP client sends when it wants the server to stop.

**Race condition:** If SIGTERM arrives while an exec is in-flight, the exec is cancelled (asyncio cancellation). The in-flight exec does not return a result — the client is about to lose the connection anyway. This matches the functional spec: "In-flight exec is killed immediately" (§4.4).

### 6.4 Death Propagation: HTTP

For HTTP sessions, sandbox death should terminate only the affected session, not the entire server. The mechanism depends on the SDK's session management capabilities.

**Approach: Session-scoped death handling**

The death task runs within the session's lifespan scope. When it fires, it needs to terminate that specific session. The approach varies based on SDK capabilities:

**Option A — Request-time detection (guaranteed to work):**

The simplest approach, and the baseline for v1: the death is detected at the next tool call. The `sandbox.exec()` call raises `SandboxDiedError`, and the handler returns `isError: true`. The session is effectively dead — all future tool calls will fail with the same error.

This does not proactively notify the client that the sandbox died. The client discovers the death on its next request. For MCP usage patterns (LLM making sequential tool calls), this delay is usually negligible — the next request comes shortly after.

**Option B — Proactive session termination (preferred if SDK supports it):**

If the SDK exposes a mechanism to close a session programmatically (e.g., closing the session's SSE stream, or removing the session from the `StreamableHTTPSessionManager`), the death task uses it:

```python
async def _on_death():
    await sandbox.wait_for_death()
    # Proactively terminate the session
    await session_manager.close_session(session_id)
```

This immediately notifies clients with open SSE connections (used by Streamable HTTP for server-initiated messages). Clients without open connections discover the termination on their next request.

**Recommendation for v1:** Implement Option A (request-time detection) as the baseline. It is simple, requires no SDK-internal access, and is functionally correct. Add Option B as an enhancement if the SDK provides session termination APIs. Both approaches satisfy the functional spec — the connection is dropped (either proactively or on next request), and the client gets a fresh sandbox by creating a new session.

### 6.5 Post-Death Behavior

After sandbox death is detected (by either path):

1. **The sandbox is marked as dead.** All subsequent `sandbox.exec()` calls raise `SandboxDiedError`. No recovery is attempted (D6).
2. **The session is terminated.** For stdio, the process exits. For HTTP, the session becomes invalid.
3. **Cleanup runs.** The lifespan's `finally` block calls `sandbox.stop()`, which is idempotent — if the sandbox is already dead, `stop()` is a best-effort no-op (Phase 3 §5.6).
4. **The client can reconnect.** For stdio, most MCP clients auto-restart the server. For HTTP, the client sends a new `initialize` request to get a new session and sandbox.

---

## 7. Graceful Shutdown

### 7.1 Shutdown Triggers

| Trigger | Transport | Scope |
|---|---|---|
| stdin EOF | stdio | Process |
| SIGTERM / SIGINT | Both | Process (all sessions for HTTP) |
| Client closes session | HTTP | Single session |
| Idle timeout | HTTP | Single session |
| Sandbox death | Both | Single session (process for stdio) |

### 7.2 Session Shutdown Sequence

When a single session ends (any trigger), the lifespan's `finally` block executes `SessionContext.cleanup()`:

```
Session shutdown triggered
  │
  ├── SessionContext.cleanup()
  │     │
  │     ├── 1. Cancel death monitor task (if sandbox was created)
  │     │     └── death_task.cancel() → CancelledError → docker wait subprocess killed
  │     │
  │     ├── 2. Stop sandbox (if sandbox was created)
  │     │     ├── sandbox.stop()
  │     │     │     ├── Set _stop_requested flag
  │     │     │     ├── docker stop -t 5 <container_id>
  │     │     │     │     └── SIGTERM → 5s grace → SIGKILL
  │     │     │     └── Container removed (--rm flag)
  │     │     │
  │     │     └── If stop takes >10s → docker stop subprocess killed
  │     │           └── Container may be orphaned (labeled for manual cleanup)
  │     │
  │     └── (If no sandbox was ever created, cleanup is a no-op)
  │
  └── 3. Session context released
        └── SDK cleans up session state
```

**In-flight exec during shutdown:** If an exec call is running when shutdown is triggered, the asyncio task running `sandbox.exec()` is cancelled. This cancels the `docker exec` subprocess. The client does not receive a response — the connection is being torn down. This is expected and matches the functional spec: "In-flight exec is killed immediately. The client is disconnecting — no one will receive the result." (§4.4)

### 7.3 Server Shutdown (HTTP)

When the HTTP server receives SIGTERM, all active sessions are torn down:

```
SIGTERM received
  │
  ├── 1. Stop accepting new connections
  │     └── HTTP server stops listening
  │
  ├── 2. Tear down all active sessions (concurrent)
  │     ├── Session A: cancel death task → stop sandbox A
  │     ├── Session B: cancel death task → stop sandbox B
  │     └── Session C: cancel death task → stop sandbox C
  │
  ├── 3. Wait for cleanup (up to 10s per sandbox)
  │
  └── 4. Process exits
```

The SDK's HTTP server (Starlette/uvicorn) handles the signal and initiates graceful shutdown. Each session's lifespan exits, triggering the cleanup sequence. Sessions are torn down concurrently — there's no ordering dependency between independent sessions.

### 7.4 Force-Kill Timeout

The functional spec requires that cleanup completes within 10 seconds (§4.4). This is enforced at two levels:

1. **Docker stop timeout:** `docker stop -t 5` gives the in-container process 5 seconds to handle SIGTERM before Docker sends SIGKILL. This is set in the `stop()` method (Phase 3 §5.6).

2. **Stop subprocess timeout:** An outer `asyncio.wait_for(proc.wait(), timeout=10)` in `stop()` ensures that even if Docker itself stalls (daemon issue, I/O hang), we kill the stop subprocess after 10 seconds and proceed.

If both timeouts fire (Docker daemon unresponsive), the container may be left running as an orphan. It is labeled `kilntainers=true` for manual identification (Phase 3 §6.6).

---

## 8. Lifespan Context: Complete Design

The lifespan context manager was introduced in Phase 4 §2.2. This section provides the complete design incorporating lazy sandbox creation, death propagation, shutdown handling, and transport-aware behavior.

### 8.1 SessionContext

`SessionContext` owns the lazy sandbox lifecycle. The sandbox is not created until the first `terminal_execute` call triggers `get_or_create_sandbox()`.

```python
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
    ) -> None:
        self._backend = backend
        self._transport = transport
        self._death_callback = death_callback
        self._sandbox: Sandbox | None = None
        self._death_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def sandbox(self) -> Sandbox | None:
        """The sandbox, or None if not yet created. Read-only."""
        return self._sandbox

    @property
    def death_task(self) -> asyncio.Task[None] | None:
        """The death monitor task, or None if sandbox not yet created."""
        return self._death_task

    async def get_or_create_sandbox(self) -> Sandbox:
        """Get the sandbox, creating it lazily on first call.

        Concurrency-safe: uses asyncio.Lock to ensure only one sandbox
        is created even if multiple calls arrive simultaneously.

        Returns the sandbox. Raises BackendError if creation fails.
        On failure, subsequent calls will retry creation.
        """
        if self._sandbox is not None:
            return self._sandbox
        async with self._lock:
            if self._sandbox is not None:
                return self._sandbox
            sandbox = await self._backend.create_sandbox()
            self._start_death_monitor(sandbox)
            self._sandbox = sandbox
            return sandbox

    def _start_death_monitor(self, sandbox: Sandbox) -> None:
        """Start monitoring sandbox for unexpected death."""
        async def _monitor_death() -> None:
            try:
                await sandbox.wait_for_death()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if self._transport == "stdio":
                if self._death_callback is not None:
                    self._death_callback()
                else:
                    os.kill(os.getpid(), signal.SIGTERM)

        self._death_task = asyncio.create_task(_monitor_death())

    async def cleanup(self) -> None:
        """Clean up resources. Called by lifespan on exit.

        Safe to call even if no sandbox was ever created (no-op).
        """
        if self._death_task is not None:
            self._death_task.cancel()
            try:
                await self._death_task
            except asyncio.CancelledError:
                pass
        if self._sandbox is not None:
            await self._sandbox.stop()
```

**Key design properties:**

- **Lazy creation**: `sandbox` and `death_task` are `None` until `get_or_create_sandbox()` is called.
- **Double-checked locking**: Fast path without lock (`if self._sandbox is not None`), then lock + re-check for the slow path.
- **Retry-safe**: If `create_sandbox()` raises, `self._sandbox` stays `None`, so the next call retries.
- **Clean cleanup**: `cleanup()` is a no-op if no sandbox was ever created.

### 8.2 Lifespan Factory

The lifespan is now trivial — it creates a `SessionContext` and cleans up on exit. Sandbox creation is deferred to the first `terminal_execute` call.

```python
def create_lifespan(
    backend: Backend,
    transport: str,
    *,
    death_callback: Callable[[], None] | None = None,
) -> Callable[[FastMCP], AsyncContextManager[SessionContext]]:
    """Create a lifespan context manager for the given transport.

    The returned context manager sets up a SessionContext per session.
    Sandbox creation is lazy — it happens on the first terminal_execute
    call, not when the lifespan enters.
    """

    @asynccontextmanager
    async def lifespan(server: FastMCP) -> AsyncIterator[SessionContext]:
        ctx = SessionContext(
            backend=backend,
            transport=transport,
            death_callback=death_callback,
        )
        try:
            yield ctx
        finally:
            await ctx.cleanup()

    return lifespan
```

### 8.3 Integration with create_server

The lifespan factory integrates with `create_server()` from Phase 4 §9. Unchanged from Phase 4 — the only difference is that `create_lifespan` now returns a trivial context manager:

```python
def create_server(
    backend: Backend,
    config: ServerConfig,
) -> FastMCP:
    description = assemble_tool_description(
        backend,
        override=config.tool_instruction_override,
        extended=config.extended_tool_instruction,
    )

    lifespan = create_lifespan(backend, config.transport)

    mcp = FastMCP(
        name="Kilntainers",
        lifespan=lifespan,
        host=config.host,
        port=config.port,
    )

    handler = _create_handler(config)
    mcp.add_tool(handler, name="terminal_execute", description=description)

    return mcp
```

---

## 9. Edge Cases

### 9.1 Sandbox Creation Failure

Since sandbox creation is lazy (happens on first `terminal_execute`, not during session initialization), creation failures are handled differently than if they occurred at session init:

If `backend.create_sandbox()` raises `BackendError` during `get_or_create_sandbox()`:

- **The `terminal_execute` call returns `isError: true`** with the BackendError message. The session remains alive.
- **Subsequent `terminal_execute` calls retry creation.** The `SessionContext` allows retry — `_sandbox` stays `None` on failure, so the next call enters the creation path again.
- **Once a sandbox is successfully created, it is used for all future calls.** No second sandbox is ever created within a session.

This covers: Docker daemon down, image pull failure, readiness check failure, resource exhaustion. Transient failures (e.g., network hiccup during image pull) are automatically retried on the next `terminal_execute` call.

### 9.2 Concurrent Death and Exec

If the sandbox dies while an exec is in progress:

1. The `docker exec` subprocess exits abnormally (the container is gone).
2. `_do_exec` detects the non-zero exit code and checks `_is_container_running()` (Phase 3 §7).
3. The container is not running → raises `SandboxDiedError`.
4. The tool handler catches it and returns `isError: true`.
5. Concurrently, the `death_task` fires and triggers shutdown (§6.3/§6.4).

Both paths converge on the same outcome: error to the client, session terminated. The `finally` block in the lifespan ensures cleanup runs exactly once regardless of which path triggers first.

### 9.3 Rapid Reconnection (HTTP)

If a client's session is terminated (sandbox death or idle timeout) and it immediately sends a new `initialize`:

1. The old session is being cleaned up (sandbox stopping).
2. The new session creates a new, independent sandbox.
3. Both proceed concurrently — the old sandbox is being torn down while the new one is being created.

This works because `backend.create_sandbox()` creates independent containers. There's no shared state between the old and new sandboxes.

### 9.4 Server SIGTERM During Sandbox Creation

If SIGTERM arrives while a sandbox is being created (image pull, container start, readiness check):

- The async task running `create_sandbox()` is cancelled.
- If a container was created but the readiness check was interrupted, the container may not be explicitly stopped.
- However, `--rm` means the container will be cleaned up when it stops. If it's still running (no process managing it), it becomes an orphan — labeled for manual cleanup.
- This is an acceptable edge case for v1.

### 9.5 Multiple Sandbox Deaths (HTTP)

If the Docker daemon crashes, all sandboxes die simultaneously:

- Each session's death_task fires independently.
- Each session's lifespan exits and attempts cleanup.
- `sandbox.stop()` calls will fail (Docker daemon is down) — errors are swallowed (best-effort stop).
- All sessions become invalid.
- Clients can retry once the daemon is back.

---

## 10. Module Changes

This phase does not introduce new modules. It refines the existing modules defined in earlier phases:

### 10.1 `server.py` Changes

- **`SessionContext`** — Refactored from a simple dataclass to a class with lazy sandbox creation, concurrency-safe `get_or_create_sandbox()`, death monitor management, and `cleanup()`. See §8.1.
- **`create_lifespan()`** — Simplified. No longer creates a sandbox or death task directly — delegates to `SessionContext`. See §8.2.
- **`terminal_execute_handler()`** — Uses `await session_context.get_or_create_sandbox()` instead of direct `sandbox` attribute access. Catches `BackendError` from lazy creation.

### 10.2 `cli.py` Changes

- **Remove eager backend validation** — The `_validate_backend()` call in `main()` is removed. Backend validation happens lazily inside `create_sandbox()` on first `terminal_execute`.
- **Session timeout passthrough** — The startup flow must pass `config.session_timeout` to the FastMCP/session manager configuration. The exact mechanism depends on the SDK integration approach (§4.2).

### 10.3 Imports

```python
import os
import signal
```

Required for the SIGTERM-based death propagation in stdio mode (§6.3). These are used within `SessionContext._start_death_monitor()`.

---

## 11. Testing

### 11.1 Unit Tests (`tests/unit/test_lifecycle.py`)

Lifecycle tests use `MockBackend` and `MockSandbox` from Phase 2 §11. No Docker required.

#### Lifespan — Normal Flow

- Lifespan creates a sandbox via `backend.create_sandbox()`.
- Lifespan yields a `SessionContext` with sandbox and death_task.
- On exit, death_task is cancelled and `sandbox.stop()` is called.
- Sandbox stop is called even if the body raises an exception.

#### Lifespan — Sandbox Creation Failure

- `backend.create_sandbox()` raises `BackendError` → lifespan propagates the exception.
- No death_task is created. No cleanup needed.

#### Death Propagation — stdio

- Simulate sandbox death (`mock_sandbox.simulate_death()`).
- Verify that `os.kill(os.getpid(), signal.SIGTERM)` is called (mock `os.kill`).
- Verify the signal is sent to the current process (not a different PID).

#### Death Propagation — HTTP

- Simulate sandbox death.
- Verify the death_task completes (no exception).
- Verify that subsequent `sandbox.exec()` calls raise `SandboxDiedError`.

#### Session Teardown Ordering

- Verify that death_task is cancelled **before** `sandbox.stop()` is called.
- This ensures `wait_for_death()` doesn't trigger death propagation during normal shutdown.

#### Concurrent Death and Shutdown

- Start a death simulation and immediately trigger lifespan exit.
- Verify cleanup completes without deadlock or double-stop errors.
- `sandbox.stop()` is idempotent, so this should be safe.

### 11.2 Unit Tests (`tests/unit/test_server.py` — additions)

#### create_lifespan Integration

- `create_lifespan(backend, "stdio")` returns a valid async context manager.
- `create_lifespan(backend, "http")` returns a valid async context manager.
- Both create a sandbox and yield a SessionContext.

#### create_server with Lifespan

- `create_server()` creates a FastMCP instance with the lifespan configured.
- The lifespan captures the backend and transport correctly.

### 11.3 Integration Tests (`tests/integration/test_lifecycle_integration.py`)

These tests require Docker and validate the full lifecycle with real containers.

#### stdio Lifecycle

- Create backend → create server → simulate a full stdio session (start, exec, stop).
- Verify the container exists while the session is active.
- Verify the container is removed after the session ends.

#### Sandbox Death — Container Killed

- Create a sandbox, kill the container externally (`docker kill`).
- Verify `wait_for_death()` resolves.
- Verify subsequent `exec()` calls raise `SandboxDiedError`.

#### Graceful Stop

- Create a sandbox, execute a command, stop the sandbox.
- Verify the container is removed (`docker ps` no longer lists it).
- Verify `stop()` completes within 10 seconds.

#### Idle Session Timeout (HTTP)

- This is an end-to-end test of the session timeout integration.
- Create an HTTP server with a short `--session-timeout` (e.g., 5 seconds).
- Connect a session, execute a command, then wait for the timeout.
- Verify the sandbox is stopped after the timeout.
- Verify a new `initialize` request creates a new sandbox.
- **Note:** This test depends on the SDK integration approach (§4.2) and may be deferred if the integration is not straightforward.

### 11.4 Testing Approach

Lifecycle tests are primarily about ordering and cleanup guarantees. The key testing patterns:

1. **Mock `os.kill`** for stdio death propagation tests to avoid actually sending signals.
2. **Use `MockSandbox.simulate_death()`** to trigger death without Docker.
3. **Verify call ordering** — death_task cancelled before stop, stop always called in finally.
4. **Verify idempotency** — double-stop, stop after death, cleanup after cancelled creation.

```python
@pytest.fixture
def mock_backend():
    return MockBackend()

async def test_lifespan_creates_and_stops_sandbox(mock_backend):
    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        assert ctx.sandbox is not None
        assert ctx.sandbox.sandbox_id == "mock-sandbox-001"

    assert mock_backend.mock_sandbox._stopped is True

async def test_death_triggers_sigterm_stdio(mock_backend, monkeypatch):
    kill_calls = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: kill_calls.append((pid, sig)))

    lifespan_fn = create_lifespan(mock_backend, "stdio")
    mock_server = MagicMock()

    async with lifespan_fn(mock_server) as ctx:
        ctx.sandbox.simulate_death()
        await asyncio.sleep(0.1)  # let death task run

    assert len(kill_calls) == 1
    assert kill_calls[0] == (os.getpid(), signal.SIGTERM)
```

---

## 12. Implementation Notes

### 12.1 SIGTERM Self-Signal Behavior

When `os.kill(os.getpid(), signal.SIGTERM)` is called from within an asyncio task:

1. The signal is delivered to the process.
2. Python's default SIGTERM handler raises `SystemExit` (in the main thread).
3. asyncio's shutdown sequence cancels all tasks.
4. The lifespan's `finally` block runs.

This is the same path as an external SIGTERM. The self-signal approach is intentional — it reuses the SDK's and asyncio's existing shutdown machinery rather than implementing a custom shutdown flow.

**Note:** In some asyncio configurations, signal handling may differ. If issues arise during implementation, an alternative is to cancel the main server task directly (requires a reference to it, which may need to be stored in `SessionContext`).

### 12.2 Death Task Exception Handling

The `_monitor_death()` function must not raise exceptions (other than `CancelledError`). If `wait_for_death()` raises unexpectedly, the error should be logged to stderr and treated as a death event:

```python
async def _monitor_death() -> None:
    try:
        await sandbox.wait_for_death()
    except asyncio.CancelledError:
        raise  # Normal shutdown — propagate cancellation
    except Exception:
        # Unexpected error monitoring sandbox — treat as death
        print(
            f"kilntainers: error monitoring sandbox: {e}",
            file=sys.stderr,
        )
    # If we reach here, sandbox died (or monitoring failed)
    if transport == "stdio":
        os.kill(os.getpid(), signal.SIGTERM)
```

### 12.3 anyio Compatibility

The lifespan uses `asyncio` APIs (`asyncio.create_task`, `asyncio.CancelledError`). As noted in Phase 4 §11.1, this is compatible with the MCP SDK's anyio usage because anyio runs on top of asyncio by default. The `os.kill` and `signal` APIs are synchronous stdlib calls and have no async framework concerns.

### 12.4 HTTP Session Timeout — Implementation Priority

The `--session-timeout` integration is a "must have" for HTTP mode. Without it, idle sessions leak sandboxes (containers) indefinitely. The implementation should:

1. First check if FastMCP exposes a session timeout parameter.
2. If not, implement the manual transport setup (§4.2 approach 3).
3. If the manual setup is complex, add a temporary workaround: a background task per session that checks idle time and calls `sandbox.stop()` after the timeout. This is less clean but functional.

The key constraint: idle sessions **must** be cleaned up. Leaking containers is not acceptable, even in v1.

### 12.5 Streamable HTTP Connection Semantics

Streamable HTTP is not a persistent connection like WebSocket. The protocol uses standard HTTP requests with an optional SSE stream for server-initiated messages. Between requests, there may be no active TCP connection.

This affects death propagation (§6.4): "dropping the connection" for HTTP means invalidating the session so the next request fails, not closing a TCP socket. If the client has an open SSE stream, closing it provides immediate notification. Otherwise, the client discovers the session is dead on its next request.

# Project: Lazy Load Container

## Problem

MCP requests like `tools/list` and `initialize` currently block on sandbox creation (image pull, container start, readiness check). This means the server takes many seconds to respond to any MCP request, even those that don't need a sandbox. This is a poor user experience — MCP clients appear hung during container startup.

**Goal:** The server should respond to all MCP requests except `terminal_execute` without starting a container. The sandbox is created lazily on the first `terminal_execute` call.

## Design

### Core Change

Move sandbox creation from the lifespan entry (eager) to the first `terminal_execute` tool call (lazy). The `SessionContext` becomes the owner of lazy sandbox lifecycle, including concurrency-safe creation and cleanup.

### Key Properties

1. **Lazy creation**: No sandbox is created until the first `terminal_execute` call. `tools/list`, `initialize`, and all other MCP requests work immediately.
2. **Concurrency safe**: An `asyncio.Lock` ensures only one sandbox is created per session, even if multiple `terminal_execute` calls arrive simultaneously.
3. **Timeout isolation**: The command timeout (`ExecRequest.timeout`) applies only to the `sandbox.exec()` call, not to sandbox startup. Sandbox creation has its own internal timeouts (e.g., `docker run` 30s, readiness check 15s).
4. **Retry on creation failure**: If sandbox creation fails on the first `terminal_execute`, the error is returned for that call. Subsequent `terminal_execute` calls will retry creation. Once a sandbox is successfully created, it's used for all future calls. If the sandbox dies after creation, the session is dead (existing behavior via `SandboxDiedError`).
5. **Deferred validation**: `backend.validate()` is no longer called eagerly at startup. It runs automatically inside `backend.create_sandbox()` on first exec (the base class `create_sandbox()` calls `validate()` which is cached after first success).
6. **All backends**: This is a server-layer change. Docker, WASM, Modal — all backends benefit. No backend code changes needed.

### What Doesn't Change

- `tool_instructions()` is still called eagerly during `create_server()` — it reads config only, no backend interaction needed.
- Tool description assembly happens at startup (unchanged).
- CLI argument parsing and config validation happen at startup (unchanged).
- The `Backend` and `Sandbox` ABCs are unchanged.
- `ExecRequest`, `ExecResult` are unchanged.
- The handler's input validation, response formatting, and error mapping are unchanged.

## Implementation Tasks

### Task 1: Refactor `SessionContext` in `server.py`

Replace the current `@dataclass` with a class that owns lazy sandbox lifecycle.

**Current:**

```python
@dataclass
class SessionContext:
    sandbox: Sandbox
    death_task: asyncio.Task[None]
```

**New:**

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

        Returns:
            The sandbox instance.

        Raises:
            BackendError: If sandbox creation fails. The next call
                will retry creation.
        """
        if self._sandbox is not None:
            return self._sandbox
        async with self._lock:
            # Double-check after acquiring lock
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
                # Unexpected error monitoring sandbox — treat as death
                pass

            # Sandbox died (or monitoring failed)
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

**Key design notes:**

- `sandbox` and `death_task` properties provide read-only access (returns `None` if not yet created). This keeps test compatibility for code that checks these fields.
- `get_or_create_sandbox()` uses the double-checked locking pattern: fast path without lock, then lock + re-check.
- If `create_sandbox()` raises, `self._sandbox` stays `None`, so the next call retries.
- `_start_death_monitor()` captures the `sandbox` local variable directly, not `self._sandbox`, so it's safe to call before assigning `self._sandbox`.
- `cleanup()` is a no-op if no sandbox was ever created.

### Task 2: Simplify `create_lifespan()` in `server.py`

The lifespan no longer creates a sandbox — it just sets up a `SessionContext` and cleans up on exit.

**Current:**

```python
def create_lifespan(backend, transport, *, death_callback=None):
    @asynccontextmanager
    async def lifespan(server):
        sandbox = await backend.create_sandbox()
        async def _monitor_death(): ...
        death_task = asyncio.create_task(_monitor_death())
        try:
            yield SessionContext(sandbox=sandbox, death_task=death_task)
        finally:
            death_task.cancel()
            ...
            await sandbox.stop()
    return lifespan
```

**New:**

```python
def create_lifespan(backend, transport, *, death_callback=None):
    @asynccontextmanager
    async def lifespan(server):
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

The function signature is unchanged — `create_lifespan(backend, transport, *, death_callback=None)`. The return type is unchanged. Only the body is simplified.

### Task 3: Update the tool handler in `server.py`

The handler must use `get_or_create_sandbox()` instead of direct attribute access, and handle `BackendError` from lazy creation.

**Current (inside `_create_handler`):**

```python
sandbox = ctx.request_context.lifespan_context.sandbox

request = ExecRequest(...)

try:
    result = await sandbox.exec(request)
except SandboxDiedError as e:
    ...
```

**New:**

```python
session_context = ctx.request_context.lifespan_context

try:
    sandbox = await session_context.get_or_create_sandbox()
except BackendError as e:
    return CallToolResult(
        content=[TextContent(type="text", text=str(e))],
        isError=True,
    )

request = ExecRequest(...)

try:
    result = await sandbox.exec(request)
except SandboxDiedError as e:
    ...
```

Note: `BackendError` is already imported in `server.py`. The `get_or_create_sandbox()` call must come before `ExecRequest` construction (it's a separate step, not interleaved with validation).

### Task 4: Remove eager backend validation from `cli.py`

Remove the `_validate_backend()` call from `main()`. Validation now happens lazily inside `backend.create_sandbox()` on first `terminal_execute`.

**In `main()`**, remove:

```python
try:
    _validate_backend(backend)
except BackendError as e:
    _startup_error(str(e))
```

The `_validate_backend()` function itself can be removed if no other code references it. Check for references first. The `_async_main()` function also has backend validation — if it exists and is used, remove the validation from there too.

### Task 5: Update unit tests in `test_server.py`

#### 5a: Update `mock_context` fixture

The fixture needs to create a `SessionContext` with the new constructor. Pre-create the sandbox for handler tests that configure exec results before calling the handler.

```python
@pytest.fixture
async def mock_context(mock_backend: MockBackend) -> MagicMock:
    ctx = MagicMock()
    session_ctx = SessionContext(
        backend=mock_backend,
        transport="stdio",
    )
    # Pre-create sandbox so handler tests can configure exec results
    await session_ctx.get_or_create_sandbox()
    ctx.request_context.lifespan_context = session_ctx
    return ctx
```

Handler tests that access `mock_context.request_context.lifespan_context.sandbox.exec_results.append(...)` will still work because `session_ctx.sandbox` returns the created `MockSandbox`.

#### 5b: Update lifespan tests

Existing lifespan tests assert that the sandbox is created inside the lifespan. Update these:

- `test_lifespan_creates_sandbox` → The lifespan no longer creates a sandbox eagerly. Test that the yielded `SessionContext` has `sandbox=None` initially, and that calling `get_or_create_sandbox()` creates it.
- `test_lifespan_yields_session_context` → Update to check new `SessionContext` fields. `sandbox` is `None`, `death_task` is `None`.
- `test_lifespan_cancels_death_task_on_exit` → Create sandbox during lifespan, then verify death task cancelled on exit.
- `test_lifespan_calls_sandbox_stop_on_exit` → Create sandbox during lifespan, then verify stop on exit.
- `test_lifespan_stops_sandbox_even_if_exception_raised` → Create sandbox, raise exception, verify stop.
- Death propagation tests → Create sandbox first, then simulate death.

#### 5c: Add new tests for lazy creation

Add tests for the new `SessionContext` behavior:

- **`test_session_context_lazy_creation`**: `SessionContext` starts with `sandbox=None`. After `get_or_create_sandbox()`, sandbox is not None.
- **`test_session_context_returns_same_sandbox`**: Two calls to `get_or_create_sandbox()` return the same sandbox instance.
- **`test_session_context_concurrent_creation`**: Launch multiple concurrent `get_or_create_sandbox()` calls. Assert only one sandbox is created (check `MockBackend` create count or sandbox IDs).
- **`test_session_context_cleanup_without_sandbox`**: Call `cleanup()` without ever creating a sandbox. Should be a no-op (no errors).
- **`test_session_context_cleanup_with_sandbox`**: Create sandbox, then cleanup. Death task cancelled, sandbox stopped.
- **`test_session_context_retry_on_creation_failure`**: First `get_or_create_sandbox()` fails (configure MockBackend to raise). Second call succeeds. Verify sandbox is created on retry.
- **`test_handler_backend_error_on_lazy_creation`**: Handler with a backend that fails to create sandbox returns `isError=True` with the BackendError message.
- **`test_session_context_death_monitor_starts_after_creation`**: `death_task` is None before `get_or_create_sandbox()`, not None after.

#### 5d: Add concurrency test helper to MockBackend

To test that only one sandbox is created, add a `create_count` tracker to `MockBackend`:

```python
class MockBackend(Backend):
    def __init__(self, ...):
        ...
        self.create_count = 0

    async def _create_sandbox(self) -> Sandbox:
        self.create_count += 1
        return MockSandbox(...)
```

To test creation failure + retry, add a `fail_next_create` flag:

```python
class MockBackend(Backend):
    def __init__(self, ...):
        ...
        self.fail_next_create = False

    async def _create_sandbox(self) -> Sandbox:
        if self.fail_next_create:
            self.fail_next_create = False
            raise BackendError("mock creation failure")
        self.create_count += 1
        return MockSandbox(...)
```

### Task 6: Update integration tests in `test_lifecycle_integration.py`

- **`test_lifecycle_full_stdio_session`**: The lifespan no longer creates a sandbox on entry. Update to call `get_or_create_sandbox()` or just call `sandbox.exec()` (which first requires getting the sandbox). The simplest approach: call `ctx.get_or_create_sandbox()` before the exec.
- **`test_sandbox_creation_failure_raises`**: The error now happens on `get_or_create_sandbox()`, not on lifespan entry. Update the test to call `get_or_create_sandbox()` inside the lifespan.
- **`test_lifecycle_stops_sandbox_on_exception`**: Create sandbox first, then raise exception.
- **`test_lifecycle_death_task_cancelled_before_stop`**: Create sandbox first.
- **Death propagation tests**: Create sandbox first, then kill container.
- **`test_container_removed_after_lifespan`**: Create sandbox during lifespan, verify removal after.

Also add a new integration test:

- **`test_lifespan_no_container_before_exec`**: Enter lifespan, verify no container is running (no docker process spawned). Then call `get_or_create_sandbox()`, verify container starts. This confirms the lazy behavior end-to-end.

### Task 7: Update CLI tests in `test_cli.py`

Remove or update any tests that test eager backend validation in `main()`. If there are tests for `_validate_backend()`, remove them or convert them to test that validation happens lazily (which is tested via server tests, not CLI tests).

## Ordering

Tasks should be done in this order:

1. **Task 5d** — Add test helpers to MockBackend (no breaking changes)
2. **Task 1** — Refactor SessionContext (core change)
3. **Task 2** — Simplify create_lifespan (depends on Task 1)
4. **Task 3** — Update handler (depends on Task 1)
5. **Task 4** — Remove eager validation from CLI
6. **Task 5a–c** — Update and add unit tests (depends on Tasks 1–4)
7. **Task 6** — Update integration tests (depends on Tasks 1–4)
8. **Task 7** — Update CLI tests (depends on Task 4)

## Test Plan Summary

| Category | What to test | Type |
|---|---|---|
| Lazy creation | `SessionContext` starts with no sandbox, creates on first `get_or_create_sandbox()` | Unit |
| Idempotent | Multiple calls return same sandbox | Unit |
| Concurrency | Concurrent `get_or_create_sandbox()` creates exactly one sandbox | Unit |
| Retry | Failed creation allows retry on next call | Unit |
| Cleanup no-op | `cleanup()` without sandbox creation is safe | Unit |
| Cleanup with sandbox | `cleanup()` cancels death task and stops sandbox | Unit |
| Handler lazy creation | Handler triggers sandbox creation, returns exec result | Unit |
| Handler creation failure | Handler returns `isError=True` on `BackendError` | Unit |
| Death monitor timing | Death task starts only after sandbox creation | Unit |
| Timeout isolation | Command timeout applies to exec only, not sandbox startup | Unit (verify ExecRequest.timeout is passed to sandbox.exec(), not to create_sandbox()) |
| E2E lazy start | Lifespan yields immediately, no container until exec | Integration |
| E2E full lifecycle | Create sandbox on first exec, exec commands, cleanup | Integration |
| E2E creation failure | Bad image fails on first exec, not on lifespan entry | Integration |
| No eager validation | Server starts without backend validation | Unit (CLI) |

## Spec Updates

The following spec documents have been updated to reflect the lazy loading design. These updates were made alongside this plan — the coding agent does not need to update specs.

- `specs/functional_spec.md` — Sections 3.3, 4.1, 4.2, 4.3
- `specs/architecture/connection_lifecycle.md` — Sections 2, 3, 5, 8
- `specs/architecture/mcp_server.md` — Sections 2.2, 3.3, 6.1
- `specs/architecture/cli_and_startup.md` — Section 6

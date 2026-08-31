# Architecture: Backend Abstraction Layer

**Phase 2** of the architecture specification. Defines the Python ABCs for Backend and Sandbox, shared type contracts, error types, async patterns, and how the MCP layer interacts with backends without knowing which one is running.

**References:** D2 (pluggability), D19 (ABC), D28 (multi-sandbox), D29 (serial exec), D14 (mounts — future), D26 (resource limits are backend-specific), Functional spec §5.

---

## 1. Overview

The backend abstraction is the central interface in Kilntainers. It cleanly separates the MCP server (protocol concerns, tool registration, request validation) from backend implementations (sandbox lifecycle, command execution, resource management). The MCP layer programs against these abstractions and never imports or references a specific backend.

Two ABCs define the contract:

- **`Backend`** — Factory for creating sandboxes. Handles prerequisite validation and provides tool description text. One instance per server process, configured at startup.
- **`Sandbox`** — An active, isolated environment. Handles command execution, stop, and death detection. One instance per MCP session (stdio has one session; HTTP has many).

Supporting types:

- **`ExecRequest`** — Validated parameters for a single command execution.
- **`ExecResult`** — Structured response from command execution.
- **`Mount`** — Future: host-to-sandbox filesystem mapping (designed in v1, not implemented).

All types live in `src/kilntainers/backends/base.py` (Phase 1).

---

## 2. Shared Types

### 2.1 ExecResult

The return type from every exec call. Immutable, with no optional fields — every execution produces all four values.

```python
@dataclass(frozen=True, slots=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int
    exec_duration_ms: int
```

This maps directly to the MCP response schema (Functional spec §2.2). The MCP layer serializes this to JSON for the tool response.

**Field semantics:**

| Field | Description |
|---|---|
| `stdout` | Standard output. Empty string if none, or if suppressed by timeout/output-limit. |
| `stderr` | Standard error. Contains infrastructure messages for timeout/output-limit conditions. |
| `exit_code` | Process exit code. 124 for timeout (GNU convention). Backend-chosen code for output limit. |
| `exec_duration_ms` | Wall-clock execution time in milliseconds. |

**Important:** Timeout and output-limit conditions produce an `ExecResult` (not an exception). These are normal results from the backend's perspective — the process ran, hit a limit, and was killed. The MCP layer returns them as `isError: false`. Only infrastructure failures (sandbox death, internal errors) raise exceptions.

### 2.2 ExecRequest

Validated parameters for a command execution. Constructed by the MCP layer after input validation. The MCP layer resolves defaults (effective timeout, configured output limit) so the backend always receives concrete values.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ExecRequest:
    # Exactly one of command or args must be provided
    command: str | None = None
    args: list[str] | None = None

    # Optional parameters
    stdin: str | None = None
    working_directory: str | None = None

    # Always provided by MCP layer (defaults resolved before reaching backend)
    timeout: int          # seconds
    output_limit: int     # bytes
```

**kw_only=True** forces callers to use keyword arguments, which is clearer for a dataclass with many optional fields and avoids positional argument ordering bugs.

**Validation in `__post_init__`:**

```python
def __post_init__(self) -> None:
    if self.command is not None and self.args is not None:
        raise ValueError("command and args are mutually exclusive")
    if self.command is None and self.args is None:
        raise ValueError("either command or args must be provided")
    if self.working_directory is not None and not self.working_directory.startswith("/"):
        raise ValueError("working_directory must be an absolute path")
    if self.timeout < 1:
        raise ValueError("timeout must be at least 1 second")
    if self.output_limit < 1:
        raise ValueError("output_limit must be positive")
```

This is defense-in-depth. The MCP layer validates the raw tool input before constructing an ExecRequest, but the dataclass validates its own invariants so backend implementations can trust the request is well-formed. A malformed ExecRequest is always a programming error (bug in the MCP layer), not a user input error.

### 2.3 Mount (Future — Designed, Not Implemented)

Defined in v1 to reserve the interface shape. Not implemented by any backend. (D14)

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class Mount:
    host_path: str       # absolute path on the host
    sandbox_path: str    # absolute path inside the sandbox
```

When mounted working directories are implemented, `Mount` will be passed to `Backend.create_sandbox()`. For now, it exists only as a type definition — passing mounts to any v1 backend raises `NotImplementedError`.

**Design considerations documented for future:**

- Docker: maps to `docker run -v host_path:sandbox_path`.
- Modal: would map to Modal volume mounts.
- E2B: would map to E2B file sync.

We don't need to support `readonly` mounts at this time. If added later, will require additional work, but not a constraint we want to add now.

---

## 3. Sandbox ABC

A Sandbox represents a running, isolated environment. It is created by `Backend.create_sandbox()` and is the target for all command execution.

```python
class Sandbox(ABC):
    """An active, isolated sandbox for executing commands.

    Created by Backend.create_sandbox(). Each Sandbox is independent —
    no shared state between Sandbox instances. Supports async context
    manager for automatic cleanup.
    """

    @property
    @abstractmethod
    def sandbox_id(self) -> str:
        """Unique identifier for this sandbox.

        Used for logging, debugging, and session-to-sandbox mapping in
        HTTP mode. For Docker, this is the container ID (short form).
        """
        ...

    @abstractmethod
    async def exec(self, request: ExecRequest) -> ExecResult:
        """Execute a command in the sandbox.

        Returns an ExecResult for all normal outcomes including timeout
        and output-limit conditions. Raises SandboxDiedError if the
        sandbox has died (before or during execution).
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the sandbox and release all resources.

        Idempotent — safe to call on an already-stopped sandbox.
        """
        ...

    @abstractmethod
    async def wait_for_death(self) -> None:
        """Block until the sandbox dies unexpectedly.

        Resolves when the sandbox terminates for reasons other than
        stop() being called (OOM, external kill, daemon crash, etc.).

        Must NOT resolve when stop() is called. Implementations track
        whether stop was requested and suppress the signal in that case.

        The MCP layer runs this as a background task to detect sandbox
        death between exec calls. On normal shutdown, the MCP layer
        cancels this task before calling stop().
        """
        ...

    # --- Context manager support (concrete, not abstract) ---

    async def __aenter__(self) -> "Sandbox":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.stop()
```

### Method contracts

#### `exec(request)`

- **Input:** A validated `ExecRequest`.
- **Output:** `ExecResult` for all normal outcomes — success, command failure (non-zero exit), timeout, output-limit exceeded.
- **Raises `SandboxDiedError`:** If the sandbox has died (detected before or during execution). The MCP layer translates this to an `isError: true` MCP response and then drops the connection.
- **Timeout enforcement:** The backend monitors wall-clock time. When the timeout fires: kill the process, set `exit_code` to 124, set `stderr` to the timeout notice, set `stdout` to empty string. No partial output. (Functional spec §2.3)
- **Output limit enforcement:** The backend monitors combined stdout+stderr byte size. When exceeded: kill the process, set `stderr` to the limit-exceeded notice, set `stdout` to empty string. No partial output. (Functional spec §2.4)
- **Shell selection:** For `command` mode, the backend wraps the string in its configured shell (e.g., `bash -c`). For `args` mode, arguments are passed directly via exec with no shell. (D15, D20)
- **Stdin piping:** If `request.stdin` is not None, pipe its content to the process's standard input. If None, stdin is not connected (EOF). (D30)

#### `stop()`

- **Idempotent.** Calling stop() on an already-stopped sandbox is a no-op.
- Must release all resources (containers, processes, temp files).
- For Docker: `docker stop` + `--rm` handles cleanup.
- Should complete within 10 seconds. If cleanup stalls, force-kill and proceed. (Functional spec §4.4)

#### `wait_for_death()`

- The MCP layer creates a background `asyncio.Task` for this immediately after receiving the sandbox from `create_sandbox()`.
- If the sandbox dies unexpectedly, the task completes — the MCP layer detects this and drops the connection.
- On normal shutdown, the MCP layer cancels this task before calling `stop()`.
- **Implementation note:** The sandbox must distinguish unexpected death from normal stop. Typical approach: set a `_stop_requested` flag in `stop()` before issuing the stop command; the monitoring logic checks this flag when the sandbox exits.

#### `sandbox_id`

- Read-only property, available immediately after creation.
- For Docker: the container ID (short form, 12 hex chars).
- Used by the MCP layer for logging and session-to-sandbox mapping.

### Context manager pattern

The `__aenter__`/`__aexit__` methods are concrete (not abstract) since the behavior is the same for all backends: enter returns self, exit calls stop. This enables:

```python
async with await backend.create_sandbox() as sandbox:
    result = await sandbox.exec(request)
# sandbox.stop() called automatically, even on exception
```

---

## 4. Backend ABC

A Backend is a factory for creating sandboxes. One Backend instance exists per server process. It holds configuration (from CLI args) and knows how to validate prerequisites and create sandboxes.

```python
class Backend(ABC):
    """Factory for creating sandboxes.

    Configured at startup from CLI arguments. One instance per server
    process. Creates independent Sandbox objects on demand.
    """

    def __init__(self) -> None:
        self._validated: bool = False

    async def validate(self) -> None:
        """Check all prerequisites. Raises BackendError on failure.

        Results are cached — subsequent calls are no-ops after the
        first successful validation.
        """
        if self._validated:
            return
        await self._validate()
        self._validated = True

    @abstractmethod
    async def _validate(self) -> None:
        """Implementation-specific validation. Override this method.

        Check that the backend's prerequisites are met (e.g., Docker
        daemon is reachable, configured image is valid). Raise
        BackendError with an actionable message on failure.
        """
        ...

    async def create_sandbox(
        self, *, mounts: list[Mount] | None = None
    ) -> Sandbox:
        """Create a new sandbox and return it ready to use.

        Auto-validates if validate() has not been called. Returns a
        ready-to-use Sandbox (readiness check already passed).

        mounts: Reserved for future use (D14). V1 implementations
        raise NotImplementedError if mounts is provided.
        """
        await self.validate()
        if mounts is not None:
            raise NotImplementedError(
                "Mounts are not supported in v1. "
                "This parameter is reserved for future use."
            )
        return await self._create_sandbox()

    @abstractmethod
    async def _create_sandbox(self) -> Sandbox:
        """Implementation-specific sandbox creation. Override this method.

        Must perform the full startup sequence:
        1. Create the sandbox (e.g., docker run)
        2. Verify readiness (e.g., trivial exec)
        3. Return the ready Sandbox object

        Raise BackendError if startup fails at any step.
        """
        ...

    @abstractmethod
    def tool_instructions(self) -> str | None:
        """Return tool description text for the terminal_execute tool.

        Returns a string describing this backend's sandbox capabilities,
        or None if the backend cannot provide a description (e.g.,
        custom Docker image where the baked-in description doesn't apply).

        The MCP layer uses this in tool description assembly (§6).
        When None, the server requires --tool-instruction-override.
        """
        ...
```

### Design: Template Method pattern

`validate()` and `create_sandbox()` are **concrete methods** on the ABC that handle cross-cutting concerns (validation caching, auto-validation, mounts rejection), then delegate to **abstract `_validate()` and `_create_sandbox()`** that subclasses override. This ensures:

- **Validation is never skipped.** `create_sandbox()` always calls `validate()`. Backend authors can't forget.
- **Validation runs only once.** The `_validated` flag prevents redundant checks on repeated `create_sandbox()` calls (HTTP mode creates many sandboxes).
- **Mounts rejection is centralized.** V1 mounts handling lives in one place, not duplicated across backends.

Backend subclasses override `_validate()` and `_create_sandbox()` — never `validate()` or `create_sandbox()`.

### Constructor

The ABC's `__init__` only initializes the `_validated` flag. Backend subclasses define their own constructors to accept configuration:

```python
class DockerBackend(Backend):
    def __init__(self, config: DockerBackendConfig) -> None:
        super().__init__()
        self._config = config
```

The config type is backend-specific (Phase 5 covers config dataclasses). The ABC does not prescribe constructor parameters because different backends need fundamentally different configuration. (D26 — resource limits are purely backend-specific.)

### CLI Argument Classmethods

Each backend owns its CLI argument definitions and config construction through two abstract classmethods on the ABC:

```python
@classmethod
@abstractmethod
def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
    """Register backend-specific CLI arguments on the given argparse group.

    Called during parser construction for every registered backend,
    so that --help shows all available options regardless of which
    backend is selected. Argument names should be distinctive to
    avoid collisions between backends (e.g., --docker-run-flag,
    not --extra-flag).
    """
    ...

@classmethod
@abstractmethod
def config_from_args(cls, args: argparse.Namespace) -> "BackendConfig":
    """Build this backend's config dataclass from parsed CLI arguments.

    Called during config construction for the selected backend only.
    The backend reads its own arguments from the shared namespace
    and returns its typed, frozen config object.
    """
    ...
```

**Why classmethods:** These are called before any backend instance exists — during parser construction and config building. They operate on the class, not an instance. Using `@classmethod` (not `@staticmethod`) allows subclasses to be identified and enables future patterns like inheritance.

**Why on the ABC:** Putting these methods on the Backend ABC ensures every backend implements them. A backend that forgets to register its arguments or provide config construction will fail at class definition time (abstract method enforcement), not at runtime.

**Example — DockerBackend:**

```python
class DockerBackend(Backend):
    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        group.add_argument("--engine", default="docker", ...)
        group.add_argument("--image", default="debian:bookworm-slim", ...)
        group.add_argument("--shell", default="/bin/bash", ...)
        group.add_argument("--network", action="store_true", ...)
        group.add_argument("--cpu", default=None, ...)
        group.add_argument("--memory", default=None, ...)
        group.add_argument("--docker-run-flag", action="append", ...)

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> DockerBackendConfig:
        return DockerBackendConfig(
            engine=args.engine,
            image=args.image,
            shell=args.shell,
            network_enabled=args.network,
            cpu=args.cpu,
            memory=args.memory,
            docker_run_flags=args.docker_run_flags or [],
            default_timeout=args.timeout,
        )
```

This keeps `cli.py` backend-agnostic — it iterates the registry and delegates, with zero backend-specific code. See Phase 5 §3 for the full parser construction flow.

### Why `create_sandbox()` creates AND starts

A sandbox that exists but isn't running is a useless intermediate state — `exec()` can't work, `sandbox_id` may not exist yet (Docker hasn't assigned a container ID), `wait_for_death()` has nothing to monitor. Splitting creation from starting would force every backend to guard every method with "am I started?" checks and add a third state (created-not-started, running, stopped) with no benefit. The Python standard library uses this same pattern: `asyncio.start_server()` and `subprocess.Popen()` create-and-start in one call.

Re-starting a stopped sandbox is explicitly not supported (D6 — ephemeral, no crash recovery), so there's no reason for `start()` to live on Sandbox.

### `tool_instructions()` is synchronous

This method returns a pre-composed string (or None) based on the backend's configuration. No I/O is involved — the backend knows its image, shell, timeout, and output-limit at construction time. Making it synchronous simplifies the tool description assembly in the MCP layer (which happens during startup, before the async event loop is fully running in some configurations).

---

## 5. Error Types

Error types used across the backend abstraction. These live in `src/kilntainers/errors.py` (Phase 1).

```python
class KilntainersError(Exception):
    """Base exception for all Kilntainers errors."""
    pass


class BackendError(KilntainersError):
    """Raised by backend operations when something goes wrong.

    Used for prerequisite validation failures (Docker not running),
    sandbox startup failures (image pull failed), and internal backend
    errors. The message should be actionable — tell the operator what
    to fix.
    """
    pass


class SandboxDiedError(KilntainersError):
    """Raised when a sandbox has died unexpectedly.

    Raised by Sandbox.exec() if the sandbox is dead (detected before
    or during execution). The MCP layer catches this, returns an
    isError: true response, and drops the connection.
    """
    pass
```

### Error vs ExecResult decision boundary

| Condition | Mechanism | Why |
|---|---|---|
| Command succeeds/fails | `ExecResult` (exit_code reflects outcome) | Normal operation — the tool worked. |
| Timeout fires | `ExecResult` (exit_code 124, stderr notice) | The command ran and hit a limit. Tool worked. |
| Output limit exceeded | `ExecResult` (exit_code 1, stderr notice) | Same — a limit condition, not a tool failure. |
| Sandbox died | `SandboxDiedError` exception | The tool itself is broken — cannot execute. |
| Backend can't start | `BackendError` exception | Infrastructure failure. |
| Invalid ExecRequest | `ValueError` (from dataclass validation) | Programming error in MCP layer. |

This boundary matches the functional spec's `isError` mapping (§2.5): ExecResults become `isError: false`, exceptions become `isError: true`.

---

## 6. MCP Layer Interaction Patterns

The MCP layer (Phase 4) interacts with the backend abstraction through a well-defined sequence. This section shows the patterns; it does not define the MCP layer's internal design.

### 6.1 Startup (stdio mode)

```python
# 1. Construct backend with parsed config
backend = DockerBackend(config)

# 2. Validate prerequisites (Functional spec §3.3)
await backend.validate()   # raises BackendError on failure

# 3. Get tool description for tool registration
instructions = backend.tool_instructions()
# ... assemble final tool description with overrides/extensions ...

# 4. Create the single sandbox
sandbox = await backend.create_sandbox()

# 5. Start death monitor
death_task = asyncio.create_task(sandbox.wait_for_death())

# 6. Run MCP server (accepts tool calls, delegates to sandbox.exec())
# ... server loop ...

# 7. Shutdown
death_task.cancel()
await sandbox.stop()
```

### 6.2 Per-session (HTTP mode)

```python
# On new session (initialize request):
sandbox = await backend.create_sandbox()
death_task = asyncio.create_task(sandbox.wait_for_death())

# On tool call:
request = ExecRequest(
    command=params.get("command"),
    args=params.get("args"),
    stdin=params.get("stdin"),
    working_directory=params.get("working_directory"),
    timeout=params.get("timeout", server_default_timeout),
    output_limit=server_output_limit,
)
try:
    result = await sandbox.exec(request)
    # Return result as isError: false
except SandboxDiedError:
    # Return error as isError: true, then drop session

# On session end (client close / idle timeout):
death_task.cancel()
await sandbox.stop()
```

### 6.3 Death detection

```python
# Background task — runs for the lifetime of the sandbox
async def monitor_sandbox_death(sandbox: Sandbox, on_death: Callable):
    try:
        await sandbox.wait_for_death()
        # Sandbox died unexpectedly
        on_death()
    except asyncio.CancelledError:
        pass  # Normal shutdown — task cancelled before stop()
```

The `on_death` callback triggers connection teardown:
- **stdio:** Exit the process.
- **HTTP:** Terminate the session and clean up.

---

## 7. Async Design

### Why async

All backend operations involve I/O waits: subprocess calls (Docker CLI), network operations (image pull), process monitoring. Async enables the server to handle multiple concurrent sessions in HTTP mode without threading.

### Async method summary

| Method | Async? | Reason |
|---|---|---|
| `Backend.add_cli_arguments()` | No (classmethod) | Pure argparse registration. No I/O. |
| `Backend.config_from_args()` | No (classmethod) | Pure config construction. No I/O. |
| `Backend.validate()` | Yes | May run subprocess (e.g., `docker info`). |
| `Backend.create_sandbox()` | Yes | Pulls images, creates containers, runs readiness check. |
| `Backend.tool_instructions()` | No | Returns pre-composed string from config. No I/O. |
| `Sandbox.exec()` | Yes | Runs subprocess, monitors output, enforces limits. |
| `Sandbox.stop()` | Yes | Runs subprocess (`docker stop`), waits for cleanup. |
| `Sandbox.wait_for_death()` | Yes | Long-running monitor (e.g., `docker wait`). |
| `Sandbox.sandbox_id` | No (property) | Returns cached value. No I/O. |

### Event loop ownership

The MCP library owns the event loop. The backend abstraction does not create or manage event loops — it provides coroutines that the MCP layer schedules. The CLI entry point uses `asyncio.run()` to bootstrap.

---

## 8. Concurrency and Serialization

### Multi-sandbox (D28)

The backend supports creating multiple concurrent sandboxes. `Backend.create_sandbox()` can be called multiple times, each returning an independent `Sandbox`. No shared state between sandboxes. This is required for HTTP mode where each session has its own sandbox.

The Backend instance itself must be safe for concurrent `create_sandbox()` calls (multiple HTTP sessions initializing simultaneously). The `_validated` flag is set before any concurrent calls, so no race condition exists (validation happens once during server startup, before accepting connections).

### Serial exec within a sandbox (D29)

V1 serializes exec calls within a single sandbox. This is an **implementation detail** of the Sandbox, not an ABC contract. The ABC does not mention serialization — future backends may support parallel exec.

**Implementation approach:** The Sandbox subclass uses an `asyncio.Lock` internally:

```python
class DockerSandbox(Sandbox):
    def __init__(self) -> None:
        self._exec_lock = asyncio.Lock()

    async def exec(self, request: ExecRequest) -> ExecResult:
        async with self._exec_lock:
            return await self._do_exec(request)
```

The MCP layer does not coordinate exec serialization — it simply awaits `sandbox.exec()`. If the sandbox serializes internally, concurrent calls queue transparently. If a future backend supports parallel exec, the MCP layer's code doesn't change.

---

## 9. Future Extensibility

### Adding a new backend

A new backend (e.g., Modal, E2B, WASI) requires:

1. **Create `src/kilntainers/backends/{name}.py`** with `{Name}Backend(Backend)` and `{Name}Sandbox(Sandbox)`.
2. **Implement all abstract methods:** `_validate()`, `_create_sandbox()`, `tool_instructions()`, `add_cli_arguments()`, `config_from_args()` on the backend; `exec()`, `stop()`, `wait_for_death()`, `sandbox_id` on the sandbox.
3. **Create a config dataclass** (e.g., `ModalBackendConfig(BackendConfig)`) in `config.py`.
4. **Register in `backends/__init__.py`** so the `--backend` CLI arg can find it.

No changes to `cli.py`, the MCP layer, Backend ABC, or Sandbox ABC are needed. The backend's `add_cli_arguments()` classmethod registers its own CLI args, and `config_from_args()` constructs its own config. This is the pluggability guarantee (D2).

### Mounts (D14)

The `create_sandbox(mounts=...)` parameter is defined and typed but rejected in v1. When a backend is ready to support mounts:

1. Remove the `NotImplementedError` in `Backend.create_sandbox()` (or make it conditional — only reject if the specific backend doesn't support mounts).
2. Pass mounts through to `_create_sandbox()`.
3. The backend implementation maps mounts to its native mechanism (Docker bind mounts, Modal volumes, etc.).

### Parallel exec (D29)

The ABC does not enforce serialization. To enable parallel exec in a future backend, simply omit the internal lock. The MCP layer already awaits each exec independently — no API change needed.

---

## 10. Complete Type Reference

For clarity, here is the full set of types and ABCs defined in `backends/base.py`, in dependency order:

```python
"""Backend abstraction layer — ABCs and shared types."""

import argparse
from abc import ABC, abstractmethod
from dataclasses import dataclass
from types import TracebackType

from kilntainers.config import BackendConfig


# --- Shared types ---

@dataclass(frozen=True, slots=True)
class ExecResult:
    """Result of a command execution."""
    stdout: str
    stderr: str
    exit_code: int
    exec_duration_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecRequest:
    """Validated parameters for a command execution."""
    command: str | None = None
    args: list[str] | None = None
    stdin: str | None = None
    working_directory: str | None = None
    timeout: int          # seconds — always provided by MCP layer
    output_limit: int     # bytes — always provided by MCP layer

    def __post_init__(self) -> None:
        if self.command is not None and self.args is not None:
            raise ValueError("command and args are mutually exclusive")
        if self.command is None and self.args is None:
            raise ValueError("either command or args must be provided")
        if self.working_directory is not None and not self.working_directory.startswith("/"):
            raise ValueError("working_directory must be an absolute path")
        if self.timeout < 1:
            raise ValueError("timeout must be at least 1 second")
        if self.output_limit < 1:
            raise ValueError("output_limit must be positive")


@dataclass(frozen=True, slots=True, kw_only=True)
class Mount:
    """Host-to-sandbox filesystem mapping. Designed in v1, not implemented."""
    host_path: str
    sandbox_path: str


# --- ABCs ---

class Sandbox(ABC):
    """An active, isolated sandbox for executing commands."""

    @property
    @abstractmethod
    def sandbox_id(self) -> str: ...

    @abstractmethod
    async def exec(self, request: ExecRequest) -> ExecResult: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def wait_for_death(self) -> None: ...

    async def __aenter__(self) -> "Sandbox":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.stop()


class Backend(ABC):
    """Factory for creating sandboxes."""

    def __init__(self) -> None:
        self._validated: bool = False

    # --- CLI argument classmethods ---

    @classmethod
    @abstractmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        """Register backend-specific CLI arguments."""
        ...

    @classmethod
    @abstractmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        """Build backend config from parsed CLI arguments."""
        ...

    # --- Lifecycle methods ---

    async def validate(self) -> None:
        if self._validated:
            return
        await self._validate()
        self._validated = True

    @abstractmethod
    async def _validate(self) -> None: ...

    async def create_sandbox(
        self, *, mounts: list[Mount] | None = None
    ) -> Sandbox:
        await self.validate()
        if mounts is not None:
            raise NotImplementedError(
                "Mounts are not supported in v1. "
                "This parameter is reserved for future use."
            )
        return await self._create_sandbox()

    @abstractmethod
    async def _create_sandbox(self) -> Sandbox: ...

    @abstractmethod
    def tool_instructions(self) -> str | None: ...
```

---

## 11. Testing

### Unit tests for the abstraction (`tests/unit/backends/test_base.py`)

#### ExecRequest validation

Test `__post_init__` enforcement:

- Both `command` and `args` provided → `ValueError`.
- Neither `command` nor `args` provided → `ValueError`.
- `working_directory` is relative path → `ValueError`.
- `timeout` < 1 → `ValueError`.
- `output_limit` < 1 → `ValueError`.
- Valid requests with `command` only, `args` only, with/without optional fields → success.

#### ExecResult construction

- All fields populated correctly.
- Frozen (immutable) — assignment raises `FrozenInstanceError`.

#### Backend ABC behavior

Test the concrete methods on Backend using a minimal concrete subclass:

```python
class StubBackend(Backend):
    """Minimal implementation for testing ABC behavior."""
    validate_called: int = 0
    start_called: int = 0

    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        group.add_argument("--stub-option", default="default")

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> StubBackendConfig:
        return StubBackendConfig(stub_option=args.stub_option)

    async def _validate(self) -> None:
        self.validate_called += 1

    async def _create_sandbox(self) -> Sandbox:
        self.start_called += 1
        return StubSandbox()

    def tool_instructions(self) -> str | None:
        return "stub instructions"
```

Tests:

- `add_cli_arguments()` registers arguments on an argparse group.
- `config_from_args()` constructs the correct config type from parsed args.
- `validate()` calls `_validate()` on first call, caches on subsequent calls.
- `create_sandbox()` auto-calls `validate()` if not already validated.
- `create_sandbox()` does not re-validate if already validated.
- `create_sandbox(mounts=[...])` raises `NotImplementedError`.
- `create_sandbox(mounts=None)` succeeds (no mounts is fine).
- `tool_instructions()` returns value from subclass.

#### Sandbox context manager

Using a stub Sandbox:

- `async with sandbox:` calls `stop()` on normal exit.
- `async with sandbox:` calls `stop()` on exception.
- `stop()` is idempotent (double-stop doesn't error).

### Mock sandbox for MCP layer tests

Phase 4 (MCP server) will need to test the server without a real backend. A `MockSandbox` and `MockBackend` in `tests/conftest.py` (or a dedicated test utility) will implement the ABCs with configurable responses:

```python
class MockSandbox(Sandbox):
    """Sandbox that returns preconfigured ExecResults."""

    def __init__(self, *, sandbox_id: str = "mock-sandbox-001"):
        self._sandbox_id = sandbox_id
        self._stopped = False
        self._death_event = asyncio.Event()
        self.exec_results: list[ExecResult] = []  # queue of results to return
        self.exec_calls: list[ExecRequest] = []    # record of calls received

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    async def exec(self, request: ExecRequest) -> ExecResult:
        if self._death_event.is_set():
            raise SandboxDiedError("mock sandbox died")
        self.exec_calls.append(request)
        if self.exec_results:
            return self.exec_results.pop(0)
        return ExecResult(stdout="", stderr="", exit_code=0, exec_duration_ms=1)

    async def stop(self) -> None:
        self._stopped = True

    async def wait_for_death(self) -> None:
        await self._death_event.wait()

    def simulate_death(self) -> None:
        """Test helper: trigger sandbox death."""
        self._death_event.set()
```

This mock is critical for Phase 4 testing — it lets the MCP server tests verify request routing, response formatting, and error handling without Docker.

### Integration test coverage (Phase 3)

The Docker backend's implementation of these ABCs is tested in Phase 3 (Docker backend architecture), which covers both unit tests (mocked subprocess) and integration tests (real Docker).

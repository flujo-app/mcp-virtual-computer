# Phase 2: Core Types & Backend Abstraction — Implementation Plan

Implement the foundational types that everything else depends on: exception hierarchy, shared data types, the Backend and Sandbox ABCs, configuration dataclasses, and the backend registry.

**Architecture references:**
- [backend_abstraction.md](../architecture/backend_abstraction.md) §2–§5, §7–§10
- [error_handling.md](../architecture/error_handling.md) §2
- [cli_and_startup.md](../architecture/cli_and_startup.md) §2 (config dataclasses)

---

## Implementation Steps (Ordered)

### Step 1: Exception Hierarchy (`src/kilntainers/errors.py`)

Create the exception hierarchy. This is foundational — other modules depend on these types.

**Reference:** backend_abstraction.md §5, error_handling.md §2

```python
class KilntainersError(Exception):
    """Base exception for all Kilntainers errors."""

class BackendError(KilntainersError):
    """Raised by backend operations when something goes wrong."""

class SandboxDiedError(KilntainersError):
    """Raised when a sandbox has died unexpectedly."""
```

---

### Step 2: Shared Types (`src/kilntainers/backends/base.py`)

Implement the dataclasses used across the codebase: `ExecResult`, `ExecRequest`, and `Mount`.

**Reference:** backend_abstraction.md §2

**ExecResult:**
- `@dataclass(frozen=True, slots=True)`
- Fields: `stdout: str`, `stderr: str`, `exit_code: int`, `exec_duration_ms: int`
- Maps directly to MCP response schema

**ExecRequest:**
- `@dataclass(frozen=True, slots=True, kw_only=True)`
- Fields: `command: str | None = None`, `args: list[str] | None = None`, `stdin: str | None = None`, `working_directory: str | None = None`, `timeout: int`, `output_limit: int`
- `__post_init__` validation:
  - `command` and `args` are mutually exclusive
  - At least one of `command` or `args` must be provided
  - `working_directory` must be absolute (starts with "/")
  - `timeout` must be >= 1
  - `output_limit` must be >= 1

**Mount:**
- `@dataclass(frozen=True, slots=True, kw_only=True)`
- Fields: `host_path: str`, `sandbox_path: str`
- Designed in v1, not implemented

---

### Step 3: Sandbox ABC (`src/kilntainers/backends/base.py`)

Implement the `Sandbox` abstract base class.

**Reference:** backend_abstraction.md §3

```python
class Sandbox(ABC):
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
```

---

### Step 4: Backend ABC (`src/kilntainers/backends/base.py`)

Implement the `Backend` abstract base class with template method pattern.

**Reference:** backend_abstraction.md §4

```python
class Backend(ABC):
    def __init__(self) -> None:
        self._validated: bool = False

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

### Step 5: Configuration Dataclasses (`src/kilntainers/config.py`)

Implement frozen dataclasses for server and backend configuration.

**Reference:** cli_and_startup.md §2

**ServerConfig:**
- `@dataclass(frozen=True, slots=True, kw_only=True)`
- Fields:
  - `transport: str = "stdio"`
  - `host: str = "127.0.0.1"`
  - `port: int = 8435`
  - `default_timeout: int = 120`
  - `output_limit: int = 2_097_152`
  - `tool_instruction_override: str | None = None`
  - `extended_tool_instruction: str | None = None`
  - `session_timeout: int = 300`

**DockerBackendConfig:**
- `@dataclass(frozen=True, slots=True, kw_only=True)`
- Fields:
  - `engine: str = "docker"`
  - `image: str = "debian:bookworm-slim"`
  - `shell: str = "/bin/bash"`
  - `network_enabled: bool = False`
  - `cpu: str | None = None`
  - `memory: str | None = None`
  - `docker_run_flags: list[str] = field(default_factory=list)`
  - `default_timeout: int = 120`

---

### Step 6: Backend Registry (`src/kilntainers/backends/__init__.py`)

Implement the backend registry and lookup function.

**Reference:** cli_and_startup.md §5

```python
BACKEND_REGISTRY: dict[str, type] = {
    "docker": DockerBackend,  # Will be stubbed for now
}

def get_backend_class(name: str) -> type:
    """Look up a backend class by name."""
```

**Note:** `DockerBackend` doesn't exist yet (Phase 3). For now, create a stub class that inherits from `Backend` with `NotImplementedError` for abstract methods, or import it as `None` and handle appropriately. A stub is cleaner for testing.

---

## Tests to Implement

### Unit Tests for Errors (`src/kilntainers/test_errors.py`)

**Reference:** error_handling.md §12.1

- Exception hierarchy: `KilntainersError` is base, `BackendError` and `SandboxDiedError` inherit correctly
- Exception messages are preserved

---

### Unit Tests for Types (`src/kilntainers/backends/test_base.py`)

**Reference:** backend_abstraction.md §11

**ExecRequest validation:**
- Both `command` and `args` provided → `ValueError`
- Neither `command` nor `args` provided → `ValueError`
- `working_directory` is relative → `ValueError`
- `working_directory` is absolute → valid
- `timeout` < 1 → `ValueError`
- `timeout` >= 1 → valid
- `output_limit` < 1 → `ValueError`
- `output_limit` >= 1 → valid
- Valid requests with `command` only, `args` only, with/without optional fields → success

**ExecResult:**
- All fields populated correctly
- Frozen (immutable) → assignment raises `FrozenInstanceError`

**Mount:**
- Construction succeeds

**Backend ABC behavior** (using stub subclass):
- `validate()` calls `_validate()` on first call, caches on subsequent calls
- `create_sandbox()` auto-calls `validate()` if not already validated
- `create_sandbox()` does not re-validate if already validated
- `create_sandbox(mounts=[...])` raises `NotImplementedError`
- `create_sandbox(mounts=None)` succeeds
- `tool_instructions()` returns value from subclass

**Sandbox context manager** (using stub):
- `async with sandbox:` calls `stop()` on normal exit
- `async with sandbox:` calls `stop()` on exception
- `stop()` is idempotent (double-stop doesn't error)

---

### Unit Tests for Config (`src/kilntainers/test_config.py`)

**Reference:** cli_and_startup.md §9.2

- **Frozen:** Assignment to fields raises `FrozenInstanceError`
- **Defaults:** Default construction produces expected values
- **kw_only:** Positional construction raises `TypeError`
- **DockerBackendConfig.docker_run_flags default:** Empty list

---

### Mock Sandbox/Backend for Future Testing

Create mock implementations in `src/kilntainers/backends/test_utils.py` for use in Phase 4 (MCP server) tests.

**Reference:** backend_abstraction.md §11

```python
class MockSandbox(Sandbox):
    """Sandbox that returns preconfigured ExecResults."""
    # Implementation with configurable responses

class MockBackend(Backend):
    """Backend that returns MockSandbox instances."""
    # Minimal implementation for testing
```

---

## Implementation Notes

1. **Order matters:** Implement in step order. Each step depends on the previous one.

2. **Imports to add:**
   - `errors.py`: No imports needed
   - `base.py`: `from abc import ABC, abstractmethod`, `from dataclasses import dataclass`, `from types import TracebackType`, `from ..errors import KilntainersError, BackendError, SandboxDiedError`
   - `config.py`: `from dataclasses import dataclass, field`
   - `backends/__init__.py`: `from .base import Backend, Sandbox`, stub import of DockerBackend

3. **DockerBackend stub:** Since `DockerBackend` won't be implemented until Phase 3, create a minimal stub in `backends/docker.py` that:
   - Inherits from `Backend`
   - Implements all abstract methods with `raise NotImplementedError`
   - Has a `__init__` accepting `DockerBackendConfig`

4. **Test naming:** Tests are co-located beside the files they test with a `test_` prefix (e.g., `errors.py` → `test_errors.py`, `backends/base.py` → `backends/test_base.py`).

5. **All tests must pass** before marking this phase complete. Run `uv run ./checks.sh` to verify.

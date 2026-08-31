# Architecture: WASM Backend Implementation

Defines how the WASM backend implements the Backend and Sandbox ABCs from Phase 2 using the wasmtime Python package. Covers in-process WASM execution, entry-point-based backend discovery, the GoBusyBox subclass with a bundled binary, filesystem model, timeout enforcement via epoch interruption, and resource configuration.

**References:** Functional spec §5, §8.5, §9. Phase 2 (backend abstraction). [wasmtime-py](https://github.com/bytecodealliance/wasmtime-py). [go-busybox](https://github.com/rcarmo/go-busybox).

---

## 1. Overview

The WASM backend consists of three classes in `src/kilntainers/backends/wasm.py`:

- **`WasmBackend(Backend)`** — Validates wasmtime availability, compiles the WASM module, manages the epoch ticker thread, creates sandboxes, and provides tool description text.
- **`WasmSandbox(Sandbox)`** — Wraps a temporary directory that serves as the sandbox filesystem. Handles command execution by creating a fresh wasmtime Store per exec call, with timeout enforcement via epoch-based interruption and file-based I/O capture.
- **`GoBusyBoxBackend(WasmBackend)`** — Subclass that bundles the [go-busybox](https://github.com/rcarmo/go-busybox) `busybox.wasm` binary, sets shell defaults, and provides a specialized tool description.

All WASM execution happens **in-process** via the `wasmtime` Python package. Unlike the Docker backend (subprocess to CLI) or the Modal backend (SDK API calls to a cloud service), the WASM backend loads and runs WebAssembly modules directly in the Python process using wasmtime's embedding API. No subprocess management is involved for WASM execution.

**Key differences from Docker and Modal:**

| Concern | Docker | Modal | WASM |
|---|---|---|---|
| Interface | CLI subprocess | Python SDK (remote API) | Python SDK (in-process) |
| Execution location | Local container | Remote cloud VM | In-process (same host) |
| Isolation mechanism | Linux namespaces/cgroups | Cloud container | WASM memory sandbox |
| Filesystem | Full Linux FS | Full Linux FS | Preopened tmp directory only |
| Shell | Always available (bash/sh) | Always available (bash/sh) | Only if WASM module provides one |
| Keep-alive process | `tail -f /dev/null` | Not needed (Modal manages) | Not needed (no persistent process) |
| Death detection | `docker wait` | `sb.wait.aio()` | Not applicable (no persistent process) |
| Timeout enforcement | Client-side (`asyncio.wait_for`) | Server-side + client safety net | Epoch-based interruption (in-process) |
| Network | `--network none` | `block_network=True` | WASI-level (limited by WASI preview support) |
| Install | Docker daemon required | Modal account + SDK | `uv add kilntainers[wasm]` |

This backend also introduces **entry-point-based backend discovery** (§2), replacing the hard-coded backend registry. This enables the WASM backend to be an optional dependency while remaining discoverable by the CLI.

---

## 2. Entry Point Backend Discovery

### 2.1 Motivation

The current backend registry in `backends/__init__.py` hard-imports all backend classes at module load time. This requires all backend dependencies to be installed — adding `wasmtime` as a required dependency would bloat the base install for users who only need Docker.

Entry-point-based discovery solves this: each backend registers itself via `[project.entry-points]` metadata in `pyproject.toml`. The registry discovers backends at runtime using `importlib.metadata`, and dependencies are only loaded when a backend is actually selected.

### 2.2 Entry Point Registration

In `pyproject.toml`:

```toml
[project.entry-points."kilntainers.backends"]
docker = "kilntainers.backends.docker:DockerBackend"
modal = "kilntainers.backends.modal:ModalBackend"
wasm = "kilntainers.backends.wasm:WasmBackend"
go_busybox = "kilntainers.backends.wasm:GoBusyBoxBackend"
```

Each entry maps a `--backend` CLI name to a `module:ClassName` reference. The entry points are resolved lazily — the module is only imported when the backend is selected or its CLI arguments are needed.

### 2.3 Discovery Implementation

Replace the hard-coded `BACKEND_REGISTRY` in `backends/__init__.py`:

```python
"""Backend implementations and registry."""

import importlib.metadata

from kilntainers.backends.base import Backend
from kilntainers.errors import BackendError


def _discover_entry_points() -> dict[str, importlib.metadata.EntryPoint]:
    """Discover registered backends via entry points.

    Returns a dict mapping backend names to their EntryPoint objects.
    Entry points are not loaded (imported) at discovery time.
    """
    eps = importlib.metadata.entry_points(group="kilntainers.backends")
    return {ep.name: ep for ep in eps}


# Discovered at import time (fast — no imports, just metadata scan)
_ENTRY_POINTS = _discover_entry_points()


def get_backend_class(name: str) -> type[Backend]:
    """Look up and load a backend class by name.

    Raises KeyError if the backend name is not registered.
    Raises BackendError if the backend's dependencies are not installed.
    """
    if name not in _ENTRY_POINTS:
        available = ", ".join(sorted(_ENTRY_POINTS.keys()))
        raise KeyError(
            f"Unknown backend: '{name}'. Available backends: {available}"
        )

    ep = _ENTRY_POINTS[name]
    try:
        cls = ep.load()
    except ImportError:
        raise BackendError(
            f"Backend '{name}' requires additional dependencies. "
            f"Install with: uv add kilntainers[wasm]"
        )

    if not (isinstance(cls, type) and issubclass(cls, Backend)):
        raise BackendError(
            f"Entry point '{name}' does not point to a Backend subclass"
        )

    return cls


def get_available_backend_names() -> list[str]:
    """Return names of all discovered backends (for --backend choices)."""
    return sorted(_ENTRY_POINTS.keys())
```

**Design notes:**

- **`_discover_entry_points()`** runs at import time but is fast — it reads installed package metadata without importing any backend modules.
- **`get_backend_class()`** lazy-loads the entry point on first access. The `ep.load()` call imports the module and resolves the class. If the module's dependencies are missing (e.g., `wasmtime` not installed), `ImportError` is caught and converted to an actionable `BackendError`.
- **Type checking** after load ensures the entry point actually points to a `Backend` subclass. This catches misconfigured entry points early.
- **Error messages** guide the user to install the right extra. For WASM backends, the message says `uv add kilntainers[wasm]`.

### 2.4 CLI Integration

The CLI (`cli.py`) changes to use the dynamic registry:

```python
from kilntainers.backends import get_available_backend_names, get_backend_class

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(...)

    core = parser.add_argument_group("core options")
    core.add_argument(
        "--backend",
        default="docker",
        choices=get_available_backend_names(),
        help="Backend to use (default: docker)",
    )
    # ... other core args ...

    # Backend-specific parameters (from entry points)
    for name in get_available_backend_names():
        try:
            backend_cls = get_backend_class(name)
            group = parser.add_argument_group(f"{name} backend options")
            backend_cls.add_cli_arguments(group)
        except BackendError:
            # Dependencies not installed — skip this backend's CLI args.
            # The backend name still appears in --backend choices so the
            # user can discover it, and gets a clear install message if selected.
            pass
```

**Behavior when wasmtime is not installed:**

- `--backend` choices still include `wasm` and `go_busybox` (visible in `--help`).
- WASM-specific CLI args (`--wasm-path`, etc.) are not shown (their backend class can't be loaded).
- Selecting `--backend wasm` produces: `"Backend 'wasm' requires additional dependencies. Install with: uv add kilntainers[wasm]"`.

### 2.5 Migration of Existing Backends

Docker and Modal backends move from hard-coded imports to entry point registration. The only code change is in `backends/__init__.py` (replace the hard-coded dict with discovery logic) and `pyproject.toml` (add entry points). The backend classes themselves are unchanged.

**Note:** The `modal` package should also move to `[project.optional-dependencies]` as part of this change, making it installable via `uv add kilntainers[modal]`. This is a separate task but naturally follows from the entry point architecture.

---

## 3. Configuration

The WASM backend receives its configuration as a typed dataclass.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class WasmBackendConfig(BackendConfig):
    """Configuration for the WASM backend.

    Populated from CLI args by WasmBackend.config_from_args().
    """
    # Module
    wasm_path: str                               # --wasm-path (required)

    # Shell for command mode
    shell: str | None = None                     # --shell (e.g., "ash")

    # Network
    network_enabled: bool = False                # --network

    # Resource limits
    max_memory_mib: int = 256                    # --wasm-max-memory (MiB)
    fuel: int | None = None                      # --wasm-fuel (instruction limit)

    # Epoch ticker
    epoch_tick_interval_ms: int = 1000           # internal: tick every 1s

    # Tool description
    default_timeout: int = 120                   # --timeout (for tool description)
```

**Notes:**

- `wasm_path` is the path to the `.wasm` file. Required for the base `WasmBackend`. `GoBusyBoxBackend` sets this automatically to the bundled binary.
- `shell` is the shell command name within the WASM module (e.g., `"ash"` for go-busybox). When set, command mode wraps with `[shell, "-c", command]` in argv. When `None`, command mode returns an error. This follows the same semantics as Docker/Modal's `--shell` — the backend appends `-c` implicitly.
- `max_memory_mib` caps the WASM linear memory. Default 256 MiB is generous for CLI tools. wasmtime converts this to pages internally (1 page = 64 KiB).
- `fuel` is wasmtime's instruction-counting mechanism. When set, the WASM execution traps after consuming this many fuel units. `None` means no fuel limit (timeout is the primary execution bound). Useful as a defense-in-depth against CPU-intensive loops that produce no output.
- `epoch_tick_interval_ms` controls how often the epoch ticker thread increments the epoch. 1000ms (1 second) gives ±1s timeout precision. Not exposed as a CLI arg — implementation detail.
- `default_timeout` is used only for the tool description text (same pattern as Docker/Modal).

### 3.1 CLI Argument Registration

```python
@classmethod
def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
    group.add_argument(
        "--wasm-path",
        default=None,
        help="Path to the .wasm file to execute (required for wasm backend)",
    )
    group.add_argument(
        "--shell",
        default=None,
        help=(
            "Shell command within the WASM module for command mode "
            "(e.g., 'ash'). When not set, only args mode is supported."
        ),
    )
    group.add_argument(
        "--wasm-max-memory",
        type=int,
        default=256,
        help="Max WASM memory in MiB (default: 256)",
    )
    group.add_argument(
        "--wasm-fuel",
        type=int,
        default=None,
        help="WASM instruction fuel limit (default: unlimited)",
    )
```

**Note on `--shell`:** This argument name is shared with Docker and Modal backends. Since all backends register their args in separate groups, and only the selected backend's `config_from_args()` reads the value, the shared name works correctly. The semantics are consistent: "shell to use for command mode." Docker defaults to `/bin/bash`, Modal to `/bin/bash`, WASM to `None` (no shell by default). GoBusyBox overrides the default to `ash`.

If argument name collision causes issues with argparse (multiple registrations of `--shell`), the implementation should promote `--shell` to a core parameter. This is an implementation detail — the architecture is the same either way.

---

## 4. Wasmtime Python SDK Interaction

### 4.1 Key API Surface

The WASM backend uses these wasmtime-py types:

| Type | Purpose |
|---|---|
| `wasmtime.Config` | Engine configuration (epoch interruption, memory limits) |
| `wasmtime.Engine` | Compilation engine (shared across all execs) |
| `wasmtime.Module` | Compiled WASM module (compiled once, reused) |
| `wasmtime.Store` | Execution state (one per exec call) |
| `wasmtime.Linker` | Links WASI imports to the module |
| `wasmtime.WasiConfig` | WASI configuration (argv, preopened dirs, I/O files) |

### 4.2 Execution Model

Each exec call creates a **fresh Store, WasiConfig, and instance**. The Engine, compiled Module, and Linker are shared across all exec calls (and even across sandboxes from the same backend). This split between fresh-per-exec and shared objects is a hard constraint of wasmtime's execution model, not a conservative design choice:

**Why Store must be fresh per exec:**

- A WASM module's `_start` function is an entry point designed to be called **exactly once**. It initializes memory, runs the program, and either exits normally or traps. There is no "reset" mechanism to return a Store to its initial state after `_start` completes.
- After execution, the Store's linear memory, globals, and WASI file descriptors are in a consumed/mutated state. Re-running `_start` on the same instance would produce undefined behavior.
- If execution traps (timeout, fuel, runtime error), the Store is in an indeterminate state and cannot be reused.
- `WasiConfig` can only be set once per Store (via `store.set_wasi(config)`), and each exec needs different argv, stdin, stdout/stderr paths, and epoch deadlines.

**What is shared (immutable after creation):**

- **Engine** — compilation configuration and epoch counter. Created once during validation. Thread-safe.
- **Module** — compiled WASM code. The expensive compilation step (parsing, validation, code generation) happens once. The Module is immutable and safe to share across threads.
- **Linker** — defines available WASI imports. Created once, reused to instantiate fresh stores.

**Performance:** Store creation and module instantiation are cheap (microseconds). Module compilation is expensive (milliseconds to seconds depending on module size). The shared-Module-fresh-Store pattern gives near-zero per-exec overhead while maintaining clean isolation between calls.

### 4.3 Synchronous Execution

WASM execution via wasmtime-py is **synchronous** — calling the module's `_start` function blocks the calling thread until the WASM program exits (or traps). Since the Kilntainers server runs in an asyncio event loop, WASM execution must be offloaded to a thread:

```python
result = await asyncio.to_thread(self._run_wasm_sync, store, instance)
```

The `asyncio.to_thread` call runs the blocking WASM execution in a separate thread while keeping the event loop responsive for concurrent operations (death detection tasks, other sandbox execs in HTTP mode).

### 4.4 Epoch-Based Interruption

wasmtime provides exactly two mechanisms for interrupting execution: **fuel** (deterministic instruction counting, ~20-30% overhead) and **epochs** (wall-time-based, ~10% overhead). There is no `store.set_timeout(seconds)` API. We use epochs because wall-time timeout semantics match the functional spec's behavior, and the lower overhead is better for CLI workloads.

The epoch mechanism works as follows:

1. **Engine configuration:** `config.epoch_interruption = True` enables epoch tracking.
2. **Store deadline:** `store.set_epoch_deadline(N)` means "trap after N more epoch increments from now." Each Store tracks its deadline independently, even when sharing an Engine.
3. **Epoch ticker:** A background thread calls `engine.increment_epoch()` at regular intervals (default: every 1 second). This is the [recommended pattern from wasmtime's documentation](https://docs.wasmtime.dev/examples-interrupting-wasm.html).
4. **Trap:** When the deadline is reached, wasmtime raises a `Trap` at the next yield point (function calls, loop back-edges). This gives roughly ±1 tick-interval precision.

**Example:** With a 1-second tick interval and `timeout=120`, set `epoch_deadline = 120`. After ~120 seconds (±1s), the WASM execution traps.

**Multi-sandbox correctness:** Multiple sandboxes can share the same Engine and ticker thread. Each Store independently tracks how many ticks have elapsed since its `set_epoch_deadline` call. If sandbox A has `deadline=120` and sandbox B has `deadline=5`, after 5 ticks only B traps — A keeps running.

**Known limitation — blocking WASI host calls:** Epoch checks only fire when entering WASM functions and at loop back-edges, **not during blocking WASI host calls**. If a WASM program calls a WASI function that blocks (e.g., `clock_nanosleep` for `sleep`), the epoch trap fires only after the host call completes. This is a [known wasmtime limitation](https://github.com/bytecodealliance/wasmtime/issues/9188). The exec flow handles this with a dual timeout mechanism: epoch interruption as the primary mechanism, and an `asyncio.wait_for` safety net at `timeout + 1s` that catches the blocking-WASI-call case (see §6.3).

---

## 5. WasmBackend

### 5.1 Construction and State

```python
class WasmBackend(Backend):
    def __init__(self, config: WasmBackendConfig) -> None:
        super().__init__()
        self._config = config
        self._engine: wasmtime.Engine | None = None
        self._module: wasmtime.Module | None = None
        self._linker: wasmtime.Linker | None = None
        self._epoch_ticker_stop: threading.Event | None = None
        self._epoch_ticker_thread: threading.Thread | None = None
```

The Engine, Module, and Linker are created during validation and shared across all sandboxes. The epoch ticker thread starts during validation and runs for the backend's lifetime.

### 5.2 Validation (`_validate`)

```python
async def _validate(self) -> None:
    # 1. Check wasmtime is importable
    if wasmtime is None:
        raise BackendError(
            "WASM backend requires the 'wasmtime' package. "
            "Install with: uv add kilntainers[wasm]"
        )

    # 2. Check .wasm file exists
    if not os.path.isfile(self._config.wasm_path):
        raise BackendError(
            f"WASM file not found: '{self._config.wasm_path}'"
        )

    # 3. Create engine with epoch interruption
    config = wasmtime.Config()
    config.epoch_interruption = True
    self._engine = wasmtime.Engine(config)

    # 4. Compile module (expensive — cached for all future execs)
    try:
        self._module = wasmtime.Module.from_file(
            self._engine, self._config.wasm_path
        )
    except wasmtime.WasmtimeError as e:
        raise BackendError(
            f"Failed to compile WASM module '{self._config.wasm_path}': {e}"
        )

    # 5. Create linker with WASI
    self._linker = wasmtime.Linker(self._engine)
    self._linker.define_wasi()

    # 6. Start epoch ticker thread
    self._epoch_ticker_stop = threading.Event()
    self._epoch_ticker_thread = threading.Thread(
        target=self._epoch_ticker_loop,
        daemon=True,
    )
    self._epoch_ticker_thread.start()
```

**What this validates:**

- The `wasmtime` Python package is installed and importable.
- The `.wasm` file exists and is a valid WebAssembly module (compilation succeeds).
- The engine and linker are functional.

**What this does NOT validate:**

- Whether the WASM module's WASI imports are satisfiable (checked at instantiation time during exec).
- Whether the configured shell exists in the module (checked during readiness verification).

### 5.3 Epoch Ticker Thread

```python
def _epoch_ticker_loop(self) -> None:
    """Background thread that increments the engine epoch at regular intervals."""
    interval_s = self._config.epoch_tick_interval_ms / 1000.0
    while not self._epoch_ticker_stop.is_set():
        self._epoch_ticker_stop.wait(interval_s)
        if not self._epoch_ticker_stop.is_set():
            self._engine.increment_epoch()
```

The ticker runs as a daemon thread for the lifetime of the backend. It increments the epoch every `epoch_tick_interval_ms` (default: 1000ms). When the backend is no longer needed, `self._epoch_ticker_stop.set()` signals the thread to exit.

**Thread safety:** `engine.increment_epoch()` is thread-safe in wasmtime — it's designed to be called from a different thread than the one running WASM execution. The epoch counter is an atomic integer.

### 5.4 Sandbox Creation (`_create_sandbox`)

```python
async def _create_sandbox(self) -> "WasmSandbox":
    # 1. Create temporary directory
    tmp_dir = tempfile.mkdtemp(prefix="kilntainers-wasm-")

    # 2. Create sandbox object
    sandbox = WasmSandbox(
        engine=self._engine,
        module=self._module,
        linker=self._linker,
        tmp_dir=tmp_dir,
        config=self._config,
    )

    # 3. Verify readiness
    try:
        await sandbox._verify_readiness()
    except Exception:
        await sandbox.stop()
        raise

    return sandbox
```

**Key differences from Docker/Modal:**

- **No image pull.** The WASM module is already compiled during validation.
- **No container creation.** The "sandbox" is a local temp directory + shared WASM engine/module.
- **Instant startup.** Creating a temp directory and verifying readiness takes milliseconds, not seconds.

#### 5.4.1 Readiness Verification

Readiness verification confirms the WASM module can be instantiated and run. The approach depends on whether a shell is configured:

```python
async def _verify_readiness(self) -> None:
    """Verify the WASM module can execute commands."""
    if self._config.shell is not None:
        # Verify shell works (command mode)
        result = await self.exec(ExecRequest(
            command="echo kilntainers-ready",
            timeout=10,
            output_limit=4096,
        ))
        if "kilntainers-ready" not in result.stdout:
            raise BackendError(
                f"WASM readiness check failed. Shell '{self._config.shell}' "
                f"may not be available in the module."
            )
    else:
        # No shell — verify module can be instantiated at all.
        # Run a minimal exec with args mode using a command that
        # should be available in any WASI module.
        try:
            result = await self.exec(ExecRequest(
                args=["--help"],
                timeout=10,
                output_limit=4096,
            ))
            # Any exit code is fine — we just need instantiation to succeed
        except BackendError as e:
            raise BackendError(
                f"WASM readiness check failed: {e}"
            )
```

### 5.5 Tool Instructions

```python
def tool_instructions(self) -> str | None:
    # General WasmBackend cannot describe an arbitrary .wasm module
    return None
```

The base `WasmBackend` returns `None` — it cannot describe an arbitrary WASM module's capabilities. The server requires `--tool-instruction-override` (Functional spec §6 rule 3). Subclasses like `GoBusyBoxBackend` override this to provide a meaningful description (§8.3).

---

## 6. WasmSandbox

### 6.1 State and Construction

```python
class WasmSandbox(Sandbox):
    def __init__(
        self,
        *,
        engine: wasmtime.Engine,
        module: wasmtime.Module,
        linker: wasmtime.Linker,
        tmp_dir: str,
        config: WasmBackendConfig,
    ) -> None:
        self._engine = engine
        self._module = module
        self._linker = linker
        self._tmp_dir = tmp_dir
        self._config = config
        self._stopped = False
        self._exec_lock = asyncio.Lock()
        self._sandbox_id = os.path.basename(tmp_dir)

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id
```

**State fields:**

| Field | Purpose |
|---|---|
| `_engine` | Shared wasmtime Engine (with epoch interruption enabled). |
| `_module` | Compiled WASM module (shared across all execs). |
| `_linker` | Linker with WASI imports defined. |
| `_tmp_dir` | Host-side temp directory — the sandbox's filesystem. Preopened as `.` in WASM. |
| `_config` | Backend config (shell, resource limits, etc.). |
| `_stopped` | Idempotency guard for `stop()`. |
| `_exec_lock` | Serializes exec calls within this sandbox (D29). |
| `_sandbox_id` | The temp directory basename, used for identification. |

**No `_stop_requested` flag:** Unlike Docker/Modal, WASM has no long-running process that could die unexpectedly. There is no race between stop and death detection (§6.7).

### 6.2 Command Execution

```python
async def exec(self, request: ExecRequest) -> ExecResult:
    if self._stopped:
        raise SandboxDiedError("Sandbox has been stopped")

    async with self._exec_lock:
        return await self._do_exec(request)
```

Same pattern as Docker/Modal — check liveness, acquire the serialization lock, delegate.

#### 6.2.1 Core Exec Flow

```python
async def _do_exec(self, request: ExecRequest) -> ExecResult:
    argv = self._build_argv(request)

    # Create temp files for I/O capture
    stdout_path = os.path.join(self._tmp_dir, ".kilntainers_stdout")
    stderr_path = os.path.join(self._tmp_dir, ".kilntainers_stderr")
    stdin_path = None

    if request.stdin is not None:
        stdin_path = os.path.join(self._tmp_dir, ".kilntainers_stdin")
        with open(stdin_path, "w", encoding="utf-8") as f:
            f.write(request.stdin)

    start_time = time.monotonic()

    try:
        # Dual timeout: epoch interruption (primary, in-thread) +
        # asyncio.wait_for (safety net for blocking WASI calls).
        exit_code = await asyncio.wait_for(
            asyncio.to_thread(
                self._run_wasm_sync,
                argv=argv,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                stdin_path=stdin_path,
                timeout=request.timeout,
                working_directory=request.working_directory,
            ),
            timeout=request.timeout + 1,  # safety net: 1s after epoch should fire
        )
    except (_WasmTimeout, asyncio.TimeoutError):
        # _WasmTimeout: epoch fired inside WASM (normal timeout path)
        # asyncio.TimeoutError: epoch didn't fire in time (blocking WASI call)
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return ExecResult(
            stdout="",
            stderr=f"[kilntainers: command timed out after {request.timeout}s]",
            exit_code=124,
            exec_duration_ms=elapsed_ms,
        )

    elapsed_ms = int((time.monotonic() - start_time) * 1000)

    # Read output files
    stdout_str = self._read_output_file(stdout_path)
    stderr_str = self._read_output_file(stderr_path)

    # Check output limit (post-execution)
    combined_bytes = len(stdout_str.encode("utf-8")) + len(stderr_str.encode("utf-8"))
    if combined_bytes > request.output_limit:
        return ExecResult(
            stdout="",
            stderr=(
                f"[kilntainers: output limit exceeded "
                f"({request.output_limit} bytes). Command terminated. "
                f"No output returned. Re-run with head, tail, or grep "
                f"to manage output size.]"
            ),
            exit_code=1,
            exec_duration_ms=elapsed_ms,
        )

    return ExecResult(
        stdout=stdout_str,
        stderr=stderr_str,
        exit_code=exit_code,
        exec_duration_ms=elapsed_ms,
    )
```

**Key design choices:**

- **`asyncio.to_thread`** offloads the synchronous WASM execution to a thread, keeping the asyncio event loop responsive.
- **Dual timeout** — Two layers of timeout enforcement work together:
  - **Epoch interruption (primary):** The epoch ticker fires inside the WASM execution at yield points (function calls, loop back-edges). This handles 99% of cases cleanly — the thread returns promptly with a `_WasmTimeout` exception.
  - **`asyncio.wait_for` (safety net):** Set to `timeout + 1s`, catches the case where a blocking WASI host call (e.g., `sleep 9999`) prevents the epoch from firing. If the epoch hasn't fired after timeout+1s, `asyncio.wait_for` times out. The thread may continue briefly in the background but the exec returns immediately with the timeout result.
- **File-based I/O** — stdout, stderr, and stdin are redirected through temp files in the sandbox directory. These files use a `.kilntainers_` prefix to avoid collision with user files.
- **Output limit is checked post-execution** (see §6.4 for rationale).
- **`_WasmTimeout`** is a private exception raised by `_run_wasm_sync` when the epoch deadline fires.

#### 6.2.2 Synchronous WASM Execution

```python
class _WasmTimeout(Exception):
    """Internal signal: WASM execution hit epoch deadline."""
    pass


def _run_wasm_sync(
    self,
    *,
    argv: list[str],
    stdout_path: str,
    stderr_path: str,
    stdin_path: str | None,
    timeout: int,
    working_directory: str | None,
) -> int:
    """Run the WASM module synchronously. Called from a thread.

    Returns the exit code. Raises _WasmTimeout on epoch deadline.
    """
    # 1. Configure WASI
    wasi_config = wasmtime.WasiConfig()
    wasi_config.argv = tuple(argv)
    wasi_config.stdout_file = stdout_path
    wasi_config.stderr_file = stderr_path
    if stdin_path is not None:
        wasi_config.stdin_file = stdin_path

    # 2. Preopen the sandbox directory
    if working_directory is not None:
        # Resolve working_directory relative to tmp_dir
        host_dir = os.path.join(self._tmp_dir, working_directory.lstrip("/"))
        os.makedirs(host_dir, exist_ok=True)
        wasi_config.preopen_dir(host_dir, ".")
    else:
        wasi_config.preopen_dir(self._tmp_dir, ".")

    # Always preopen full tmp_dir so the module can access all sandbox files
    wasi_config.preopen_dir(self._tmp_dir, "/sandbox")

    # 3. Create store with epoch deadline and resource limits
    store = wasmtime.Store(self._engine)
    store.set_wasi(wasi_config)

    # Epoch deadline: timeout seconds * (1000ms / tick_interval_ms) ticks
    ticks_per_second = 1000 // self._config.epoch_tick_interval_ms
    store.set_epoch_deadline(timeout * ticks_per_second)

    # Fuel limit (optional)
    if self._config.fuel is not None:
        store.set_fuel(self._config.fuel)

    # Memory limit
    # Wasmtime enforces memory limits via the module's declared memory
    # or via Store limits. The Store-level limit is set here.
    store.set_limits(
        memory_size=self._config.max_memory_mib * 1024 * 1024,
    )

    # 4. Instantiate and run
    try:
        instance = self._linker.instantiate(store, self._module)
        start_func = instance.exports(store).get("_start")
        if start_func is None:
            raise BackendError(
                "WASM module does not export a '_start' function. "
                "Only WASI command modules are supported."
            )
        start_func(store)
        return 0  # Normal exit (no proc_exit call)

    except wasmtime.ExitTrap as e:
        return e.code  # WASI proc_exit() was called

    except wasmtime.Trap as e:
        trap_msg = str(e)
        if "epoch" in trap_msg.lower() or "interrupt" in trap_msg.lower():
            raise _WasmTimeout()
        if "fuel" in trap_msg.lower():
            # Fuel exhaustion — treat as timeout-like condition
            raise _WasmTimeout()
        # Other traps (unreachable, memory access, etc.)
        # Write trap info to stderr file and return exit code 1
        with open(stderr_path, "a", encoding="utf-8") as f:
            f.write(f"[kilntainers: WASM trap: {trap_msg}]\n")
        return 1
```

**Design notes:**

- **Dual preopen:** The sandbox directory is preopened twice — as `.` (current directory, or working directory if specified) and as `/sandbox` (absolute reference to the full sandbox root). This lets WASM programs use both relative and absolute-style paths.
- **`_start` export:** WASI command modules export `_start` as their entry point. Non-WASI modules (libraries, reactors) don't have `_start` and are not supported.
- **`ExitTrap`** is raised when the WASM program calls `proc_exit(code)`. This is the normal exit path — we extract the exit code.
- **`Trap`** is raised for epoch deadline (timeout), fuel exhaustion, or runtime errors (unreachable instruction, out-of-bounds memory access). We inspect the trap message to distinguish timeout from other traps.
- **Fuel exhaustion** is treated as a timeout-like condition (returns exit code 124 with timeout message). This matches the user-facing behavior — the command was forcibly stopped.

#### 6.2.3 Command Construction

```python
def _build_argv(self, request: ExecRequest) -> list[str]:
    """Build the WASI argv for this exec call."""
    # argv[0] is the module name (derived from filename)
    module_name = os.path.splitext(
        os.path.basename(self._config.wasm_path)
    )[0]

    if request.command is not None:
        # Command mode — requires shell
        if self._config.shell is None:
            raise BackendError(
                "This WASM backend does not support command mode "
                "(no shell configured). Use args mode instead, which "
                "passes arguments directly to the WASM module. "
                "Example: args=['ls', '-la'] instead of command='ls -la'. "
                "To enable command mode, configure --shell with the "
                "shell available in the WASM module (e.g., --shell ash)."
            )
        return [module_name, self._config.shell, "-c", request.command]
    else:
        # Args mode — pass directly
        assert request.args is not None
        return [module_name, *request.args]
```

**Examples of constructed argv:**

| Backend | Request | WASI argv |
|---|---|---|
| WasmBackend (shell=None) | `args=["echo", "hello"]` | `["mymodule", "echo", "hello"]` |
| WasmBackend (shell="ash") | `command="echo hello"` | `["mymodule", "ash", "-c", "echo hello"]` |
| GoBusyBox | `args=["ls", "-la"]` | `["busybox", "ls", "-la"]` |
| GoBusyBox | `command="echo hi \| grep hi"` | `["busybox", "ash", "-c", "echo hi \| grep hi"]` |
| WasmBackend (shell=None) | `command="ls"` | **Error:** command mode not supported |

**argv[0]** is the stem of the `.wasm` filename (e.g., `"busybox"` from `busybox.wasm`). For multicall binaries like go-busybox, this is important — the binary uses argv[0] to determine its identity.

### 6.3 Timeout Enforcement

Timeout is enforced via a **dual mechanism**: epoch interruption as the primary (in-thread) mechanism, and `asyncio.wait_for` as a safety net.

**Primary: Epoch interruption (handles most cases)**

1. The backend's epoch ticker thread increments the engine epoch every `epoch_tick_interval_ms` (default: 1000ms).
2. Each exec call sets `store.set_epoch_deadline(timeout * ticks_per_second)` on a fresh store.
3. During WASM execution, wasmtime checks the epoch counter at yield points (function calls, loop back-edges).
4. When the deadline is reached, wasmtime raises a `Trap`.
5. `_run_wasm_sync` catches the trap and raises `_WasmTimeout`.
6. `_do_exec` catches `_WasmTimeout` and returns the timeout ExecResult (exit code 124, no output).

This handles the vast majority of cases. The thread returns promptly when the epoch fires, the exec lock is released cleanly, and resources are freed.

**Safety net: `asyncio.wait_for` (handles blocking WASI calls)**

As noted in §4.4, epoch checks only fire at WASM yield points — not during blocking WASI host calls. A command like `sleep 9999` calls WASI's `clock_nanosleep`, which blocks in host code where epochs don't apply. Without a safety net, the exec would hang for the full sleep duration.

The `asyncio.wait_for` wrapper on `asyncio.to_thread` is set to `timeout + 1s`:

```python
exit_code = await asyncio.wait_for(
    asyncio.to_thread(self._run_wasm_sync, ...),
    timeout=request.timeout + 1,
)
```

If the epoch fires (normal case), the thread returns before the asyncio timeout. If a blocking WASI call prevents the epoch from firing, the asyncio timeout fires 1 second later. The `asyncio.TimeoutError` is caught alongside `_WasmTimeout` and produces the same timeout ExecResult.

**When `asyncio.wait_for` fires, the thread may still be running briefly** (the blocking WASI call hasn't returned). This is acceptable because:

- The thread is a daemon thread that will be cleaned up when the process exits.
- The blocking WASI call will eventually complete (WASI calls aren't infinite — they're bounded by the host OS).
- The `_exec_lock` is released by the `async with` block in the caller, so subsequent execs can proceed once the thread finishes.
- In practice, the only blocking WASI call in go-busybox is `sleep`, and the orphaned thread will complete when the sleep finishes.

**Precision:** With the default 1-second epoch tick interval, normal timeouts are accurate to ±1 second. The safety net adds at most 1 additional second. This is acceptable for the use case (typical timeouts are 120 seconds).

**Comparison with Docker/Modal:** Docker uses `asyncio.wait_for` on the subprocess. Modal uses server-side timeout + client-side safety net. The WASM approach is similar to Modal's dual pattern: the in-process epoch mechanism is the primary enforcer, and the asyncio timeout is the safety net.

### 6.4 Output Limit Enforcement

Output limit is enforced **post-execution** by checking the size of stdout and stderr files after the WASM program finishes (or traps).

```python
# After execution completes:
stdout_str = self._read_output_file(stdout_path)
stderr_str = self._read_output_file(stderr_path)

combined_bytes = len(stdout_str.encode("utf-8")) + len(stderr_str.encode("utf-8"))
if combined_bytes > request.output_limit:
    return ExecResult(
        stdout="",
        stderr="[kilntainers: output limit exceeded ...]",
        exit_code=1,
        exec_duration_ms=elapsed_ms,
    )
```

**Why post-execution, not streaming:**

Docker and Modal monitor output byte-by-byte during execution and kill the process when the limit is exceeded. The WASM backend uses file-based I/O (wasmtime's `WasiConfig.stdout_file`), which does not provide a streaming interface. The output is written directly by the WASM runtime to the file, with no Python-level interception point.

**Risk mitigation for excessive output:**

- **Timeout** is the primary bound. A WASM program that writes endlessly will hit the epoch deadline and be killed.
- **Memory limits** cap the WASM module's own memory, though file writes bypass WASM memory (they go through WASI host calls).
- **Disk space** in the temp directory is bounded by the host's available disk. For production deployments, operators should use a filesystem with quotas or place the temp directory on a size-limited tmpfs.
- **Practical impact is low.** WASM I/O goes through WASI function calls, which are slower than native I/O. Combined with the timeout, excessive output is bounded in practice.

**Known limitation:** Unlike Docker/Modal, a WASM program can write more than `output_limit` bytes before the post-execution check catches it. The bytes are written to disk (temp directory) but never returned to the caller. This is documented as a behavioral difference from container-based backends.

### 6.5 Stdin Piping

Stdin is provided via a temp file:

```python
if request.stdin is not None:
    stdin_path = os.path.join(self._tmp_dir, ".kilntainers_stdin")
    with open(stdin_path, "w", encoding="utf-8") as f:
        f.write(request.stdin)

# In WasiConfig:
if stdin_path is not None:
    wasi_config.stdin_file = stdin_path
```

The WASM program reads from stdin normally via WASI's `fd_read` on file descriptor 0. The wasmtime runtime reads from the file we provided.

**Why temp files:** The wasmtime-py `WasiConfig` API only accepts file paths for stdin/stdout/stderr — it does not support in-memory buffers or file-like objects ([open feature request](https://github.com/bytecodealliance/wasmtime-py/issues/123)). Temp files are the standard approach for programmatic I/O with wasmtime-py.

**Stdin size limit** (2 MiB, D32) is enforced by the MCP layer before the ExecRequest reaches the backend — same as Docker/Modal.

### 6.6 Stop

```python
async def stop(self) -> None:
    if self._stopped:
        return
    self._stopped = True

    # Clean up the temporary directory
    try:
        shutil.rmtree(self._tmp_dir, ignore_errors=True)
    except Exception:
        pass  # Best-effort cleanup
```

**Design details:**

- **Idempotent** — the `_stopped` flag ensures double-stop is a no-op.
- **`shutil.rmtree`** removes the entire temp directory tree. `ignore_errors=True` ensures cleanup doesn't raise even if files are locked or missing.
- **No process to stop.** Unlike Docker (`docker stop`) or Modal (`terminate()`), there's no long-running process. If an exec is in progress when stop is called, the `_exec_lock` ensures stop waits until the exec completes. The `_stopped` flag then prevents subsequent execs.
- **No subprocess cleanup needed.** WASM runs in-process — when `_run_wasm_sync` returns (or traps), execution is fully complete.

### 6.7 Death Detection

```python
async def wait_for_death(self) -> None:
    """Block forever — WASM sandbox cannot die unexpectedly.

    There is no long-running process that could crash, no container
    that could be OOM-killed, and no remote service that could
    terminate. The sandbox is "alive" as long as the temp directory
    exists and stop() hasn't been called.
    """
    try:
        await asyncio.Future()  # Never completes
    except asyncio.CancelledError:
        return  # Normal shutdown — MCP layer cancelled this task
```

**Rationale:** Docker/Modal backends have persistent processes (containers, cloud sandboxes) that can die unexpectedly — OOM kills, external termination, daemon crashes. The WASM backend has no persistent process. Each exec is an isolated, synchronous call that either completes or traps. The sandbox's "liveness" is just the existence of the temp directory, which only goes away when `stop()` is called.

The MCP layer still starts a `wait_for_death()` task (that's the contract), but it will never resolve on its own — it's always cancelled during normal shutdown.

### 6.8 Output File Reading

```python
def _read_output_file(self, path: str) -> str:
    """Read an output file and return its contents as a string."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    finally:
        # Clean up I/O temp files after reading
        try:
            os.unlink(path)
        except OSError:
            pass
```

Output files are read with `errors="replace"` to handle binary output gracefully (same approach as Docker/Modal). The files are deleted after reading to avoid accumulation across exec calls.

---

## 7. Sandbox Environment

### 7.1 Filesystem

The sandbox's filesystem is a **host-side temp directory** preopened into the WASM environment:

- **Created** during `_create_sandbox()` via `tempfile.mkdtemp(prefix="kilntainers-wasm-")`.
- **Preopened** as `.` (current directory) in the WASM module's WASI configuration.
- **Also preopened** as `/sandbox` for absolute-path-style access.
- **Cleaned up** during `stop()` via `shutil.rmtree()`.

**What the WASM module sees:**

- `.` — the sandbox root (files persist across exec calls within a session).
- `/sandbox` — same directory, accessible by absolute path.
- No other paths are accessible. `/usr`, `/etc`, `/tmp`, etc. do not exist in the WASM namespace.

**Filesystem state between calls:** Unlike Docker/Modal where each exec is truly stateless (no filesystem changes persist unless using mounts), the WASM sandbox preserves filesystem changes between exec calls. A file created in one exec is visible in subsequent execs. This is because all execs share the same host-side temp directory. This matches the functional spec's stateless execution model for shell variables and environment, but provides filesystem persistence — similar to how Docker containers preserve filesystem state between `docker exec` calls.

### 7.2 Working Directory

The `working_directory` parameter is supported through filesystem preopening:

- **Default (no `working_directory`):** The sandbox root (`.`) is the current directory.
- **With `working_directory`:** The specified subdirectory within the sandbox becomes `.` for that exec call. The directory is created if it doesn't exist. The full sandbox root is still accessible via `/sandbox`.

```python
if working_directory is not None:
    host_dir = os.path.join(self._tmp_dir, working_directory.lstrip("/"))
    os.makedirs(host_dir, exist_ok=True)
    wasi_config.preopen_dir(host_dir, ".")
else:
    wasi_config.preopen_dir(self._tmp_dir, ".")

# Always preopen full sandbox for absolute access
wasi_config.preopen_dir(self._tmp_dir, "/sandbox")
```

**Note:** `working_directory` is treated as relative to the sandbox root, even if specified as an absolute path (leading `/` is stripped). This prevents path traversal outside the sandbox.

### 7.3 Network

Network is controlled by the `--network` flag (default: disabled).

- **Network disabled (default):** No WASI networking capabilities are provided to the module. Network-related WASI imports are not linked, so any network call from the WASM module traps.
- **Network enabled:** WASI networking imports are linked if the wasmtime Python package supports them. The exact API depends on the wasmtime-py version and WASI preview support level.

**Current limitation:** WASI preview 1 (which go-busybox targets) does not include networking. The `--network` flag is accepted for forward compatibility with WASI preview 2 modules but has no effect for WASI preview 1 modules. Network-dependent commands in go-busybox (wget, nc, dig, ss) will not work regardless of this flag.

### 7.4 Resource Limits

| CLI Parameter | wasmtime Mechanism | Default | Example |
|---|---|---|---|
| `--wasm-max-memory` | `store.set_limits(memory_size=...)` | 256 MiB | `--wasm-max-memory 512` |
| `--wasm-fuel` | `store.set_fuel(N)` | None (unlimited) | `--wasm-fuel 1000000000` |
| `--timeout` (core) | Epoch deadline | 120s | `--timeout 300` |

**Memory:** Caps the maximum WASM linear memory the module can grow to. Default 256 MiB is generous for CLI tools. If the module tries to grow memory beyond this limit, the grow operation fails (returns -1 in WASM), which typically causes the program to abort.

**Fuel:** An instruction-counting mechanism. Each WASM instruction consumes one fuel unit. When fuel runs out, execution traps. Useful as defense-in-depth against CPU-bound loops that produce no output (and thus wouldn't be caught by output limits). The default is unlimited — timeout is the primary execution bound.

---

## 8. GoBusyBoxBackend

### 8.1 Bundled Binary

The [go-busybox](https://github.com/rcarmo/go-busybox) `busybox.wasm` binary is bundled as a package data file:

```
src/kilntainers/
  backends/
    wasm.py
    wasm_data/
      __init__.py          # empty, makes it a package for importlib.resources
      busybox.wasm         # compiled go-busybox multicall binary
```

The binary is located at runtime using `importlib.resources`:

```python
import importlib.resources

def _get_bundled_busybox_path() -> str:
    """Return the path to the bundled busybox.wasm binary."""
    ref = importlib.resources.files("kilntainers.backends.wasm_data") / "busybox.wasm"
    # as_posix() works for file-system-backed resources
    return str(ref)
```

**Package data configuration** in `pyproject.toml`:

```toml
[tool.setuptools.package-data]
"kilntainers.backends.wasm_data" = ["*.wasm"]
```

Or with the `uv`/`hatch` build system, ensure the `wasm_data` directory and its contents are included in the package distribution.

**Binary size:** The go-busybox optimized WASM build targets <2 MB, which is acceptable for a pip package. The binary is included in the `kilntainers[wasm]` optional extra.

### 8.2 Subclass Design

```python
class GoBusyBoxBackend(WasmBackend):
    """WASM backend with bundled go-busybox multicall binary.

    Provides a zero-config POSIX-like sandbox with common shell
    utilities (ls, cat, grep, sed, awk, etc.) via go-busybox
    compiled to WebAssembly.

    Usage: kilntainers --backend go_busybox
    """

    def __init__(self, config: WasmBackendConfig) -> None:
        super().__init__(config)

    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        # GoBusyBox doesn't need --wasm-path (it's bundled)
        # It inherits resource limit args from WasmBackend
        group.add_argument(
            "--shell",
            default="ash",
            help="Shell within go-busybox for command mode (default: ash)",
        )
        group.add_argument(
            "--wasm-max-memory",
            type=int,
            default=256,
            help="Max WASM memory in MiB (default: 256)",
        )
        group.add_argument(
            "--wasm-fuel",
            type=int,
            default=None,
            help="WASM instruction fuel limit (default: unlimited)",
        )

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> WasmBackendConfig:
        return WasmBackendConfig(
            wasm_path=_get_bundled_busybox_path(),
            shell=args.shell,   # default: "ash"
            network_enabled=args.network,
            max_memory_mib=args.wasm_max_memory,
            fuel=args.wasm_fuel,
            default_timeout=args.timeout,
        )
```

**Key overrides:**

- **`add_cli_arguments`** does NOT register `--wasm-path` (the module is bundled). It registers `--shell` with a default of `"ash"` and the resource limit args.
- **`config_from_args`** sets `wasm_path` to the bundled binary path automatically.
- **`tool_instructions`** returns a go-busybox-specific description (§8.3).

### 8.3 Tool Instructions

```python
def tool_instructions(self) -> str | None:
    timeout = self._config.default_timeout

    return (
        "Execute a command in a lightweight POSIX sandbox powered by "
        "go-busybox (WebAssembly). Commands run in ash (POSIX shell). "
        "Only POSIX shell syntax is supported — do not use bash-specific "
        "features like arrays, [[ ]], or process substitution.\n\n"
        "Available commands: ls, cat, grep, sed, awk, sort, uniq, wc, "
        "head, tail, find, mkdir, rm, cp, mv, echo, printf, tr, cut, "
        "diff, tar, gzip, gunzip, ps, kill, xargs, sleep, time, timeout. "
        "Run 'busybox --list' for the full list.\n\n"
        "No package manager. No network access. Filesystem persists between "
        "calls within a session. Use relative paths (no /usr, /etc, etc.). "
        "To write files or pass data without shell escaping, use the stdin "
        "parameter (e.g., command='cat > file.txt' with content in stdin). "
        f"Commands time out after {timeout} seconds by default "
        "(override with the timeout parameter for long-running operations)."
    )
```

**Key points in the description:**

- Calls out POSIX shell, not bash.
- Lists available commands (derived from go-busybox's feature table).
- Warns about relative paths (WASM sandbox limitation).
- Mentions filesystem persistence between calls.
- Follows the same pattern as Docker/Modal for stdin guidance and timeout.

---

## 9. Error Handling Summary

| WASM failure | Raised as | MCP result |
|---|---|---|
| `wasmtime` not installed | `BackendError` | Server fails to start |
| `.wasm` file not found | `BackendError` | Server fails to start |
| Module compilation fails | `BackendError` | Server fails to start |
| Readiness check fails | `BackendError` | Server fails to start (stdio) or session fails (HTTP) |
| `command` mode without shell | `BackendError` | `isError: true` |
| Exec — command succeeds | `ExecResult` (exit 0) | `isError: false` |
| Exec — command fails | `ExecResult` (non-zero exit) | `isError: false` |
| Exec — epoch timeout | `ExecResult` (exit 124) | `isError: false` |
| Exec — fuel exhausted | `ExecResult` (exit 124) | `isError: false` |
| Exec — output limit | `ExecResult` (exit 1) | `isError: false` |
| Exec — WASM trap (other) | `ExecResult` (exit 1, trap in stderr) | `isError: false` |
| Sandbox stopped, exec attempted | `SandboxDiedError` | `isError: true`, connection drops |
| `stop()` fails | Swallowed (best-effort) | N/A |

**WASM-specific failure modes:**

- **WASM traps** (unreachable instruction, out-of-bounds memory, stack overflow) are caught and returned as ExecResult with exit code 1 and the trap message in stderr. These are not propagated as exceptions because they're analogous to a segfault in a native process — the command failed, but the sandbox is fine.
- **Module instantiation failure** (missing WASI imports, incompatible module) is caught during exec and returned as a BackendError.
- **Fuel exhaustion** is treated identically to timeout — exit code 124, timeout-style message.

---

## 10. Packaging and Dependencies

### 10.1 Optional Dependency Group

```toml
[project.optional-dependencies]
wasm = ["wasmtime"]
```

Install with: `uv add kilntainers[wasm]` or `uv add kilntainers[wasm]`.

The `wasmtime` Python package includes the wasmtime runtime — no external binary installation needed. This makes `kilntainers[wasm]` fully self-contained.

### 10.2 Conditional Import

The WASM backend module handles missing dependencies gracefully:

```python
try:
    import wasmtime
except ImportError:
    wasmtime = None  # type: ignore[assignment]
```

This allows the module to be imported even when `wasmtime` is not installed. The `BackendError` with installation instructions is raised in `__init__` or `_validate`, not at import time. This is critical for the entry point discovery mechanism — `ep.load()` needs to import the module to get the class, and this must work even without wasmtime installed so that `--help` can still show WASM backends in the `--backend` choices.

### 10.3 Entry Point Registration

```toml
[project.entry-points."kilntainers.backends"]
docker = "kilntainers.backends.docker:DockerBackend"
modal = "kilntainers.backends.modal:ModalBackend"
wasm = "kilntainers.backends.wasm:WasmBackend"
go_busybox = "kilntainers.backends.wasm:GoBusyBoxBackend"
```

### 10.4 Package Data (Bundled WASM Binary)

```toml
[tool.setuptools.package-data]
"kilntainers.backends.wasm_data" = ["*.wasm"]
```

The `busybox.wasm` binary is included in the package distribution. It is only present when `kilntainers[wasm]` is installed (the `wasm_data` directory is part of the `kilntainers` package).

### 10.5 Future: Modal as Optional

The entry point architecture naturally supports making `modal` an optional dependency too:

```toml
[project.optional-dependencies]
modal = ["modal>=1.3.2"]
wasm = ["wasmtime"]
```

This is a separate task but follows the same pattern.

---

## 11. Testing

### 11.1 Unit Tests (`backends/test_wasm.py`)

Unit tests mock the wasmtime package to test backend logic without requiring wasmtime to be installed.

#### Mock Strategy

```python
@pytest.fixture
def mock_wasmtime(monkeypatch):
    """Mock wasmtime module with configurable behavior."""

    class MockWasiConfig:
        def __init__(self):
            self.argv = ()
            self.stdout_file = None
            self.stderr_file = None
            self.stdin_file = None
            self._preopened_dirs = []

        def preopen_dir(self, host_path, guest_path):
            self._preopened_dirs.append((host_path, guest_path))

    class MockStore:
        def __init__(self, engine):
            self._engine = engine
            self._wasi = None
            self._epoch_deadline = None
            self._fuel = None

        def set_wasi(self, config):
            self._wasi = config

        def set_epoch_deadline(self, deadline):
            self._epoch_deadline = deadline

        def set_fuel(self, fuel):
            self._fuel = fuel

        def set_limits(self, **kwargs):
            self._limits = kwargs

    class MockExports:
        def __init__(self, funcs):
            self._funcs = funcs

        def get(self, name):
            return self._funcs.get(name)

    class MockInstance:
        def __init__(self, exports_dict):
            self._exports = exports_dict

        def exports(self, store):
            return MockExports(self._exports)

    # ... additional mocks for Engine, Module, Linker, Config ...

    return mock_module
```

#### Test Cases

**Validation:**
- wasmtime not installed → `BackendError` with install instructions.
- .wasm file not found → `BackendError` with file path.
- Module compilation fails → `BackendError` with compile error.
- Valid module → engine, module, linker created; epoch ticker started.

**Command construction:**
- `args` mode → argv: `[module_stem, *args]`.
- `command` mode with shell → argv: `[module_stem, shell, "-c", command]`.
- `command` mode without shell → `BackendError` with guidance.
- Module name derived from filename stem.

**Exec — normal results:**
- Successful command → ExecResult with stdout, stderr, exit_code=0, duration.
- Failed command (non-zero exit via ExitTrap) → ExecResult with exit code preserved.
- Empty output → ExecResult with empty strings.

**Exec — timeout:**
- Epoch deadline trap → ExecResult with exit_code=124, timeout message.
- Duration reflects actual wall-clock time.

**Exec — fuel exhaustion:**
- Fuel trap → ExecResult with exit_code=124, timeout message.

**Exec — output limit:**
- Output exceeds limit (post-execution check) → ExecResult with exit_code=1, limit message.
- Under limit → output returned normally.

**Exec — stdin:**
- `stdin` provided → written to temp file, path set in WasiConfig.
- `stdin` not provided → no stdin_file configured.

**Exec — working directory:**
- `working_directory` set → subdirectory preopened as `.`.
- Directory created if it doesn't exist.
- Default → sandbox root preopened as `.`.

**Exec — WASM trap (other):**
- Non-timeout trap → ExecResult with exit_code=1, trap message in stderr.

**Sandbox lifecycle:**
- Creation → temp dir exists, readiness verified.
- Stop → temp dir removed.
- Stop is idempotent.
- Exec after stop → `SandboxDiedError`.

**Death detection:**
- `wait_for_death()` blocks forever.
- Cancellation cleans up.

**Tool instructions:**
- WasmBackend → `None` (requires override).
- GoBusyBoxBackend → description with available commands.

**GoBusyBox:**
- `wasm_path` set to bundled binary.
- `shell` defaults to `ash`.
- `config_from_args` constructs correct config.

### 11.2 Integration Tests (`backends/test_wasm_integration.py`)

Integration tests run with the real wasmtime package and bundled go-busybox binary. They require the `wasmtime` package to be installed (skipped otherwise).

```python
skip_without_wasmtime = pytest.mark.skipif(
    not _wasmtime_available(),
    reason="wasmtime package not installed"
)

def _wasmtime_available() -> bool:
    try:
        import wasmtime
        return True
    except ImportError:
        return False
```

#### Test Cases

**Lifecycle:**
- Create sandbox → temp dir exists → readiness passes → sandbox_id valid.
- Stop sandbox → temp dir removed.
- Stop is idempotent.

**Basic exec (go-busybox):**
- `command="echo hello"` → stdout `"hello\n"`, exit_code 0.
- `args=["echo", "hello"]` → stdout `"hello\n"`, exit_code 0.
- `args=["ls", "/nonexistent"]` → non-zero exit, stderr with error.

**Command mode vs args mode:**
- `command="echo hello | tr a-z A-Z"` → stdout `"HELLO\n"` (shell pipes work).
- `args=["echo", "hello world"]` → stdout `"hello world\n"` (no shell splitting).

**Filesystem persistence:**
- `command="echo test > file.txt"` then `command="cat file.txt"` → stdout `"test\n"`.
- Files persist between exec calls within a session.

**Working directory:**
- `command="mkdir subdir"` then `command="pwd"` with `working_directory="/subdir"` → reflects subdir.

**Stdin:**
- `command="cat > file.txt"` with `stdin="content"` then `command="cat file.txt"` → stdout `"content"`.
- `args=["wc", "-c"]` with `stdin="hello"` → stdout `"5\n"`.

**Timeout:**
- `command="sleep 60"` with `timeout=2` → exit_code 124, timeout message, duration ~2–3s.

**Output limit:**
- `command="yes"` with `output_limit=1000`, `timeout=5` → exit_code 1 or 124 (timeout may fire before post-exec check).

**Stateless execution (environment):**
- `command="export FOO=bar"` then `command="echo $FOO"` → empty (no env state between calls).

**WASM-specific:**
- Relative paths work (`ls .`, `cat ./file.txt`).
- Absolute sandbox paths work (`ls /sandbox`).
- Paths outside sandbox fail (`ls /usr`).

---

## 12. Implementation Notes

### 12.1 Thread Safety

The WASM backend uses threads in two places:

1. **Epoch ticker thread** — runs for the backend's lifetime, calling `engine.increment_epoch()`. This is thread-safe per wasmtime's design.
2. **Exec thread** — each exec call runs WASM in a thread via `asyncio.to_thread`. The `_exec_lock` ensures only one exec runs at a time per sandbox, so there's no concurrent WASM execution within a sandbox.

The Engine and Module are safe to share across threads (they're immutable after creation). The Store is created fresh per exec and used only within its thread.

### 12.2 I/O Temp File Cleanup

Stdout, stderr, and stdin temp files (`.kilntainers_stdout`, `.kilntainers_stderr`, `.kilntainers_stdin`) are created in the sandbox's tmp directory. They are:

- **Cleaned up** by `_read_output_file()` after reading (stdout/stderr).
- **Overwritten** on each exec call (same paths reused).
- **Removed** when the sandbox is stopped (`shutil.rmtree`).

These files are in the preopened directory and technically visible to the WASM module. The `.kilntainers_` prefix makes them unlikely to collide with user files. If a WASM program reads or modifies these files, it could affect I/O — this is a known but low-risk edge case.

### 12.3 wasmtime-py Version Compatibility

The architecture assumes wasmtime-py features available in recent versions (epoch interruption, fuel metering, Store limits, WasiConfig file I/O). The `pyproject.toml` dependency should pin a minimum version that supports all required features:

```toml
wasm = ["wasmtime>=20.0.0"]
```

The exact minimum version should be verified during implementation.

### 12.4 ExitTrap vs Trap Distinction

wasmtime-py raises different exception types:

- **`wasmtime.ExitTrap`** — the WASM program called `proc_exit(code)`. Normal exit. Extract `e.code` for the exit code.
- **`wasmtime.Trap`** — runtime trap (epoch deadline, fuel, unreachable, OOB, stack overflow). Inspect `str(e)` or trap code to classify.

The implementation must correctly distinguish these. If the wasmtime-py API changes in future versions (e.g., unified exception type), the exception handling should be updated accordingly.

### 12.5 go-busybox Binary Updates

The bundled `busybox.wasm` binary should be updated when new go-busybox releases add features or fix bugs. The update process:

1. Download the optimized WASM build from the go-busybox releases.
2. Replace `src/kilntainers/backends/wasm_data/busybox.wasm`.
3. Update the command list in `GoBusyBoxBackend.tool_instructions()` if commands were added/removed.
4. Run integration tests to verify compatibility.

Consider adding a `BUSYBOX_VERSION` constant to track the bundled version.

### 12.6 Entry Point Discovery and Development Mode

Entry points require the package to be installed (not just on `sys.path`). During development with `uv` or `uv add .`, the package is installed in editable mode and entry points are registered. Running the module directly without installation (e.g., `python -m kilntainers`) will not discover entry-point-registered backends.

This is consistent with the existing project setup — the `pyproject.toml` already configures `[project.scripts]` which requires installation.

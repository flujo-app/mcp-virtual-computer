# Architecture: Modal Backend Implementation

Defines how the Modal backend implements the Backend and Sandbox ABCs from Phase 2 using the Modal Python SDK. Covers authentication, sandbox lifecycle, exec flow with timeout and output-limit enforcement, death detection, and resource configuration.

**References:** Functional spec §3.2, §4.3, §4.4, §5, §7, §8.4, §9. Phase 2 (backend abstraction). Modal SDK docs: [Sandboxes](https://modal.com/docs/guide/sandboxes), [Running commands](https://modal.com/docs/guide/sandbox-spawn), [Networking](https://modal.com/docs/guide/sandbox-networking), [modal.Sandbox reference](https://modal.com/docs/reference/modal.Sandbox).

---

## 1. Overview

The Modal backend consists of two classes in `src/kilntainers/backends/modal.py`:

- **`ModalBackend(Backend)`** — Validates Modal authentication and connectivity, constructs Modal Image objects, creates sandboxes, and provides tool description text.
- **`ModalSandbox(Sandbox)`** — Wraps a running Modal Sandbox. Handles command execution with timeout and output-limit enforcement, stop, and death detection.

All interaction with Modal happens through the **Modal Python SDK** (`modal` package). Unlike the Docker backend which shells out to a CLI binary, the Modal backend uses the SDK's async API (`.aio()` methods) directly. No subprocess management is involved — the Modal SDK handles all communication with Modal's cloud infrastructure.

**Key differences from the Docker backend:**

| Concern | Docker | Modal |
|---|---|---|
| Interface | CLI subprocess (`docker` binary) | Python SDK (`modal` package) |
| Execution location | Local container on host | Remote container on Modal's cloud |
| Image management | Explicit pull, local cache | Handled by Modal (build + cache) |
| Keep-alive process | `tail -f /dev/null` | Not needed — sandboxes stay alive until timeout/termination |
| Network isolation | `--network none` | `block_network=True` |
| Death detection | `docker wait` (blocking) | `sb.wait.aio()` (blocking) |
| Timeout enforcement | Client-side (`asyncio.wait_for` on subprocess) | Server-side (Modal kills the process) + client-side safety net |
| Auth | Docker daemon socket | Token-based (token ID + secret) |
| Cost | Local compute only | Pay-per-use cloud compute |

---

## 2. Configuration

The Modal backend receives its configuration as a typed dataclass. As with the Docker backend, the full definition lives alongside CLI & configuration code (Phase 5). This section documents what the Modal backend needs.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ModalBackendConfig(BackendConfig):
    """Configuration for the Modal backend.

    Populated from CLI args by ModalBackend.config_from_args().
    """
    # Authentication (optional — falls back to Modal's default auth)
    token_id: str | None = None                  # --modal-token-id
    token_secret: str | None = None              # --modal-token-secret

    # Modal app
    app_name: str = "kilntainers"                # --modal-app-name

    # Sandbox environment
    image: str | None = None                     # --image (None = debian_slim default)
    shell: str = "/bin/bash"                     # --shell
    network_enabled: bool = False                # --network

    # Resources
    cpu: float = 1.0                             # --cpu (fractional cores)
    memory: int = 512                            # --memory (MiB)
    gpu: str | None = None                       # --gpu (e.g., "A10G", "H100:2")
    region: str | None = None                    # --region (e.g., "us-east")

    # Sandbox lifetime
    sandbox_timeout: int = 3600                  # --modal-sandbox-timeout (seconds)

    # Tool description
    default_timeout: int = 120                   # --timeout (for tool description)
```

**Notes:**

- `token_id` and `token_secret` are optional. When not provided, the Modal SDK uses its default authentication chain: `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` environment variables, then `~/.modal/credentials`. When provided, they override the default auth.
- `image` being `None` means "use `modal.Image.debian_slim()`" — Modal's pre-cached Debian slim image. Any non-None value is treated as a Docker registry reference and passed to `modal.Image.from_registry(image)`.
- `cpu` is a float (fractional cores). Modal's default is 0.125, but we default to 1.0 for a sandbox that needs to compile code, run tests, etc.
- `memory` is an integer in MiB. Modal's default is 128 MiB, but we default to 512 MiB.
- `gpu` accepts Modal's GPU specification strings (e.g., `"T4"`, `"A10G"`, `"A100"`, `"H100"`, `"H100:2"`). Default is `None` (CPU-only).
- `sandbox_timeout` is the maximum lifetime of the Modal sandbox container. Default is 1 hour (3600 seconds). The sandbox is terminated when the session ends regardless, but this caps runaway sandboxes. Modal's maximum is 24 hours.
- `default_timeout` is used only for the tool description text (same pattern as Docker).
- `region` is optional geographic placement (e.g., `"us-east"`, `"eu-west"`). When `None`, Modal chooses automatically.

---

## 3. Modal SDK Interaction

### 3.1 Async API

The Modal SDK provides both synchronous and asynchronous APIs. Async methods are accessed via the `.aio` attribute:

```python
# Sync
sb = modal.Sandbox.create(app=app)

# Async
sb = await modal.Sandbox.create.aio(app=app)
```

The Modal backend uses the async API exclusively, since the backend abstraction layer is async and the MCP server runs in an asyncio event loop.

### 3.2 Authentication Setup

When custom tokens are provided via CLI args, they are set as environment variables before any Modal SDK calls. The Modal SDK picks up `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` automatically.

```python
def _configure_auth(self) -> None:
    """Set Modal auth environment variables if custom tokens are provided."""
    if self._config.token_id is not None:
        os.environ["MODAL_TOKEN_ID"] = self._config.token_id
    if self._config.token_secret is not None:
        os.environ["MODAL_TOKEN_SECRET"] = self._config.token_secret
```

**Design decision:** Environment variables are the simplest and most compatible approach. The Modal SDK reads them on first API call. This avoids coupling to internal SDK client initialization methods.

**Security note:** Both `--modal-token-id` and `--modal-token-secret` will be visible in the process's environment. This is acceptable since the process is short-lived and controlled by the operator. For production deployments, operators should prefer setting `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` directly (no CLI args needed) or using Modal's config file (`~/.modal/credentials`).

### 3.3 App Lookup

Modal requires an `App` reference when creating sandboxes outside of a Modal container. The backend looks up (or creates) the app by name during validation:

```python
self._app = await modal.App.lookup.aio(
    self._config.app_name,
    create_if_missing=True,
)
```

The `create_if_missing=True` flag ensures the app is created on first use. The app persists on Modal's side and is reused on subsequent runs.

---

## 4. ModalBackend

### 4.1 Validation (`_validate`)

Checks that Modal authentication is configured and the API is reachable.

```python
async def _validate(self) -> None:
    # Configure auth from CLI args (if provided)
    self._configure_auth()

    try:
        self._app = await modal.App.lookup.aio(
            self._config.app_name,
            create_if_missing=True,
        )
    except modal.exception.AuthError:
        raise BackendError(
            "Modal authentication failed. Either:\n"
            "  - Set MODAL_TOKEN_ID and MODAL_TOKEN_SECRET environment variables\n"
            "  - Run 'modal token set' to configure credentials\n"
            "  - Pass --modal-token-id and --modal-token-secret"
        )
    except modal.exception.ConnectionError:
        raise BackendError(
            "Cannot connect to Modal. Check your network connection "
            "and Modal service status at https://status.modal.com"
        )
```

**What this validates:**

- Modal credentials exist and are valid (token ID + secret).
- The Modal API is reachable (network connectivity).
- The app can be looked up or created.

**What this does NOT validate:**

- Image availability (checked during sandbox creation — Modal builds/caches on demand).
- GPU availability (checked when the sandbox is created — Modal returns an error if the requested GPU is unavailable).
- Shell availability in the image (checked during readiness verification).

### 4.2 Image Construction

The Modal backend constructs a `modal.Image` object from the CLI configuration. This happens during sandbox creation, not validation.

```python
def _build_image(self) -> modal.Image:
    """Construct the Modal Image from configuration."""
    if self._config.image is None:
        return modal.Image.debian_slim()
    else:
        return modal.Image.from_registry(self._config.image)
```

**Design decisions:**

- **`modal.Image.debian_slim()`** is Modal's pre-built, cached Debian slim image. It's fast to start (no build step) and closely matches our Docker default (`debian:bookworm-slim`).
- **`modal.Image.from_registry(image)`** pulls any Docker registry image. This gives CLI users the same `--image python:3.12-slim` experience as Docker.
- **No image builder features** (`.pip_install()`, `.apt_install()`, etc.) are exposed via CLI. Users who need custom dependencies should build their image in a registry and reference it with `--image`. This keeps the CLI simple and matches Docker's model.
- **Modal handles caching.** Unlike Docker where we explicitly check-then-pull, Modal builds and caches images transparently. First sandbox creation with a new image may be slow; subsequent ones use Modal's cache.

### 4.3 Sandbox Creation (`_create_sandbox`)

Creates a Modal sandbox, verifies readiness, and returns a `ModalSandbox`.

```python
async def _create_sandbox(self) -> "ModalSandbox":
    # 1. Build image
    image = self._build_image()

    # 2. Build GPU config (if specified)
    gpu_config = self._config.gpu  # str like "A10G" or None

    # 3. Create sandbox
    try:
        sb = await modal.Sandbox.create.aio(
            app=self._app,
            image=image,
            timeout=self._config.sandbox_timeout,
            cpu=self._config.cpu,
            memory=self._config.memory,
            gpu=gpu_config,
            region=self._config.region,
            block_network=not self._config.network_enabled,
        )
    except modal.exception.InvalidError as e:
        raise BackendError(f"Failed to create Modal sandbox: {e}")
    except modal.exception.NotFoundError as e:
        raise BackendError(
            f"Modal image not found: '{self._config.image}'. "
            f"Check that the image name is correct and the registry is accessible."
        )

    # 4. Create sandbox wrapper
    sandbox = ModalSandbox(
        modal_sandbox=sb,
        shell=self._config.shell,
    )

    # 5. Verify readiness
    try:
        await sandbox._verify_readiness()
    except Exception:
        await sandbox.stop()
        raise

    return sandbox
```

**Notes:**

- `block_network` is the inverse of our `network_enabled` flag. When `--network` is not passed (default), `block_network=True` — the sandbox has no network access.
- `timeout` here is the sandbox lifetime (default 1 hour), not the exec timeout. The sandbox stays alive for this duration unless explicitly terminated.
- Modal raises `InvalidError` for invalid configurations (e.g., invalid GPU type, resource limits too high). We translate this to `BackendError`.
- The image build may happen during `Sandbox.create()`. If the image is new, Modal builds it (which includes package installation, etc.). This can take time but is transparent.

#### 4.3.1 Readiness Verification

After creation, a trivial exec confirms the sandbox is usable:

```python
async def _verify_readiness(self) -> None:
    """Verify the sandbox accepts exec calls and the shell works."""
    try:
        p = await self._modal_sandbox.exec.aio(
            self._shell, "-c", "echo kilntainers-ready",
            timeout=10,
        )
        stdout = await p.stdout.read.aio()
    except Exception as e:
        raise BackendError(
            f"Sandbox readiness check failed: {e}"
        )

    if "kilntainers-ready" not in stdout:
        raise BackendError(
            f"Sandbox started but readiness check failed (unexpected output). "
            f"Shell '{self._shell}' may not be available in the image. "
            f"Use --shell /bin/sh for images without bash."
        )
```

This validates:

1. **Sandbox readiness** — the sandbox is running and accepts exec calls.
2. **Shell availability** — the configured `--shell` binary exists and supports `-c`.

### 4.4 Tool Instructions

Returns the tool description for the `terminal_execute` tool, with dynamic values from configuration.

```python
def tool_instructions(self) -> str | None:
    if self._config.image is not None:
        return None

    shell_name = self._config.shell.rsplit("/", 1)[-1]  # basename
    timeout = self._config.default_timeout

    return (
        f"Execute a shell command in a remote cloud sandbox (Modal). "
        f"Commands run in {shell_name}. Each call is independent — "
        f"no state (shell variables, working directory) persists between calls. Use the working_directory "
        f"parameter or chain commands with && to control execution context."
        f"\n\n"
        f"To write files or pass data without shell escaping, use the "
        f"stdin parameter (e.g., command=\"cat > file.txt\" with content "
        f"in stdin). Commands time out after {timeout} seconds by default "
        f"(override with the timeout parameter for long-running operations)."
    )
```

**Custom image behavior:** When `--image` is set (any non-default value), `tool_instructions()` returns `None`. The server requires `--tool-instruction-override` — same pattern as Docker.

---

## 5. ModalSandbox

### 5.1 State and Construction

```python
class ModalSandbox(Sandbox):
    def __init__(
        self,
        *,
        modal_sandbox: modal.Sandbox,
        shell: str,
    ) -> None:
        self._modal_sandbox = modal_sandbox
        self._shell = shell
        self._stopped = False
        self._stop_requested = False
        self._exec_lock = asyncio.Lock()

    @property
    def sandbox_id(self) -> str:
        return self._modal_sandbox.object_id
```

**State fields:**

| Field | Purpose |
|---|---|
| `_modal_sandbox` | The Modal SDK Sandbox object. All exec/stop/wait calls go through this. |
| `_shell` | Shell binary for `command` mode exec. |
| `_stopped` | Idempotency guard for `stop()`. |
| `_stop_requested` | Distinguishes normal stop from unexpected death in `wait_for_death()`. |
| `_exec_lock` | Serializes exec calls within this sandbox (D29). |

**`sandbox_id`** returns Modal's `object_id` — a unique identifier assigned by Modal. This serves the same role as the Docker container short ID (logging, session mapping).

### 5.2 Command Execution

The exec flow uses the Modal SDK's `Sandbox.exec()` method, which returns a `ContainerProcess` with streaming stdout/stderr access.

```python
async def exec(self, request: ExecRequest) -> ExecResult:
    if self._stopped:
        raise SandboxDiedError("Sandbox has been stopped")

    async with self._exec_lock:
        return await self._do_exec(request)
```

Same pattern as Docker — check liveness, acquire the serialization lock, delegate.

#### 5.2.1 Core Exec Flow

```python
async def _do_exec(self, request: ExecRequest) -> ExecResult:
    exec_args = self._build_exec_args(request)
    exec_kwargs = self._build_exec_kwargs(request)

    start_time = time.monotonic()

    try:
        process = await self._modal_sandbox.exec.aio(
            *exec_args, **exec_kwargs,
        )

        # Write stdin if provided
        if request.stdin is not None:
            process.stdin.write(request.stdin.encode("utf-8"))
            process.stdin.write_eof()
            await process.stdin.drain.aio()

        # Read output with limit enforcement
        stdout_str, stderr_str = await self._read_output_with_limit(
            process, request.output_limit, request.timeout,
        )

        # Wait for process to finish and get exit code
        await process.wait.aio()
        exit_code = process.returncode

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        return ExecResult(
            stdout=stdout_str,
            stderr=stderr_str,
            exit_code=exit_code,
            exec_duration_ms=elapsed_ms,
        )

    except _OutputLimitExceeded:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
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

    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return ExecResult(
            stdout="",
            stderr=f"[kilntainers: command timed out after {request.timeout}s]",
            exit_code=124,
            exec_duration_ms=elapsed_ms,
        )

    except modal.exception.SandboxTerminatedError:
        if not self._stop_requested:
            raise SandboxDiedError(
                f"Sandbox {self.sandbox_id} died during command execution"
            )
        raise SandboxDiedError("Sandbox has been stopped")

    except modal.exception.SandboxTimeoutError:
        # Sandbox lifetime timeout expired (not exec timeout)
        raise SandboxDiedError(
            f"Sandbox {self.sandbox_id} lifetime timeout expired"
        )
```

**Key design choices:**

- **Modal's exec timeout** is passed to `sb.exec.aio()`, so the process is killed server-side when the timeout fires. This is more robust than client-side-only timeout (Docker's approach), since it works even if the client loses connectivity.
- **Client-side timeout** (`asyncio.wait_for` in `_read_output_with_limit`) acts as a safety net for network issues. Set to `request.timeout + buffer` to account for network latency.
- **Output limit** is enforced client-side by monitoring the byte count of data read from stdout/stderr streams.
- **Modal exceptions** (`SandboxTerminatedError`, `SandboxTimeoutError`) are caught and translated to `SandboxDiedError` for the MCP layer.
- Both timeout and output-limit conditions produce an `ExecResult`, not an exception — matching the functional spec's error model (§2.5).

#### 5.2.2 Command Construction

```python
def _build_exec_args(self, request: ExecRequest) -> list[str]:
    """Build the positional args for Modal Sandbox.exec()."""
    if request.command is not None:
        # Command mode: wrap in shell
        return [self._shell, "-c", request.command]
    else:
        # Args mode: pass directly
        assert request.args is not None
        return request.args

def _build_exec_kwargs(self, request: ExecRequest) -> dict:
    """Build keyword args for Modal Sandbox.exec()."""
    kwargs: dict = {
        "timeout": request.timeout,
    }
    if request.working_directory is not None:
        kwargs["workdir"] = request.working_directory
    return kwargs
```

**Examples of constructed calls:**

| Request | Modal exec call |
|---|---|
| `command="ls -la"` | `sb.exec("/bin/bash", "-c", "ls -la", timeout=120)` |
| `command="cat > f.txt"`, `stdin="hello"` | `sb.exec("/bin/bash", "-c", "cat > f.txt", timeout=120)` + stdin write |
| `args=["python3", "script.py"]` | `sb.exec("python3", "script.py", timeout=120)` |
| `command="make"`, `working_directory="/app"` | `sb.exec("/bin/bash", "-c", "make", timeout=120, workdir="/app")` |

**Shell selection:** Same pattern as Docker — `command` mode wraps in `<shell> -c <command>`; `args` mode passes directly with no shell.

### 5.3 Timeout Enforcement

Timeout is enforced at **two levels**:

1. **Server-side (Modal):** The `timeout` parameter on `sb.exec.aio()` tells Modal to kill the process after that many seconds. This is the primary enforcement mechanism.
2. **Client-side (safety net):** The output reading is wrapped in `asyncio.wait_for()` with `timeout + 10s` buffer. This catches cases where Modal's server-side timeout fires but the client doesn't receive the signal promptly (network delay, API latency).

```python
# In _read_output_with_limit:
try:
    stdout_str, stderr_str = await asyncio.wait_for(
        self._read_streams(process, output_limit),
        timeout=request_timeout + 10,  # safety buffer
    )
except asyncio.TimeoutError:
    # Client-side safety net fired — Modal should have already killed
    # the process server-side. Treat as timeout.
    raise
```

**Why dual enforcement:** Modal's server-side timeout is more reliable (it kills the process even if the client disconnects), but the client needs a safety net for its own event loop. Without the client-side timeout, a network stall could block the exec coroutine indefinitely.

**Exit code 124** is returned for timeout, matching the GNU `timeout` convention and the Docker backend behavior.

### 5.4 Output Limit Enforcement

Output is read from the Modal `ContainerProcess`'s stdout and stderr streams concurrently, with a combined byte counter.

```python
class _OutputLimitExceeded(Exception):
    """Internal signal: combined output exceeded the configured limit."""
    pass


async def _read_output_with_limit(
    self,
    process: modal.container_process.ContainerProcess,
    output_limit: int,
    request_timeout: int,
) -> tuple[str, str]:
    """Read process output with combined size enforcement.

    Returns (stdout_str, stderr_str).
    Raises _OutputLimitExceeded if combined output exceeds the limit.
    Raises asyncio.TimeoutError if client-side safety timeout fires.
    """
    return await asyncio.wait_for(
        self._read_streams(process, output_limit),
        timeout=request_timeout + 10,
    )


async def _read_streams(
    self,
    process: modal.container_process.ContainerProcess,
    output_limit: int,
) -> tuple[str, str]:
    """Read stdout and stderr with combined limit enforcement."""
    total_bytes = 0
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    async def read_stream(
        stream, chunks: list[str],
    ) -> None:
        nonlocal total_bytes
        async for line in stream:
            line_bytes = len(line.encode("utf-8"))
            total_bytes += line_bytes
            if total_bytes > output_limit:
                raise _OutputLimitExceeded()
            chunks.append(line)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(read_stream(process.stdout, stdout_chunks))
        tg.create_task(read_stream(process.stderr, stderr_chunks))

    return "".join(stdout_chunks), "".join(stderr_chunks)
```

**Design details:**

- **Line-based reading** — Modal's `StreamReader` yields text lines when iterated. We count the UTF-8 byte length of each line for the combined limit. This is slightly different from Docker's chunk-based reading, but achieves the same result.
- **Combined counter** (`total_bytes`) tracks stdout + stderr together (Functional spec §2.4). Safe in asyncio — both tasks run in the same thread.
- **`asyncio.TaskGroup`** cancels sibling tasks when one raises `_OutputLimitExceeded`, same pattern as Docker.
- **No partial output** returned when limit is exceeded — same as Docker.
- **Abandoned process:** When the limit fires, we stop reading but the process may continue running on Modal. Since exec calls are serialized, no new exec starts until this one's `_do_exec` returns. The abandoned process runs until its timeout or the sandbox is terminated.

### 5.5 Stdin Piping

Stdin data is written to the Modal `ContainerProcess`'s stdin stream after exec starts.

**Flow:**

1. MCP layer constructs `ExecRequest` with `stdin="file content..."`.
2. `_do_exec` starts the exec, then writes stdin data:
   ```python
   process.stdin.write(request.stdin.encode("utf-8"))
   process.stdin.write_eof()
   await process.stdin.drain.aio()
   ```
3. Inside the sandbox, the command reads from stdin.

**Encoding:** Same as Docker — `request.stdin` is a Python string, encoded to UTF-8 bytes for writing.

**Stdin size limit** (2 MiB, D32) is enforced by the MCP layer before the ExecRequest reaches the backend.

### 5.6 Stop

```python
async def stop(self) -> None:
    if self._stopped:
        return
    self._stopped = True
    self._stop_requested = True

    try:
        await asyncio.wait_for(
            self._modal_sandbox.terminate.aio(),
            timeout=10,
        )
    except asyncio.TimeoutError:
        pass  # Best-effort — Modal may take time to terminate
    except Exception:
        pass  # Best-effort cleanup
```

**Design details:**

- **Idempotent** — the `_stopped` flag ensures double-stop is a no-op (Phase 2 contract).
- **`_stop_requested` flag** is set before issuing terminate. This tells `wait_for_death()` that the sandbox exit is expected (§5.7).
- **`modal.Sandbox.terminate()`** tells Modal to stop the sandbox. This is a no-op if the sandbox has already finished running.
- **10-second timeout** covers the terminate API call. If Modal's API is slow, we proceed without waiting.
- **No `--rm` equivalent needed** — Modal sandboxes are ephemeral by nature. When terminated, all resources are released automatically.
- **Exception swallowing** — stop is best-effort, same as Docker.

### 5.7 Death Detection

`wait_for_death()` monitors the sandbox using Modal's `Sandbox.wait()`, which blocks until the sandbox finishes running.

```python
async def wait_for_death(self) -> None:
    try:
        await self._modal_sandbox.wait.aio(raise_on_termination=False)
    except asyncio.CancelledError:
        # Normal shutdown — MCP layer cancelled this task before stop()
        raise

    if self._stop_requested:
        # Sandbox exited because stop() was called — expected.
        # Block forever; the MCP layer will cancel this task.
        try:
            await asyncio.Future()  # never completes
        except asyncio.CancelledError:
            return

    # Sandbox exited unexpectedly (OOM, lifetime timeout, preemption).
    # Returning signals death to the MCP layer.
```

**How it works:**

1. `sb.wait.aio(raise_on_termination=False)` blocks until the sandbox finishes for any reason.
2. **Normal shutdown path:** The MCP layer calls `death_task.cancel()` before `sandbox.stop()`. The `CancelledError` propagates cleanly.
3. **Unexpected death path:** The sandbox exits without `stop()` being called. `wait()` returns. `_stop_requested` is `False`. The method returns normally, signaling death to the MCP layer.
4. **Race condition handling:** Same pattern as Docker — if `stop()` was called but cancellation hasn't reached this task, `_stop_requested` is `True` and the method blocks on an unresolvable `Future`.

**`sb.wait()` vs polling:** `sb.wait()` is a blocking call that resolves when the sandbox finishes — functionally equivalent to `docker wait`. No polling loop, no polling interval concerns, no wasted API calls.

**`raise_on_termination=False`:** By default, `wait()` raises on termination. We set this to `False` because we handle all exit reasons uniformly — check `_stop_requested` to distinguish expected vs. unexpected.

---

## 6. Sandbox Configuration

### 6.1 Modal Sandbox.create Parameters

The full set of parameters passed to `modal.Sandbox.create()`:

```python
sb = await modal.Sandbox.create.aio(
    app=self._app,
    image=image,                                    # modal.Image object
    timeout=self._config.sandbox_timeout,            # sandbox lifetime (seconds)
    cpu=self._config.cpu,                            # fractional CPU cores
    memory=self._config.memory,                      # MiB
    gpu=self._config.gpu,                            # GPU spec string or None
    region=self._config.region,                      # geographic region or None
    block_network=not self._config.network_enabled,  # network isolation
)
```

### 6.2 Network Isolation

Network is **disabled by default** (`block_network=True`). This matches the Docker backend's behavior and the functional spec's security model (D5, §9.1).

- **`--network` flag NOT set** (default): `block_network=True`. The sandbox cannot make outbound network connections.
- **`--network` flag set**: `block_network=False`. The sandbox has full outbound network access.

Modal also supports `cidr_allowlist` for fine-grained network control, but this is not exposed in v1. Operators who need this can use `--tool-instruction-override` to describe the environment and configure Modal separately.

### 6.3 Resource Configuration

| CLI Parameter | Modal Parameter | Default | Example |
|---|---|---|---|
| `--cpu` | `cpu` (float) | `1.0` | `--cpu 2.0` → `cpu=2.0` |
| `--memory` | `memory` (int, MiB) | `512` | `--memory 1024` → `memory=1024` |
| `--gpu` | `gpu` (str) | `None` (CPU-only) | `--gpu A10G` → `gpu="A10G"` |

**CPU:** Fractional cores. Modal's minimum is 0.125. Our default of 1.0 gives the sandbox a full core — sufficient for most CLI tasks.

**Memory:** Integer MiB. Our default of 512 MiB is reasonable for general-purpose shell commands. For data-heavy workloads, the operator can increase with `--memory`.

**GPU:** Accepts Modal's GPU specification strings. Common values: `T4`, `L4`, `A10G`, `A100`, `A100-80GB`, `H100`, `H100:2` (multi-GPU). When not set, the sandbox runs on CPU only. GPU sandboxes cost significantly more — operators should set this intentionally.

### 6.4 Sandbox Lifetime

The `--modal-sandbox-timeout` parameter controls how long the Modal sandbox stays alive (default: 1 hour). This is separate from the per-exec timeout.

- **Normal operation:** The sandbox is terminated when the session ends (`stop()` is called). The lifetime timeout doesn't fire.
- **Runaway sessions:** If the server crashes or the session hangs, the lifetime timeout ensures the sandbox (and its billing) doesn't run indefinitely.
- **Maximum:** Modal caps sandbox lifetime at 24 hours. The CLI should validate this constraint.

### 6.5 Identification and Cleanup

Modal sandboxes are identified by their `object_id` (exposed via `sandbox_id` property). Unlike Docker, there is no labeling system for cleanup — Modal sandboxes are managed entirely through the Modal dashboard and API.

**Orphan management:**

- **Normal operation:** The sandbox is terminated on session end. Modal releases all resources.
- **Server crash:** The sandbox continues running until its lifetime timeout expires, then Modal terminates it automatically. No manual cleanup needed (unlike Docker, where orphaned containers require `docker stop`).
- **Manual cleanup:** Orphaned sandboxes are visible in the Modal dashboard and can be terminated there. They can also be found via `modal.Sandbox.list()` filtered by the app.

This is a significant advantage over Docker — Modal's built-in lifetime timeout eliminates the orphan problem.

---

## 7. Error Handling Summary

| Modal failure | Raised as | MCP result |
|---|---|---|
| Auth failure (validation) | `BackendError` | Server fails to start |
| API unreachable (validation) | `BackendError` | Server fails to start |
| Image not found | `BackendError` | Server fails to start (stdio) or session fails (HTTP) |
| Invalid config (bad GPU, etc.) | `BackendError` | Same as image failure |
| Sandbox creation fails | `BackendError` | Same |
| Readiness check fails | `BackendError` | Same — shell not found |
| Exec — command succeeds | `ExecResult` (exit 0) | `isError: false` |
| Exec — command fails | `ExecResult` (non-zero exit) | `isError: false` |
| Exec — timeout | `ExecResult` (exit 124) | `isError: false` |
| Exec — output limit | `ExecResult` (exit 1) | `isError: false` |
| Sandbox terminated during exec | `SandboxDiedError` | `isError: true`, connection drops |
| Sandbox lifetime timeout | `SandboxDiedError` | `isError: true`, connection drops |
| Sandbox preempted by Modal | `SandboxDiedError` | `isError: true`, connection drops |
| Sandbox died between execs | `wait_for_death()` returns | Connection drops |
| `terminate()` fails | Swallowed (best-effort) | N/A — already shutting down |

**Modal-specific failure modes:**

- **Preemption:** Modal may preempt sandbox containers under extreme capacity pressure. This is rare but possible. The sandbox dies, `wait_for_death()` resolves, and the MCP layer drops the connection.
- **Sandbox lifetime timeout:** If the sandbox hits its lifetime limit (default 1 hour), Modal terminates it. This looks like unexpected death to the MCP layer.
- **API errors during exec:** Network issues between the Kilntainers server and Modal's API could cause exec to fail. These should be caught and translated to appropriate error responses.

---

## 8. Testing

### 8.1 Unit Tests (`tests/unit/backends/test_modal.py`)

Unit tests mock the Modal SDK to simulate sandbox behavior without a Modal account or network access.

#### Mock Strategy

A fixture provides mock Modal objects:

```python
@pytest.fixture
def mock_modal_sandbox(monkeypatch):
    """Mock modal.Sandbox with configurable responses."""

    class MockStreamReader:
        def __init__(self, content: str):
            self._lines = content.splitlines(keepends=True)

        async def read(self):
            return "".join(self._lines)

        def __aiter__(self):
            return self._iter_lines()

        async def _iter_lines(self):
            for line in self._lines:
                yield line

    class MockStreamWriter:
        def __init__(self):
            self.data = b""

        def write(self, data: bytes):
            self.data += data

        def write_eof(self):
            pass

        class drain:
            @staticmethod
            async def aio():
                pass

    class MockContainerProcess:
        def __init__(self, stdout="", stderr="", returncode=0):
            self.stdout = MockStreamReader(stdout)
            self.stderr = MockStreamReader(stderr)
            self.stdin = MockStreamWriter()
            self.returncode = returncode

        class wait:
            @staticmethod
            async def aio():
                pass

    class MockSandbox:
        def __init__(self):
            self.object_id = "sb-mock-12345"
            self.exec_responses = []
            self.terminated = False
            self._wait_event = asyncio.Event()

        class exec:
            @staticmethod
            async def aio(*args, **kwargs):
                # Return next response from queue
                ...

        class terminate:
            @staticmethod
            async def aio():
                ...

        class wait:
            @staticmethod
            async def aio(raise_on_termination=True):
                ...

    return MockSandbox()
```

#### Test Cases

**Validation:**
- Auth succeeds → validation passes.
- Auth fails → `BackendError` with auth guidance.
- API unreachable → `BackendError` with connectivity message.
- Custom token → env vars set before validation.

**Image construction:**
- Default (`image=None`) → `modal.Image.debian_slim()` called.
- Custom (`image="python:3.12-slim"`) → `modal.Image.from_registry("python:3.12-slim")` called.

**Sandbox creation:**
- Full sequence: image build → create → readiness check → sandbox returned.
- Creation fails (invalid GPU) → `BackendError`.
- Readiness check fails (shell not found) → `BackendError`, sandbox terminated.

**Command construction:**
- `command` mode → shell wrapping: `[shell, "-c", command]`.
- `args` mode → direct: `[args...]`.
- `working_directory` present → `workdir` kwarg added.
- `timeout` always present in kwargs.

**Exec — normal results:**
- Successful command → ExecResult with stdout, stderr, exit_code=0, duration.
- Failed command (non-zero exit) → ExecResult with exit code preserved.
- Empty output → ExecResult with empty strings.

**Exec — timeout:**
- Command exceeds timeout → ExecResult with exit_code=124, timeout message.

**Exec — output limit:**
- Output exceeds limit → ExecResult with exit_code=1, limit message.
- Combined stdout+stderr over limit → caught.
- Just under limit → output returned normally.

**Exec — stdin:**
- `stdin` provided → data written to process.stdin, EOF sent.
- `stdin` not provided → no stdin write.

**Exec — serialization:**
- Concurrent exec calls are serialized by the lock.

**Stop:**
- `terminate()` called on Modal sandbox.
- Idempotent — second stop is a no-op.
- `_stop_requested` flag is set.

**Death detection:**
- Sandbox dies unexpectedly → `wait_for_death()` returns.
- `stop()` called → `wait_for_death()` blocks.
- Task cancelled → clean exit.

**Tool instructions:**
- Default image → returns description with dynamic values.
- Custom image → returns `None`.

### 8.2 Integration Tests (`tests/integration/test_modal_integration.py`)

Integration tests run against the real Modal API. They create actual sandboxes, execute real commands, and verify end-to-end behavior. These tests require Modal credentials (skipped in environments without Modal auth).

```python
skip_without_modal = pytest.mark.skipif(
    not _modal_auth_available(),
    reason="Modal credentials not configured"
)

def _modal_auth_available() -> bool:
    """Check if Modal auth is available."""
    import os
    return (
        os.environ.get("MODAL_TOKEN_ID") is not None
        and os.environ.get("MODAL_TOKEN_SECRET") is not None
    )
```

#### Test Cases

**Lifecycle:**
- Create sandbox → readiness check passes → sandbox_id is valid.
- Stop sandbox → sandbox terminated.
- Stop is idempotent.

**Basic exec:**
- `echo hello` → stdout "hello\n", exit_code 0.
- `false` → exit_code 1.
- `ls /nonexistent` → non-zero exit, stderr with error message.

**Command mode vs args mode:**
- `command="echo hello | tr a-z A-Z"` → stdout "HELLO\n".
- `args=["echo", "hello world"]` → stdout "hello world\n".

**Working directory:**
- `command="pwd"`, `working_directory="/tmp"` → stdout "/tmp\n".

**Stdin:**
- `command="cat > /tmp/test.txt"`, `stdin="file content"` then `command="cat /tmp/test.txt"` → stdout "file content".

**Timeout:**
- `command="sleep 60"`, `timeout=2` → exit_code 124, timeout message, duration ~2s.

**Output limit:**
- `command="yes"`, `output_limit=1000` → exit_code 1, limit message.

**Network isolation:**
- Default: `command="curl -s http://example.com"` → fails.
- With `--network`: outbound connections succeed.

**Cost note:** Integration tests create real Modal sandboxes and incur costs. They should be tagged separately (e.g., `@pytest.mark.integration`) and run only in CI or when explicitly requested.

---

## 9. Implementation Notes

### 9.1 Modal SDK as a Dependency

The Modal SDK (`modal` package) is a required dependency for the Modal backend. It should be installed as an optional dependency group:

```toml
[project.optional-dependencies]
modal = ["modal"]
```

The backend module should handle the case where `modal` is not installed — importing `ModalBackend` without the `modal` package should produce a clear error, not a raw `ImportError`:

```python
try:
    import modal
except ImportError:
    modal = None  # type: ignore

class ModalBackend(Backend):
    def __init__(self, config: ModalBackendConfig) -> None:
        if modal is None:
            raise BackendError(
                "Modal backend requires the 'modal' package. "
                "Install it with: uv add kilntainers[modal]"
            )
        super().__init__()
        self._config = config
```

### 9.2 TaskGroup Exception Wrapping

Same consideration as Docker (§9.1 of Docker backend doc): `_OutputLimitExceeded` raised inside an `asyncio.TaskGroup` gets wrapped in an `ExceptionGroup`. The `except*` syntax handles this:

```python
except* _OutputLimitExceeded:
    await ...
```

### 9.3 Modal SDK Thread Safety

The Modal SDK manages its own connection to the Modal API. The SDK is designed to be used from a single event loop. Since the Kilntainers server runs in a single asyncio event loop, this is not a concern. However, if multiple sandboxes are active (HTTP mode), their Modal SDK objects share the underlying connection — this is handled correctly by the SDK.

### 9.4 Image Build Latency

First sandbox creation with a new or modified image may be slow (30s–2min) while Modal builds and caches the image. Subsequent creations reuse the cached image and are fast (~2–5s). This is analogous to Docker's image pull behavior.

Unlike Docker, Modal's image build happens server-side and we don't get progress output to stderr. The `modal.enable_output()` context manager can show build logs, but integrating this with the MCP stderr channel needs consideration during implementation.

### 9.5 CLI Argument Registration

Several CLI parameters overlap with the Docker backend (`--image`, `--shell`, `--network`, `--cpu`, `--memory`). Since both backends register all their args for `--help` display (Phase 5 architecture), these shared names need careful handling. Options to resolve during implementation:

1. **Promote shared params to core level** — `--image`, `--shell`, `--network`, `--cpu`, `--memory` become core params that both backends read. Backend-specific params (`--engine`, `--docker-run-flag`, `--modal-app-name`, `--gpu`, etc.) stay backend-specific.
2. **Register only selected backend's args** — Change the CLI architecture so only the chosen backend's args are registered. `--help` would only show relevant params.
3. **Accept the overlap** — Both backends register the same names with compatible types. The selected backend reads the values; the other ignores them.

Option 1 is the cleanest long-term approach. The implementation phase should resolve this.

### 9.6 Memory Parameter Format

The Docker backend accepts `--memory` as a Docker-style string (e.g., `"512m"`, `"1g"`). The Modal backend needs an integer (MiB). Two approaches:

1. **Shared parser** — Accept Docker-style strings for both backends, parse to the appropriate type in each backend's `config_from_args()`.
2. **Backend-specific format** — Docker accepts `"512m"`, Modal accepts `512` (raw MiB).

Option 1 is preferred for consistency. A utility function `parse_memory_string("512m") → 512` can be shared.

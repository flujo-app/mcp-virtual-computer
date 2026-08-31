# Architecture: Docker Backend Implementation

**Phase 3** of the architecture specification. Defines how the Docker backend implements the Backend and Sandbox ABCs from Phase 2: subprocess interaction with the Docker CLI, container lifecycle management, exec flow with timeout and output-limit enforcement, death detection, and resource configuration.

**References:** D10 (CLI via subprocess), D10a (swappable engine), D11 (Debian slim), D18 (inline pull), D20 (shell selection), D30 (stdin piping), Functional spec §3.2, §4.3, §4.4, §5, §7. Phase 2 (backend abstraction).

---

## 1. Overview

The Docker backend consists of two classes in `src/kilntainers/backends/docker.py`:

- **`DockerBackend(Backend)`** — Validates Docker prerequisites, manages image pulls, creates containers, and provides tool description text.
- **`DockerSandbox(Sandbox)`** — Wraps a running Docker container. Handles command execution with timeout and output-limit enforcement, stop, and death detection.

All interaction with Docker happens through subprocess calls to the Docker CLI (`docker` or a compatible engine like `podman`). No Docker SDK is used (D10). The engine binary is configurable via `--engine` (D10a).

---

## 2. Configuration

The Docker backend receives its configuration as a typed dataclass. The full definition lives in Phase 5 (CLI & configuration). This section documents what the Docker backend needs.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class DockerBackendConfig(BackendConfig):
    """Configuration for the Docker backend.

    Populated from CLI args by DockerBackend.config_from_args().
    """
    engine: str = "docker"                    # --engine
    image: str = "debian:bookworm-slim"       # --image
    shell: str = "/bin/bash"                  # --shell
    network_enabled: bool = False             # --network
    cpu: str | None = None                    # --cpu
    memory: str | None = None                 # --memory
    docker_run_flags: list[str] = field(      # --docker-run-flag (repeatable)
        default_factory=list
    )
    default_timeout: int = 120               # --timeout (for tool description)
```

**Notes:**

- `default_timeout` is included so `tool_instructions()` can embed the actual configured value in the description text. The per-exec timeout comes via `ExecRequest.timeout`, not from this config.
- `docker_run_flags` is a list of raw strings passed directly to `docker run`. Each `--docker-run-flag` CLI invocation appends one entry.
- `engine` controls which binary is invoked in all subprocess calls. Podman is CLI-compatible with Docker, so swapping the engine name is sufficient (D10a).

---

## 3. Docker CLI Helper

Since every Docker operation is a subprocess call, a shared helper method avoids repetition and centralizes error handling. This is a private method on `DockerBackend`, also available to `DockerSandbox` (which receives the engine name at construction).

```python
async def _run_docker(
    self,
    *args: str,
    stdin_data: bytes | None = None,
    check: bool = True,
    timeout: float = 30,
) -> tuple[int, bytes, bytes]:
    """Run a Docker CLI command and return (returncode, stdout, stderr).

    Args:
        args: Command arguments after the engine name (e.g., "info", "run", "-d").
        stdin_data: Bytes to pipe to the command's stdin.
        check: If True, raise BackendError on non-zero exit.
        timeout: Seconds to wait before killing the subprocess.

    Returns:
        Tuple of (return_code, stdout_bytes, stderr_bytes).

    Raises:
        BackendError: If check=True and the command exits non-zero,
            or if the command times out.
    """
    cmd = [self._engine, *args]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(stdin_data),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise BackendError(
            f"Docker command timed out after {timeout}s: {' '.join(cmd)}"
        )

    if check and proc.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        raise BackendError(
            f"Docker command failed (exit {proc.returncode}): "
            f"{' '.join(cmd)}\n{stderr_text}"
        )
    return proc.returncode, stdout, stderr
```

**Design notes:**

- `timeout` here is for the Docker CLI command itself (e.g., image pull, container stop), not for exec-in-container. Exec has its own timeout logic (§5.3).
- `check=True` is the default because most Docker commands should succeed. Operations where failure is expected (e.g., checking if an image exists locally) pass `check=False`.
- The helper is used for simple lifecycle commands. The exec flow (§5.2) uses its own subprocess management because it needs streaming output monitoring.

---

## 4. DockerBackend

### 4.1 Validation (`_validate`)

Checks that the Docker engine is reachable and responsive.

```python
async def _validate(self) -> None:
    try:
        await self._run_docker("info", timeout=10)
    except BackendError:
        raise BackendError(
            f"Cannot connect to {self._config.engine}. "
            f"Is the {self._config.engine} daemon running?"
        )
```

**What this validates:**

- The engine binary exists on PATH and is executable.
- The daemon is running and responsive.
- The user has permission to communicate with the daemon.

**What this does NOT validate:**

- Image availability (checked during sandbox creation).
- Shell availability in the image (checked during readiness verification).
- Network connectivity for image pull (checked when pull is attempted).

Validation errors use actionable messages. If `docker info` fails with "permission denied," the error should guide the user to check their Docker group membership or socket permissions. The `BackendError` from `_run_docker` includes stderr output from the Docker CLI, which typically contains the specific error.

### 4.2 Image Management

Image pull happens during sandbox creation, not during validation. This follows the functional spec's startup sequence: Pull → Create → Verify → Accept (§4.3).

```python
async def _ensure_image(self) -> None:
    """Pull the configured image if not available locally."""
    # Check if image exists locally
    returncode, _, _ = await self._run_docker(
        "image", "inspect", self._config.image,
        check=False,
        timeout=10,
    )
    if returncode == 0:
        return  # Image already available

    # Pull with progress output to stderr
    # Don't use _run_docker because we want stderr to pass through
    # to the parent process (for progress display to the user)
    proc = await asyncio.create_subprocess_exec(
        self._config.engine, "pull", self._config.image,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=None,  # inherit parent stderr — shows pull progress
    )
    await proc.wait()
    if proc.returncode != 0:
        raise BackendError(
            f"Failed to pull image '{self._config.image}'. "
            f"Check that the image name is correct and the registry is reachable."
        )
```

**Design decisions:**

- **Explicit check-then-pull** rather than relying on `docker run` to pull automatically. This gives us control over progress display and error messages. `docker run` would pull silently (stdout/stderr captured), providing no feedback during long pulls.
- **stderr passthrough** for pull progress. In MCP stdio mode, stderr is the channel for server-to-client notifications. Docker's pull progress (layer downloads, extraction) goes directly to the user. (D31 — no logging system, but stderr for pull progress is explicitly called for.)
- **No pull timeout.** Image pulls can take minutes for large images. Adding a timeout would break legitimate use cases. The user can Ctrl+C if the pull is taking too long.

### 4.3 Sandbox Creation (`_create_sandbox`)

Creates a Docker container, verifies readiness, and returns a `DockerSandbox`.

```python
async def _create_sandbox(self) -> "DockerSandbox":
    # 1. Ensure image is available (pull if needed)
    await self._ensure_image()

    # 2. Build docker run command
    cmd = self._build_run_command()

    # 3. Create and start container
    _, stdout, _ = await self._run_docker(*cmd, timeout=30)
    container_id = stdout.decode().strip()

    # 4. Create sandbox object
    sandbox = DockerSandbox(
        engine=self._config.engine,
        shell=self._config.shell,
        container_id=container_id,
    )

    # 5. Verify readiness
    try:
        await sandbox._verify_readiness()
    except Exception:
        # Clean up the container if readiness check fails
        await sandbox.stop()
        raise

    return sandbox
```

The `_build_run_command` method assembles the full `docker run` argument list (§6.1). The readiness check (§4.3.1) confirms the container accepts exec calls before returning it to the MCP layer.

#### 4.3.1 Readiness Verification

After `docker run`, the container is running but may not be ready for exec calls (e.g., entrypoint initialization). A trivial exec confirms the sandbox is usable:

```python
async def _verify_readiness(self) -> None:
    """Verify the sandbox accepts exec calls and the shell works."""
    result = await self._run_docker(
        "exec", self._container_id,
        self._shell, "-c", "echo kilntainers-ready",
        timeout=10,
    )
    _, stdout, _ = result
    if b"kilntainers-ready" not in stdout:
        raise BackendError(
            f"Container {self._container_id[:12]} started but "
            f"readiness check failed (unexpected output)"
        )
```

**This validates two things at once:**

1. **Container readiness** — the container is running and accepts `docker exec` calls.
2. **Shell availability** — the configured `--shell` binary exists in the image and supports `-c`. If the shell doesn't exist, `docker exec` returns a non-zero exit code and the `_run_docker` helper raises `BackendError`.

If the readiness check fails (container started but shell not found), the error message should guide the user: *"Shell '/bin/bash' not found in image 'alpine:latest'. Use --shell /bin/sh for images without bash."*

### 4.4 Tool Instructions

Returns the tool description for the `terminal_execute` tool, with dynamic values from configuration. (Functional spec §7)

```python
DEFAULT_IMAGE = "debian:bookworm-slim"

def tool_instructions(self) -> str | None:
    if self._config.image != DEFAULT_IMAGE:
        return None

    shell_name = self._config.shell.rsplit("/", 1)[-1]  # basename
    timeout = self._config.default_timeout

    return (
        f"Execute a shell command in an isolated Debian Linux sandbox. "
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

**Custom image behavior:** When `--image` is anything other than the default (`debian:bookworm-slim`), `tool_instructions()` returns `None`. The baked-in description is written for the default Debian image and would be misleading for arbitrary images. The server fails to start unless the user provides `--tool-instruction-override` (Functional spec §6 rule 3).

---

## 5. DockerSandbox

### 5.1 State and Construction

```python
class DockerSandbox(Sandbox):
    def __init__(
        self,
        *,
        engine: str,
        shell: str,
        container_id: str,
    ) -> None:
        self._engine = engine
        self._shell = shell
        self._container_id = container_id
        self._stopped = False
        self._stop_requested = False
        self._exec_lock = asyncio.Lock()

    @property
    def sandbox_id(self) -> str:
        return self._container_id[:12]
```

**State fields:**

| Field | Purpose |
|---|---|
| `_engine` | CLI binary name for subprocess calls. |
| `_shell` | Shell binary for `command` mode exec. |
| `_container_id` | Full 64-character container ID from `docker run`. |
| `_stopped` | Idempotency guard for `stop()`. |
| `_stop_requested` | Distinguishes normal stop from unexpected death in `wait_for_death()`. |
| `_exec_lock` | Serializes exec calls within this sandbox (D29). |

**`sandbox_id`** returns the short form (first 12 hex characters) of the container ID. This is the conventional Docker short ID, suitable for logging and display. All Docker CLI commands use the full ID internally for unambiguous identification.

### 5.2 Command Execution

The exec flow is the most complex part of the Docker backend. It handles command construction, stdin piping, output streaming with size monitoring, timeout enforcement, and wall-clock timing — all through a `docker exec` subprocess.

```python
async def exec(self, request: ExecRequest) -> ExecResult:
    if self._stopped:
        raise SandboxDiedError("Sandbox has been stopped")

    async with self._exec_lock:
        return await self._do_exec(request)
```

The public `exec()` method checks sandbox liveness, acquires the serialization lock (D29), and delegates to `_do_exec()`. Concurrent exec calls to the same sandbox queue on the lock — no API change needed if serialization is later removed.

#### 5.2.1 Core Exec Flow

```python
async def _do_exec(self, request: ExecRequest) -> ExecResult:
    cmd = self._build_exec_command(request)
    stdin_data = request.stdin.encode("utf-8") if request.stdin else None

    start_time = time.monotonic()

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes, returncode = await asyncio.wait_for(
            self._communicate_with_limit(proc, stdin_data, request.output_limit),
            timeout=request.timeout,
        )
        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        return ExecResult(
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            exit_code=returncode,
            exec_duration_ms=elapsed_ms,
        )

    except asyncio.TimeoutError:
        await self._kill_subprocess(proc)
        elapsed_ms = int((time.monotonic() - start_time) * 1000)
        return ExecResult(
            stdout="",
            stderr=f"[kilntainers: command timed out after {request.timeout}s]",
            exit_code=124,
            exec_duration_ms=elapsed_ms,
        )

    except _OutputLimitExceeded:
        await self._kill_subprocess(proc)
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
```

**Key design choices:**

- **`asyncio.wait_for`** wraps the entire communicate-with-limit call. When the timeout fires, `wait_for` cancels the coroutine and raises `TimeoutError`. The exception handler kills the subprocess and returns the timeout ExecResult.
- **`_OutputLimitExceeded`** is a private exception raised by the output reader when combined output exceeds the limit. It follows the same pattern — kill subprocess, return error ExecResult.
- **Both conditions produce an ExecResult**, not an exception that propagates to the MCP layer. This matches the functional spec's error model (§2.5) — timeout and output limit are `isError: false`.
- **Wall-clock timing** uses `time.monotonic()` for accuracy. The timer covers everything from subprocess creation to result assembly.
- **UTF-8 decoding** with `errors="replace"` handles binary output gracefully (D27). Unmappable bytes become replacement characters rather than crashing.

#### 5.2.2 Command Construction

```python
def _build_exec_command(self, request: ExecRequest) -> list[str]:
    """Build the docker exec argument list."""
    cmd = [self._engine, "exec"]

    # -i keeps stdin open (needed when piping stdin data)
    if request.stdin is not None:
        cmd.append("-i")

    # -w sets the working directory inside the container
    if request.working_directory is not None:
        cmd.extend(["-w", request.working_directory])

    cmd.append(self._container_id)

    if request.command is not None:
        # Command mode: wrap in shell
        cmd.extend([self._shell, "-c", request.command])
    else:
        # Args mode: pass directly, no shell
        assert request.args is not None  # guaranteed by ExecRequest validation
        cmd.extend(request.args)

    return cmd
```

**Examples of constructed commands:**

| Request | Docker command |
|---|---|
| `command="ls -la"` | `docker exec <id> /bin/bash -c "ls -la"` |
| `command="cat > f.txt"`, `stdin="hello"` | `docker exec -i <id> /bin/bash -c "cat > f.txt"` |
| `args=["python3", "script.py"]` | `docker exec <id> python3 script.py` |
| `command="make"`, `working_directory="/app"` | `docker exec -w /app <id> /bin/bash -c make` |

**Shell selection** (D20): In `command` mode, the command string is passed to the configured shell via `<shell> -c <command>`. The shell handles pipes, redirects, globbing, and variable expansion. In `args` mode, arguments go directly to `docker exec` with no shell involved — argument integrity is preserved exactly.

**`-i` flag**: Only added when `stdin` is provided. Without `-i`, Docker exec doesn't connect stdin to the container process. With `-i`, the piped data reaches the command's standard input.

### 5.3 Timeout Enforcement

Timeout is enforced at the subprocess level using `asyncio.wait_for()` wrapped around the output-reading coroutine.

**Flow when timeout fires:**

1. `asyncio.wait_for` raises `TimeoutError` after `request.timeout` seconds.
2. The `_communicate_with_limit` coroutine is cancelled (stream readers stop).
3. `_kill_subprocess(proc)` sends SIGKILL to the `docker exec` process.
4. An ExecResult is returned with `exit_code=124`, empty stdout, and the timeout notice in stderr.

```python
async def _kill_subprocess(self, proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess and wait for it to exit."""
    try:
        proc.kill()  # SIGKILL
    except ProcessLookupError:
        pass  # Already exited
    await proc.wait()
```

**In-container process lifecycle:** Killing the `docker exec` subprocess terminates the exec session. The process running inside the container may continue as an orphan until the container is stopped. This is acceptable because:

1. The `_exec_lock` prevents new exec calls from starting until the current one completes.
2. The container will be stopped when the session ends, which kills all processes inside it.
3. Orphaned processes consume container resources but not host resources beyond what cgroups allow.

### 5.4 Output Limit Enforcement

Output is read from stdout and stderr concurrently, with a combined byte counter. When the limit is exceeded, reading stops and the process is killed.

```python
class _OutputLimitExceeded(Exception):
    """Internal signal: combined output exceeded the configured limit."""
    pass


async def _communicate_with_limit(
    self,
    proc: asyncio.subprocess.Process,
    stdin_data: bytes | None,
    output_limit: int,
) -> tuple[bytes, bytes, int]:
    """Read process output with combined size enforcement.

    Similar to proc.communicate() but monitors combined stdout+stderr
    byte count and raises _OutputLimitExceeded if the limit is breached.

    Returns (stdout_bytes, stderr_bytes, returncode).
    """
    # Write stdin if provided
    if stdin_data is not None and proc.stdin is not None:
        proc.stdin.write(stdin_data)
        await proc.stdin.drain()
        proc.stdin.close()

    total_bytes = 0
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def read_stream(
        stream: asyncio.StreamReader, chunks: list[bytes]
    ) -> None:
        nonlocal total_bytes
        while True:
            chunk = await stream.read(8192)
            if not chunk:
                break  # EOF
            total_bytes += len(chunk)
            if total_bytes > output_limit:
                raise _OutputLimitExceeded()
            chunks.append(chunk)

    # Read both streams concurrently. TaskGroup cancels the other
    # reader if one raises _OutputLimitExceeded.
    async with asyncio.TaskGroup() as tg:
        tg.create_task(read_stream(proc.stdout, stdout_chunks))
        tg.create_task(read_stream(proc.stderr, stderr_chunks))

    await proc.wait()

    return (
        b"".join(stdout_chunks),
        b"".join(stderr_chunks),
        proc.returncode,
    )
```

**Design details:**

- **Streaming reads** in 8 KiB chunks. This detects limit violations promptly without reading the entire output into memory first. The 8 KiB chunk size balances responsiveness with read efficiency.
- **Combined counter** (`total_bytes`) tracks stdout + stderr together (Functional spec §2.4). The counter is a `nonlocal` variable shared between the two reader tasks. This is safe in asyncio — both tasks run in the same thread, and the increment/check sequence has no `await` points between them.
- **`asyncio.TaskGroup`** is used instead of `asyncio.gather` because TaskGroup cancels sibling tasks when one raises an exception. When the output limit is hit on one stream, the other stream's reader is cancelled immediately. `asyncio.gather` does not cancel siblings by default.
- **`_OutputLimitExceeded`** is a private exception — it never escapes `_do_exec`. The exception handler in `_do_exec` catches it, kills the subprocess, and returns an ExecResult with the appropriate error message.
- **No partial output** is returned when the limit is exceeded. The functional spec is explicit about this (§2.4): truncated output is worse than no output because the agent doesn't know what's missing.
- **`asyncio.TaskGroup` and `_OutputLimitExceeded`**: When `read_stream` raises `_OutputLimitExceeded`, the TaskGroup wraps it in an `ExceptionGroup`. The exception handler in `_do_exec` should catch `ExceptionGroup` and extract the `_OutputLimitExceeded`. Alternatively, `_OutputLimitExceeded` can be made a subclass of `BaseException` to exit the TaskGroup directly. The implementation should handle this wrapping correctly — the architecture documents the intent, and the exact exception handling mechanics will be resolved during implementation.

### 5.5 Stdin Piping

Stdin data flows through the `docker exec -i` subprocess. The `-i` flag tells Docker to keep stdin connected to the container process.

**Flow:**

1. MCP layer constructs `ExecRequest` with `stdin="file content..."`.
2. `_build_exec_command` adds `-i` to the `docker exec` arguments.
3. `_communicate_with_limit` writes the encoded stdin data to `proc.stdin`, drains the buffer, and closes the stream.
4. Inside the container, the command reads from stdin (e.g., `cat > file.txt`).

**Stdin size limit** (2 MiB, D32) is enforced by the MCP layer before the ExecRequest reaches the backend. The backend does not re-check the limit.

**Encoding:** `request.stdin` is a Python string. It's encoded to `bytes` as UTF-8 before writing to the subprocess. This matches the MCP protocol's JSON transport (all strings are UTF-8).

### 5.6 Stop

```python
async def stop(self) -> None:
    if self._stopped:
        return
    self._stopped = True
    self._stop_requested = True

    try:
        # docker stop sends SIGTERM, waits grace period, then SIGKILL
        proc = await asyncio.create_subprocess_exec(
            self._engine, "stop", "-t", "5", self._container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            # 5s Docker grace + 5s buffer for Docker overhead
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    except Exception:
        # Best-effort cleanup — don't propagate errors from stop
        pass
```

**Design details:**

- **Idempotent** — the `_stopped` flag ensures double-stop is a no-op (Phase 2 contract).
- **`_stop_requested` flag** is set before issuing the stop command. This tells `wait_for_death()` that the container exit is expected (§5.7).
- **`docker stop -t 5`** sends SIGTERM, waits 5 seconds, then sends SIGKILL. This is shorter than the default 10 seconds — for ephemeral sandboxes, fast teardown is preferred over graceful process shutdown.
- **10-second outer timeout** covers the Docker stop command plus overhead. If Docker itself stalls, we kill the stop subprocess and proceed. The container may be orphaned but is labeled for manual cleanup (§6.5).
- **`--rm` flag** on `docker run` (§6.1) means the container is automatically removed when it stops. No separate `docker rm` call is needed.
- **Exception swallowing** — stop is best-effort. If Docker isn't responding, there's nothing useful to do except proceed with cleanup. Errors during stop should not propagate to the MCP layer or prevent session teardown.

### 5.7 Death Detection

`wait_for_death()` monitors the container for unexpected termination using `docker wait`, which blocks until the container exits.

```python
async def wait_for_death(self) -> None:
    proc = await asyncio.create_subprocess_exec(
        self._engine, "wait", self._container_id,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await proc.wait()
    except asyncio.CancelledError:
        # Normal shutdown — MCP layer cancelled this task before stop()
        proc.kill()
        await proc.wait()
        raise

    if self._stop_requested:
        # Container exited because stop() was called — this is expected.
        # Block forever; the MCP layer will cancel this task.
        try:
            await asyncio.Future()  # never completes
        except asyncio.CancelledError:
            return

    # Container exited unexpectedly (OOM, external kill, daemon crash).
    # Returning signals death to the MCP layer.
```

**How it works with the MCP layer (Phase 2 §6.3):**

1. After sandbox creation, the MCP layer starts a background task: `death_task = asyncio.create_task(sandbox.wait_for_death())`.
2. `docker wait` blocks until the container exits.
3. **Normal shutdown path:** The MCP layer calls `death_task.cancel()` before `sandbox.stop()`. The `CancelledError` is caught, the `docker wait` subprocess is cleaned up, and the task ends.
4. **Unexpected death path:** The container exits without `stop()` being called. `docker wait` returns. `_stop_requested` is `False`. The method returns normally, which resolves the background task. The MCP layer detects this and drops the connection (§4.5 of functional spec).
5. **Race condition handling:** If `stop()` is called but the cancellation hasn't reached this task yet, `docker wait` returns with `_stop_requested=True`. The method blocks on an unresolvable `Future()`, and the MCP layer's subsequent cancellation cleans it up.

**`docker wait` vs polling:** `docker wait` is the right tool — it's a single blocking call with no polling overhead. Docker notifies immediately when the container exits. The alternative (polling `docker inspect` in a loop) would add latency and unnecessary load.

---

## 6. Container Configuration

### 6.1 Docker Run Command

`_build_run_command` assembles the full argument list for `docker run`.

```python
def _build_run_command(self) -> list[str]:
    """Build the docker run argument list."""
    cmd = [
        "run", "-d",                          # detached mode
        "--rm",                                # auto-remove on stop
        "--label", "kilntainers=true",         # identification label
    ]

    # Network isolation (default: disabled)
    if not self._config.network_enabled:
        cmd.extend(["--network", "none"])

    # Resource limits
    if self._config.cpu is not None:
        cmd.extend(["--cpus", self._config.cpu])
    if self._config.memory is not None:
        cmd.extend(["--memory", self._config.memory])

    # User-provided extra flags (escape hatch)
    for flag in self._config.docker_run_flags:
        cmd.append(flag)

    # Image and keep-alive command
    cmd.append(self._config.image)
    cmd.extend(["tail", "-f", "/dev/null"])

    return cmd
```

**Resulting command example (defaults):**

```
docker run -d --rm --label kilntainers=true --network none \
    debian:bookworm-slim tail -f /dev/null
```

### 6.2 Keep-Alive Process

The container runs `tail -f /dev/null` as its main process. This is a minimal, universally available command that:

- Keeps the container alive indefinitely (PID 1 must stay running).
- Consumes negligible resources (no CPU, minimal memory).
- Exists in virtually all Linux images (including busybox, alpine, debian, ubuntu).
- Does not interfere with exec'd commands.

The alternative `sleep infinity` also works on most images but is not supported by all implementations of `sleep` (e.g., very old busybox versions). `tail -f /dev/null` is the most portable option.

### 6.3 Network Isolation

Network is **disabled by default** (`--network none`). This is a core security property (D5, Functional spec §9.1).

- **`--network` flag NOT set** (default): `--network none` is added to `docker run`. The container has no network interfaces (except loopback). DNS resolution, outbound connections, and listening sockets all fail.
- **`--network` flag set**: `--network none` is omitted. The container uses Docker's default bridge network with full internet access.

There is no middle ground in v1 — network is fully off or fully on. Fine-grained network policies (allow specific hosts, restrict ports) are possible via `--docker-run-flag` but not exposed as named parameters.

### 6.4 Resource Limits

Resource limits are purely Docker-backend-specific configuration (D26). They map directly to Docker CLI flags:

| Parameter | Docker flag | Example |
|---|---|---|
| `--cpu` | `--cpus` | `--cpu 1.5` → `--cpus 1.5` |
| `--memory` | `--memory` | `--memory 512m` → `--memory 512m` |

**Default: no limits.** Docker defaults apply, which effectively means the container can use all host resources. For shared or production deployments, operators should set limits (Functional spec §9.3).

**Additional limits** (PID limits, disk quotas, etc.) are available through the `--docker-run-flag` escape hatch (§6.5).

### 6.5 Custom Docker Flags

`--docker-run-flag` is a repeatable CLI parameter that passes arbitrary flags to `docker run`. Each invocation provides a single flag string.

**Example:**

```bash
kilntainers --docker-run-flag "--pids-limit=256" \
            --docker-run-flag "--read-only" \
            --docker-run-flag "--tmpfs /tmp:size=100m"
```

Flags are appended to the `docker run` command after all named parameters but before the image name. This positioning ensures they can override earlier flags if needed (Docker uses last-wins for conflicting flags).

**Security note** (Functional spec §9.3): This escape hatch can weaken isolation. Flags like `--privileged`, `--pid=host`, or `-v /:/host` would compromise the sandbox's security properties. Operators are responsible for reviewing any custom flags they add.

### 6.6 Labeling and Cleanup

All containers are created with `--label kilntainers=true` for identification. Combined with `--rm`, this provides the orphan management strategy:

- **Normal operation:** `--rm` auto-removes the container when it stops. No orphans.
- **Server crash:** The container keeps running (not stopped, so `--rm` doesn't trigger). The label allows manual discovery and cleanup:

```bash
# Find orphaned containers
docker ps --filter label=kilntainers=true

# Stop and remove all kilntainers containers
docker stop $(docker ps -q --filter label=kilntainers=true)
```

A `kilntainers cleanup` CLI subcommand is deferred to a future version (Functional spec §5.4).

---

## 7. Error Handling Summary

This section maps Docker CLI failures to the error model from Phase 2. Phase 7 (Error Handling & Observability) covers the full error propagation architecture.

| Docker failure | Raised as | MCP result |
|---|---|---|
| `docker info` fails (validation) | `BackendError` | Server fails to start |
| `docker pull` fails | `BackendError` | Server fails to start (stdio) or session fails to initialize (HTTP) |
| `docker run` fails | `BackendError` | Same as pull failure |
| Readiness check fails | `BackendError` | Same — shell not found or container unhealthy |
| `docker exec` — command succeeds | `ExecResult` (exit 0) | `isError: false` |
| `docker exec` — command fails | `ExecResult` (non-zero exit) | `isError: false` |
| `docker exec` — timeout | `ExecResult` (exit 124) | `isError: false` |
| `docker exec` — output limit | `ExecResult` (exit 1) | `isError: false` |
| Container died during exec | `SandboxDiedError` | `isError: true`, connection drops |
| Container died between execs | `wait_for_death()` returns | Connection drops |
| `docker stop` fails | Swallowed (best-effort) | N/A — already shutting down |

**`SandboxDiedError` detection during exec:** If `docker exec` returns exit code 137 (SIGKILL) or the process exits abnormally, the sandbox should verify the container is still running before returning the result. If the container has exited, raise `SandboxDiedError` instead of returning an ExecResult. This check prevents returning a confusing result when the container was killed during execution.

```python
# After docker exec completes, check if the container is still alive
if returncode != 0:
    alive = await self._is_container_running()
    if not alive and not self._stop_requested:
        raise SandboxDiedError(
            f"Sandbox {self.sandbox_id} died during command execution"
        )
```

The `_is_container_running` check is a lightweight `docker inspect --format '{{.State.Running}}'` call.

---

## 8. Testing

### 8.1 Unit Tests (`tests/unit/backends/test_docker.py`)

Unit tests mock `asyncio.create_subprocess_exec` to simulate Docker CLI responses without a Docker daemon. This tests the Docker backend's logic — command construction, output parsing, timeout handling, limit enforcement — in isolation.

#### Mock Strategy

A fixture provides a mock subprocess factory that returns preconfigured responses:

```python
@pytest.fixture
def mock_subprocess(monkeypatch):
    """Mock asyncio.create_subprocess_exec with configurable responses."""
    responses = []

    class MockProcess:
        def __init__(self, returncode, stdout, stderr):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr
            self.stdin = MockStdin()
            self.stdout = MockStreamReader(stdout)
            self.stderr = MockStreamReader(stderr)
            self.pid = 12345

        async def wait(self):
            return self.returncode

        async def communicate(self, input=None):
            return self._stdout, self._stderr

        def kill(self):
            pass

    async def create_mock(*args, **kwargs):
        if responses:
            return responses.pop(0)
        return MockProcess(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)
    return responses
```

#### Test Cases

**Validation:**
- `docker info` succeeds → validation passes.
- `docker info` fails → `BackendError` with actionable message.
- Second `validate()` call is a no-op (caching).

**Image management:**
- Image exists locally (`docker image inspect` succeeds) → no pull.
- Image not local → `docker pull` called, succeeds.
- Image not local → `docker pull` fails → `BackendError`.

**Sandbox creation:**
- Full sequence: image check → pull (if needed) → `docker run` → readiness check → sandbox returned.
- `docker run` fails → `BackendError`.
- Readiness check fails (shell not found) → `BackendError`, container is stopped.

**Command construction:**
- `command` mode → shell wrapping: `[engine, exec, container_id, shell, -c, command]`.
- `args` mode → direct: `[engine, exec, container_id, args...]`.
- `stdin` present → `-i` flag added.
- `working_directory` present → `-w dir` added.
- Combinations of the above.

**Exec — normal results:**
- Successful command → ExecResult with stdout, stderr, exit_code=0, duration.
- Failed command (non-zero exit) → ExecResult with exit code preserved.
- Empty output → ExecResult with empty strings.

**Exec — timeout:**
- Command exceeds timeout → ExecResult with exit_code=124, empty stdout, timeout stderr message.
- Duration reflects actual wall-clock time (not timeout value).

**Exec — output limit:**
- Output exceeds limit → ExecResult with exit_code=1, empty stdout, limit stderr message.
- Limit applies to combined stdout+stderr.
- Just under limit → output returned normally.
- stdout over limit → caught.
- stderr over limit → caught.
- Neither alone over limit, combined over limit → caught.

**Exec — stdin:**
- `stdin` provided → data written to subprocess stdin, stdin closed.
- `stdin` not provided → subprocess stdin is DEVNULL.

**Exec — serialization:**
- Concurrent exec calls are serialized by the lock.

**Stop:**
- `docker stop` called with container ID.
- Idempotent — second stop is a no-op.
- `_stop_requested` flag is set.

**Death detection:**
- Container exits unexpectedly → `wait_for_death()` returns.
- `stop()` called → `wait_for_death()` blocks (does not signal death).
- Task cancelled → subprocess cleaned up.

**Tool instructions:**
- Default image → returns description with dynamic values.
- Custom image → returns `None`.

**Docker run command construction:**
- Default config → includes `--rm`, `--label`, `--network none`.
- Network enabled → no `--network none`.
- CPU set → `--cpus` flag added.
- Memory set → `--memory` flag added.
- Custom flags → appended in order.

### 8.2 Integration Tests (`tests/integration/test_docker_integration.py`)

Integration tests run against a real Docker daemon. They create actual containers, execute real commands, and verify end-to-end behavior. These tests require Docker to be available (skipped in environments without Docker).

```python
@pytest.fixture
async def docker_sandbox():
    """Create a real Docker sandbox for testing."""
    config = DockerBackendConfig()  # all defaults
    backend = DockerBackend(config)
    sandbox = await backend.create_sandbox()
    yield sandbox
    await sandbox.stop()
```

#### Test Cases

**Lifecycle:**
- Create sandbox → readiness check passes → sandbox_id is valid.
- Stop sandbox → container no longer running.
- Stop is idempotent.

**Basic exec:**
- `echo hello` → stdout "hello\n", exit_code 0.
- `false` → exit_code 1.
- `ls /nonexistent` → non-zero exit, stderr with error message.

**Command mode vs args mode:**
- `command="echo hello | tr a-z A-Z"` → stdout "HELLO\n" (shell features work).
- `args=["echo", "hello world"]` → stdout "hello world\n" (no shell splitting).

**Working directory:**
- `command="pwd"`, `working_directory="/tmp"` → stdout "/tmp\n".
- Default working directory matches image WORKDIR.

**Stdin:**
- `command="cat > /tmp/test.txt"`, `stdin="file content"` then `command="cat /tmp/test.txt"` → stdout "file content".
- `command="wc -c"`, `stdin="hello"` → stdout "5\n".
- Special characters in stdin (quotes, newlines, JSON) preserved exactly.

**Timeout:**
- `command="sleep 60"`, `timeout=2` → exit_code 124, timeout message in stderr, duration ~2s.

**Output limit:**
- `command="yes"`, `output_limit=1000` → exit_code 1, limit message in stderr.
- `command="head -c 500 /dev/urandom | base64"`, `output_limit=2000` → output returned (under limit).

**Stateless execution:**
- `command="export FOO=bar"` then `command="echo $FOO"` → stdout empty (no state between calls).
- `command="cd /tmp"` then `command="pwd"` → default working directory (not /tmp).

**Network isolation:**
- Default (no network): `command="curl -s http://example.com"` or similar → fails.
- With network enabled: outbound connections succeed (if test environment has network).

**Death detection:**
- Kill the container externally (`docker kill`), verify `wait_for_death()` resolves.

**Cleanup:**
- After stop, container is removed (`--rm` behavior).
- Container has `kilntainers=true` label while running.

### 8.3 Test Utilities

A `skip_without_docker` marker for docker integration tests:

```python
import shutil
import pytest

skip_without_docker = pytest.mark.skipif(
    not shutil.which("docker"),
    reason="Docker not available"
)
```

Applied to all docker integration test classes/modules with `@pytest.mark.integration`. CI runs on `ubuntu-latest` which includes Docker; local development environments may not.

---

## 9. Implementation Notes

### 9.1 asyncio.TaskGroup Exception Wrapping

When `_OutputLimitExceeded` is raised inside an `asyncio.TaskGroup`, it gets wrapped in an `ExceptionGroup`. The `_do_exec` method must handle this correctly. Options:

1. **Catch `ExceptionGroup`** and check for `_OutputLimitExceeded` inside:
   ```python
   except* _OutputLimitExceeded:
       # Python 3.11+ except* syntax for ExceptionGroups
       await self._kill_subprocess(proc)
       ...
   ```

2. **Use `BaseException` subclass** for `_OutputLimitExceeded` so it exits the TaskGroup without wrapping.

Option 1 (`except*` syntax) is the cleaner approach and aligns with modern Python exception handling. Since we target Python 3.13 (Phase 1), the `except*` syntax is available.

### 9.2 Subprocess Cleanup

Every subprocess started by the Docker backend must be properly cleaned up:

- **`_run_docker` helper:** Handles its own cleanup (timeout → kill → wait).
- **Exec subprocess:** Cleaned up in `_do_exec` exception handlers (kill → wait).
- **`docker wait` subprocess:** Cleaned up in `wait_for_death` on cancellation (kill → wait).

Leaked subprocesses would accumulate zombie processes. The `await proc.wait()` after every `proc.kill()` ensures the OS-level process is reaped.

### 9.3 Container ID Handling

`docker run -d` outputs the full 64-character container ID to stdout. This is stored as `_container_id` and used for all subsequent Docker commands. The short form (first 12 characters) is exposed via `sandbox_id` for display purposes.

All Docker CLI commands accept both full and short IDs. We use the full ID internally to avoid any ambiguity in environments with many containers.

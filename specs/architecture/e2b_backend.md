# Architecture: E2B Backend Implementation

Defines how the E2B backend implements the Backend and Sandbox ABCs from Phase 2 using the E2B Python SDK. Covers authentication, sandbox lifecycle, exec flow with timeout and output-limit enforcement, death detection, and resource configuration.

**References:** Functional spec §3.2, §4.3, §4.4, §5, §7, §8.4, §9. Phase 2 (backend abstraction). E2B docs: [Sandbox](https://e2b.dev/docs/sandbox), [Commands](https://e2b.dev/docs/sdk-reference/python-sdk/sandbox_sync#commands), [Templates](https://e2b.dev/docs/sandbox-template), [API Key](https://e2b.dev/docs/api-key).

---

## 1. Overview

The E2B backend consists of two classes in `src/kilntainers/backends/e2b.py`:

- **`E2BBackend(Backend)`** — Validates E2B authentication and connectivity, constructs template references, creates sandboxes, and provides tool description text.
- **`E2BSandbox(Sandbox)`** — Wraps a running E2B Sandbox. Handles command execution with timeout and output-limit enforcement, stop, and death detection.

All interaction with E2B happens through the **E2B Python SDK** (`e2b` package). Unlike the Docker backend which shells out to a CLI binary, the E2B backend uses the SDK's async API directly. No subprocess management is involved — the E2B SDK handles all communication with E2B's cloud infrastructure.

**Key differences from other backends:**

| Concern | Docker | Modal | E2B |
|---|---|---|---|
| Interface | CLI subprocess (`docker` binary) | Python SDK (`modal` package) | Python SDK (`e2b` package) |
| Execution location | Local container on host | Remote container on Modal's cloud | Remote micro-VM on E2B's cloud |
| Image management | Explicit pull, local cache | `modal.Image` (build + cache) | Pre-built templates (CLI-built from Dockerfiles) |
| Keep-alive process | `tail -f /dev/null` | Not needed | Not needed — sandboxes stay alive until timeout |
| Network isolation | `--network none` | `block_network=True` | `allow_internet_access=False` |
| Death detection | `docker wait` (blocking) | `sb.wait.aio()` (blocking) | Fail on next exec (no polling) |
| Timeout enforcement | Client-side (`asyncio.wait_for`) | Server-side + client-side | Server-side (E2B kills) + client-side safety net |
| Auth | Docker daemon socket | Token-based (token ID + secret) | API key (`E2B_API_KEY`) |
| Cost | Local compute only | Pay-per-use cloud compute | Pay-per-use cloud compute |

---

## 2. Configuration

The E2B backend receives its configuration as a typed dataclass. As with the Docker backend, the full definition lives alongside CLI & configuration code (Phase 5). This section documents what the E2B backend needs.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class E2BBackendConfig(BackendConfig):
    """Configuration for the E2B backend.

    Populated from CLI args by E2BBackend.config_from_args().
    """
    # Authentication (optional — falls back to E2B_API_KEY env var)
    api_key: str | None = None                    # --e2b-api-key

    # Sandbox template
    template: str = "base"                        # --e2b-template
    shell: str = "/bin/bash"                      # --shell
    network_enabled: bool = False                 # --network

    # Resources (template-defined, not runtime configurable in base SDK)
    # Note: CPU/memory are set when building templates, not at sandbox creation

    # Sandbox lifetime
    sandbox_timeout: int = 3600                   # --e2b-sandbox-timeout (seconds)

    # Custom metadata
    metadata: dict[str, str] | None = None        # --e2b-metadata (key=value pairs)

    # Environment variables for sandbox
    envs: dict[str, str] | None = None            # --e2b-env (key=value pairs)

    # Tool description
    default_timeout: int = 120                    # --timeout (for tool description)
```

**Notes:**

- `api_key` is optional. When not provided, the E2B SDK uses the `E2B_API_KEY` environment variable. When provided, it's passed directly to `Sandbox.create()`.
- `template` defaults to `"base"` — E2B's default sandbox template (Debian-based). Custom templates are created via the E2B CLI (`e2b template build`) from Dockerfiles and referenced by name or ID.
- `sandbox_timeout` is the maximum lifetime of the E2B sandbox. Default is 1 hour (3600 seconds). E2B's maximum is 1 hour for Base tier, 24 hours for Pro tier. The sandbox is terminated when the session ends regardless, but this caps runaway sandboxes.
- `default_timeout` is used only for the tool description text (same pattern as Docker/Modal).
- `network_enabled` defaults to `False` for security, but note that E2B's default is the opposite (`allow_internet_access=True`).
- `metadata` allows operators to tag sandboxes for tracking/billing purposes.
- `envs` allows setting environment variables that persist across all commands in the sandbox.

---

## 3. E2B SDK Interaction

### 3.1 Async API

The E2B SDK provides both synchronous and asynchronous versions. The async SDK is in the `e2b.aio` module:

```python
# Sync
from e2b import Sandbox
sb = Sandbox.create()

# Async
from e2b.aio import Sandbox
sb = await Sandbox.create()
```

The E2B backend uses the async API exclusively, since the backend abstraction layer is async and the MCP server runs in an asyncio event loop.

### 3.2 Authentication Setup

When a custom API key is provided via CLI args, it's passed directly to `Sandbox.create()`. Otherwise, the SDK reads from the `E2B_API_KEY` environment variable automatically.

```python
def _get_api_params(self) -> dict:
    """Get API parameters for E2B SDK calls."""
    params = {}
    if self._config.api_key is not None:
        params["api_key"] = self._config.api_key
    return params
```

**Design decision:** Passing the API key directly to SDK methods (rather than setting environment variables) is cleaner and avoids global state mutation. The E2B SDK supports this pattern natively.

**Security note:** The `--e2b-api-key` CLI arg will be visible in the process's environment. This is acceptable since the process is short-lived and controlled by the operator. For production deployments, operators should prefer setting `E2B_API_KEY` directly (no CLI args needed).

### 3.3 Template System

E2B uses a **template system** rather than direct Docker image references:

1. **Pre-built templates:** Operators create templates using the E2B CLI (`e2b template build`) from a Dockerfile. The build process converts the Docker image into an E2B-compatible micro-VM image.
2. **Template reference:** At runtime, templates are referenced by name or ID (e.g., `"base"`, `"my-agent-sandbox"`, `"abc123xyz"`).
3. **No runtime image building:** Unlike Modal, E2B cannot build images on-demand. Templates must be pre-built.

This means the `--image` pattern from Docker/Modal doesn't translate directly. Instead:

- `--e2b-template base` → Use E2B's default template
- `--e2b-template my-custom-template` → Use a pre-built custom template

---

## 4. E2BBackend

### 4.1 Validation (`_validate`)

Checks that E2B authentication is configured and the API is reachable.

```python
async def _validate(self) -> None:
    api_params = self._get_api_params()

    try:
        # Validate auth by listing sandboxes (lightweight API call)
        from e2b.aio import Sandbox
        paginator = await Sandbox.list(**api_params)
        # Just accessing the paginator validates auth
    except Exception as e:
        error_msg = str(e).lower()
        if "unauthorized" in error_msg or "api key" in error_msg or "401" in error_msg:
            raise BackendError(
                "E2B authentication failed. Either:\n"
                "  - Set the E2B_API_KEY environment variable\n"
                "  - Pass --e2b-api-key <key>\n"
                "Get your API key from: https://e2b.dev/dashboard?tab=keys"
            )
        raise BackendError(
            f"Cannot connect to E2B. Check your network connection "
            f"and E2B service status. Error: {e}"
        )
```

**What this validates:**

- E2B API key exists and is valid.
- The E2B API is reachable (network connectivity).

**What this does NOT validate:**

- Template availability (checked during sandbox creation).
- Shell availability in the template (checked during readiness verification).

### 4.2 Sandbox Creation (`_create_sandbox`)

Creates an E2B sandbox, verifies readiness, and returns an `E2BSandbox`.

```python
async def _create_sandbox(self) -> "E2BSandbox":
    from e2b.aio import Sandbox

    api_params = self._get_api_params()

    # Build creation parameters
    create_kwargs = {
        "template": self._config.template,
        "timeout": self._config.sandbox_timeout,
        "allow_internet_access": self._config.network_enabled,
        **api_params,
    }

    if self._config.metadata:
        create_kwargs["metadata"] = self._config.metadata

    if self._config.envs:
        create_kwargs["envs"] = self._config.envs

    try:
        sb = await Sandbox.create(**create_kwargs)
    except Exception as e:
        error_msg = str(e).lower()
        if "template" in error_msg or "not found" in error_msg:
            raise BackendError(
                f"E2B template not found: '{self._config.template}'. "
                f"Check that the template exists and is accessible. "
                f"List templates with: e2b template list"
            )
        raise BackendError(f"Failed to create E2B sandbox: {e}")

    # Create sandbox wrapper
    sandbox = E2BSandbox(
        e2b_sandbox=sb,
        shell=self._config.shell,
    )

    # Verify readiness
    try:
        await sandbox._verify_readiness()
    except Exception:
        await sandbox.stop()
        raise

    return sandbox
```

**Notes:**

- `allow_internet_access` is the E2B parameter for network access. When `--network` is not passed (default), `allow_internet_access=False` — the sandbox has no network access.
- `timeout` is the sandbox lifetime (default 1 hour), not the exec timeout. The sandbox stays alive for this duration unless explicitly terminated.
- Template availability is checked implicitly during `Sandbox.create()`.

#### 4.2.1 Readiness Verification

After creation, a trivial exec confirms the sandbox is usable:

```python
async def _verify_readiness(self) -> None:
    """Verify the sandbox accepts exec calls and the shell works."""
    try:
        result = await self._e2b_sandbox.commands.run(
            f"{self._shell} -c 'echo kilntainers-ready'",
            timeout=10,
        )
        stdout = result.stdout
    except Exception as e:
        raise BackendError(
            f"Sandbox readiness check failed: {e}"
        )

    if "kilntainers-ready" not in stdout:
        raise BackendError(
            f"Sandbox started but readiness check failed (unexpected output). "
            f"Shell '{self._shell}' may not be available in the template. "
            f"Use --shell /bin/sh for templates without bash."
        )
```

This validates:

1. **Sandbox readiness** — the sandbox is running and accepts exec calls.
2. **Shell availability** — the configured `--shell` binary exists and supports `-c`.

### 4.3 Tool Instructions

Returns the tool description for the `terminal_execute` tool, with dynamic values from configuration.

```python
def tool_instructions(self) -> str | None:
    if self._config.template != "base":
        return None

    shell_name = self._config.shell.rsplit("/", 1)[-1]  # basename
    timeout = self._config.default_timeout

    return (
        f"Execute a shell command in a remote cloud sandbox (E2B). "
        f"Commands run in {shell_name}. Each call is independent — "
        f"no state (shell variables, working directory, background "
        f"processes) persists between calls. Use the working_directory "
        f"parameter or chain commands with && to control execution context."
        f"\n\n"
        f"To write files or pass data without shell escaping, use the "
        f"stdin parameter (e.g., command=\"cat > file.txt\" with content "
        f"in stdin). Commands time out after {timeout} seconds by default "
        f"(override with the timeout parameter for long-running operations)."
    )
```

**Custom template behavior:** When `--e2b-template` is set to a non-default value, `tool_instructions()` returns `None`. The server requires `--tool-instruction-override` — same pattern as Docker/Modal.

---

## 5. E2BSandbox

### 5.1 State and Construction

```python
class E2BSandbox(Sandbox):
    def __init__(
        self,
        *,
        e2b_sandbox: "e2b.aio.Sandbox",
        shell: str,
    ) -> None:
        self._e2b_sandbox = e2b_sandbox
        self._shell = shell
        self._stopped = False
        self._stop_requested = False
        self._exec_lock = asyncio.Lock()

    @property
    def sandbox_id(self) -> str:
        return self._e2b_sandbox.sandbox_id
```

**State fields:**

| Field | Purpose |
|---|---|
| `_e2b_sandbox` | The E2B SDK Sandbox object. All exec/stop calls go through this. |
| `_shell` | Shell binary for `command` mode exec. |
| `_stopped` | Idempotency guard for `stop()`. |
| `_stop_requested` | Distinguishes normal stop from unexpected death in `wait_for_death()`. |
| `_exec_lock` | Serializes exec calls within this sandbox (D29). |

**`sandbox_id`** returns E2B's `sandbox_id` — a unique identifier assigned by E2B (e.g., `"iiny0783cype8gmoawzmx-ce30bc46"`). This serves the same role as the Docker container short ID (logging, session mapping).

### 5.2 Command Execution

The exec flow uses the E2B SDK's `sandbox.commands.run()` method, which returns a `CommandResult` with stdout/stderr.

```python
async def exec(self, request: ExecRequest) -> ExecResult:
    if self._stopped:
        raise SandboxDiedError("Sandbox has been stopped")

    async with self._exec_lock:
        return await self._do_exec(request)
```

Same pattern as Docker/Modal — check liveness, acquire the serialization lock, delegate.

#### 5.2.1 Core Exec Flow

```python
async def _do_exec(self, request: ExecRequest) -> ExecResult:
    cmd = self._build_command(request)
    run_kwargs = self._build_run_kwargs(request)

    start_time = time.monotonic()

    try:
        # E2B commands.run() is a blocking call that waits for completion
        # We need to handle stdin specially if provided
        if request.stdin is not None:
            # Run command in background, send stdin, then wait
            handle = await self._e2b_sandbox.commands.run(
                cmd,
                background=True,
                **run_kwargs,
            )
            await self._e2b_sandbox.commands.send_stdin(handle.pid, request.stdin)
            result = await handle.wait()
        else:
            result = await self._e2b_sandbox.commands.run(
                cmd,
                background=False,
                **run_kwargs,
            )

        stdout_str = result.stdout or ""
        stderr_str = result.stderr or ""
        exit_code = result.exit_code

        # Check output limit
        combined_size = len(stdout_str.encode("utf-8")) + len(stderr_str.encode("utf-8"))
        if combined_size > request.output_limit:
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

        elapsed_ms = int((time.monotonic() - start_time) * 1000)

        return ExecResult(
            stdout=stdout_str,
            stderr=stderr_str,
            exit_code=exit_code,
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

    except Exception as e:
        error_msg = str(e).lower()
        if "sandbox" in error_msg and ("killed" in error_msg or "terminated" in error_msg):
            if not self._stop_requested:
                raise SandboxDiedError(
                    f"Sandbox {self.sandbox_id} died during command execution"
                )
            raise SandboxDiedError("Sandbox has been stopped")
        # Re-raise unexpected errors
        raise
```

**Key design choices:**

- **E2B's exec timeout** is passed to `commands.run()` via the `timeout` parameter, so the process is killed server-side when the timeout fires.
- **Client-side timeout** (`asyncio.wait_for` wrapper, if needed) acts as a safety net for network issues.
- **Output limit** is enforced client-side after receiving the full output. Unlike Modal/Docker where we stream and count, E2B returns the complete output at once. For very large outputs, this could be memory-intensive — a consideration for future optimization.
- **Stdin handling** requires running the command in background mode, sending stdin via `send_stdin()`, then waiting for completion.

#### 5.2.2 Command Construction

```python
def _build_command(self, request: ExecRequest) -> str:
    """Build the command string for E2B commands.run()."""
    if request.command is not None:
        # Command mode: wrap in shell
        # E2B's run() takes a string command, so we construct the shell invocation
        return f"{self._shell} -c {shlex.quote(request.command)}"
    else:
        # Args mode: join arguments with proper quoting
        assert request.args is not None
        return " ".join(shlex.quote(arg) for arg in request.args)

def _build_run_kwargs(self, request: ExecRequest) -> dict:
    """Build keyword args for E2B commands.run()."""
    kwargs: dict = {
        "timeout": request.timeout,
    }
    if request.working_directory is not None:
        kwargs["cwd"] = request.working_directory
    return kwargs
```

**Examples of constructed calls:**

| Request | E2B run call |
|---|---|
| `command="ls -la"` | `commands.run("/bin/bash -c 'ls -la'", timeout=120)` |
| `command="cat > f.txt"`, `stdin="hello"` | Background run + `send_stdin()` |
| `args=["python3", "script.py"]` | `commands.run("'python3' 'script.py'", timeout=120)` |
| `command="make"`, `working_directory="/app"` | `commands.run("/bin/bash -c 'make'", timeout=120, cwd="/app")` |

**Shell selection:** Same pattern as Docker/Modal — `command` mode wraps in `<shell> -c <command>`; `args` mode constructs a quoted command string.

### 5.3 Timeout Enforcement

Timeout is enforced at **two levels**:

1. **Server-side (E2B):** The `timeout` parameter on `commands.run()` tells E2B to kill the process after that many seconds. This is the primary enforcement mechanism.
2. **Client-side (safety net):** The entire exec operation is wrapped in `asyncio.wait_for()` with `timeout + buffer` to catch cases where E2B's server-side timeout fires but the client doesn't receive the response promptly.

```python
# Wrapper around _do_exec (simplified)
try:
    return await asyncio.wait_for(
        self._do_exec(request),
        timeout=request.timeout + 10,  # safety buffer
    )
except asyncio.TimeoutError:
    # Client-side safety net fired
    return ExecResult(
        stdout="",
        stderr=f"[kilntainers: command timed out after {request.timeout}s]",
        exit_code=124,
        exec_duration_ms=int((time.monotonic() - start_time) * 1000),
    )
```

**Exit code 124** is returned for timeout, matching the GNU `timeout` convention and other backend behavior.

### 5.4 Output Limit Enforcement

Unlike Docker/Modal where output is streamed and counted incrementally, E2B's `commands.run()` returns the complete output. Output limit is checked after the command completes:

```python
# After getting result from E2B
stdout_str = result.stdout or ""
stderr_str = result.stderr or ""

combined_size = len(stdout_str.encode("utf-8")) + len(stderr_str.encode("utf-8"))
if combined_size > request.output_limit:
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

**Design details:**

- **Post-hoc checking** — The full output is received, then checked. This means memory usage could spike for commands that produce huge output before the limit takes effect.
- **No partial output** returned when limit is exceeded — same as Docker/Modal.
- **Combined counter** tracks stdout + stderr together (Functional spec §2.4).

**Future optimization:** For commands known to produce large output, consider using E2B's streaming callbacks (`on_stdout`, `on_stderr`) to count bytes incrementally and kill the process early. This would require running in background mode.

### 5.5 Stdin Piping

Stdin data is sent via E2B's `commands.send_stdin()` method after starting a background command.

**Flow:**

1. MCP layer constructs `ExecRequest` with `stdin="file content..."`.
2. `_do_exec` starts the command in background mode.
3. `send_stdin()` writes the stdin data to the process.
4. `handle.wait()` waits for the command to complete.

```python
if request.stdin is not None:
    handle = await self._e2b_sandbox.commands.run(
        cmd,
        background=True,
        **run_kwargs,
    )
    await self._e2b_sandbox.commands.send_stdin(handle.pid, request.stdin)
    result = await handle.wait()
```

**Encoding:** `request.stdin` is a Python string, passed directly to `send_stdin()` which handles encoding.

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
            self._e2b_sandbox.kill(),
            timeout=10,
        )
    except asyncio.TimeoutError:
        pass  # Best-effort — E2B may take time to terminate
    except Exception:
        pass  # Best-effort cleanup
```

**Design details:**

- **Idempotent** — the `_stopped` flag ensures double-stop is a no-op (Phase 2 contract).
- **`_stop_requested` flag** is set before issuing kill. This tells `wait_for_death()` that the sandbox exit is expected (§5.7).
- **`sandbox.kill()`** tells E2B to terminate the sandbox. Returns `True` if killed, `False` if not found.
- **10-second timeout** covers the kill API call. If E2B's API is slow, we proceed without waiting.
- **Exception swallowing** — stop is best-effort, same as Docker/Modal.

### 5.7 Death Detection

**Design decision:** Death detection is simplified — we don't poll E2B for sandbox status. Instead, sandbox death is detected at exec time when the E2B SDK raises an error. This eliminates the complexity and overhead of polling.

```python
async def wait_for_death(self) -> None:
    """Block until cancelled.

    Death detection is simplified: we don't poll E2B for sandbox status.
    Instead, sandbox death is detected at exec time when the E2B SDK
    raises an error. This method just blocks forever until cancelled
    by the MCP layer during normal shutdown.
    """
    try:
        # Block forever until cancelled
        await asyncio.Future()
    except asyncio.CancelledError:
        return
```

**How it works:**

1. `wait_for_death()` simply blocks forever on an unresolved `Future`.
2. **Normal shutdown path:** The MCP layer calls `death_task.cancel()` before `sandbox.stop()`. The `CancelledError` propagates and the method returns cleanly.
3. **Unexpected death detection:** When a sandbox dies unexpectedly (timeout, killed externally), the next `exec()` call will raise an exception from the E2B SDK. This is caught and translated to `SandboxDiedError`.
4. **No polling overhead:** Unlike polling `is_running()` every few seconds, this approach has zero API call overhead.

**Rationale for this approach:**

- **Simplicity:** No polling loop, no state tracking for death detection.
- **Efficiency:** Zero API calls for death monitoring.
- **Adequate for MCP use case:** The MCP layer typically calls exec frequently. Death is detected quickly on the next request.
- **Matches E2B's model:** E2B sandboxes have a lifetime timeout and are managed by E2B's infrastructure. Polling adds little value.

---

## 6. Sandbox Configuration

### 6.1 E2B Sandbox.create Parameters

The full set of parameters passed to `Sandbox.create()`:

```python
sb = await Sandbox.create(
    template=self._config.template,              # template name or ID
    timeout=self._config.sandbox_timeout,        # sandbox lifetime (seconds)
    allow_internet_access=self._config.network_enabled,  # network isolation
    metadata=self._config.metadata,              # custom metadata (optional)
    envs=self._config.envs,                      # environment variables (optional)
    api_key=self._config.api_key,                # API key (optional, uses env var if not set)
)
```

### 6.2 Network Isolation

Network is **disabled by default** (`allow_internet_access=False`). This matches the Docker/Modal backend behavior and the functional spec's security model (D5, §9.1).

**Note:** E2B's default is the opposite (`allow_internet_access=True`). We override this to match our security-first default.

- **`--network` flag NOT set** (default): `allow_internet_access=False`. The sandbox cannot make outbound network connections.
- **`--network` flag set**: `allow_internet_access=True`. The sandbox has full outbound network access.

### 6.3 Resource Configuration

Unlike Docker/Modal, E2B does not allow runtime configuration of CPU/memory. Resources are defined when building the template:

```bash
# Building a template with custom resources (E2B CLI)
e2b template build --cpu-count 2 --memory-mb 1024 --name "my-high-resource-template"
```

At runtime, the template's resource allocation is fixed. Operators who need different resource levels should create multiple templates.

**CLI implications:** The `--cpu` and `--memory` flags from Docker/Modal don't apply to E2B. These should either:
1. Not be registered for the E2B backend, or
2. Be accepted but ignored with a warning that E2B templates have fixed resources.

### 6.4 Sandbox Lifetime

The `--e2b-sandbox-timeout` parameter controls how long the E2B sandbox stays alive (default: 1 hour). This is separate from the per-exec timeout.

- **Normal operation:** The sandbox is terminated when the session ends (`stop()` is called). The lifetime timeout doesn't fire.
- **Runaway sessions:** If the server crashes or the session hangs, the lifetime timeout ensures the sandbox (and its billing) doesn't run indefinitely.
- **Maximum:** E2B caps sandbox lifetime at 1 hour for Base tier, 24 hours for Pro tier. The CLI should validate this constraint.

### 6.5 Identification and Cleanup

E2B sandboxes are identified by their `sandbox_id` (exposed via `sandbox_id` property). E2B sandboxes can be listed and managed through the E2B SDK and dashboard.

**Orphan management:**

- **Normal operation:** The sandbox is terminated on session end. E2B releases all resources.
- **Server crash:** The sandbox continues running until its lifetime timeout expires, then E2B terminates it automatically. No manual cleanup needed.
- **Manual cleanup:** Orphaned sandboxes are visible via `Sandbox.list()` and in the E2B dashboard. They can be killed with `Sandbox.kill(sandbox_id)`.

This is similar to Modal — E2B's built-in lifetime timeout eliminates the orphan problem that Docker has.

---

## 7. Error Handling Summary

| E2B failure | Raised as | MCP result |
|---|---|---|
| Auth failure (validation) | `BackendError` | Server fails to start |
| API unreachable (validation) | `BackendError` | Server fails to start |
| Template not found | `BackendError` | Server fails to start (stdio) or session fails (HTTP) |
| Sandbox creation fails | `BackendError` | Same |
| Readiness check fails | `BackendError` | Same — shell not found |
| Exec — command succeeds | `ExecResult` (exit 0) | `isError: false` |
| Exec — command fails | `ExecResult` (non-zero exit) | `isError: false` |
| Exec — timeout | `ExecResult` (exit 124) | `isError: false` |
| Exec — output limit | `ExecResult` (exit 1) | `isError: false` |
| Sandbox terminated during exec | `SandboxDiedError` | `isError: true`, connection drops |
| Sandbox lifetime timeout | `SandboxDiedError` | `isError: true`, connection drops |
| Sandbox killed externally | `SandboxDiedError` | `isError: true`, connection drops |
| Sandbox died between execs | Next `exec()` raises `SandboxDiedError` | Connection drops |
| `kill()` fails | Swallowed (best-effort) | N/A — already shutting down |

**E2B-specific failure modes:**

- **Sandbox lifetime timeout:** If the sandbox hits its lifetime limit (default 1 hour), E2B terminates it. This looks like unexpected death to the MCP layer.
- **API errors during exec:** Network issues between the Kilntainers server and E2B's API could cause exec to fail. These should be caught and translated to appropriate error responses.
- **Template quota limits:** E2B may reject sandbox creation if account limits are exceeded.

---

## 8. Testing

### 8.1 Unit Tests (`tests/unit/backends/test_e2b.py`)

Unit tests mock the E2B SDK to simulate sandbox behavior without an E2B account or network access.

#### Mock Strategy

A fixture provides mock E2B objects:

```python
@pytest.fixture
def mock_e2b_sandbox(monkeypatch):
    """Mock e2b.aio.Sandbox with configurable responses."""

    class MockCommandResult:
        def __init__(self, stdout="", stderr="", exit_code=0):
            self.stdout = stdout
            self.stderr = stderr
            self.exit_code = exit_code

    class MockCommandHandle:
        def __init__(self, pid=12345, result=None):
            self.pid = pid
            self._result = result or MockCommandResult()

        async def wait(self):
            return self._result

    class MockCommands:
        def __init__(self):
            self.run_responses = []
            self.sent_stdin = []

        async def run(self, cmd, background=False, **kwargs):
            response = self.run_responses.pop(0) if self.run_responses else MockCommandResult()
            if background:
                return MockCommandHandle(result=response)
            return response

        async def send_stdin(self, pid, data):
            self.sent_stdin.append((pid, data))

    class MockSandbox:
        def __init__(self):
            self.sandbox_id = "mock-sandbox-abc123"
            self.commands = MockCommands()
            self._is_running = True
            self._killed = False

        async def is_running(self):
            return self._is_running

        async def kill(self):
            self._killed = True
            self._is_running = False
            return True

        @classmethod
        async def create(cls, **kwargs):
            return cls()

        @classmethod
        async def list(cls, **kwargs):
            return []

    return MockSandbox
```

#### Test Cases

**Validation:**
- Auth succeeds → validation passes.
- Auth fails (401) → `BackendError` with auth guidance.
- API unreachable → `BackendError` with connectivity message.
- Custom API key → passed to SDK calls.

**Sandbox creation:**
- Full sequence: create → readiness check → sandbox returned.
- Creation fails (template not found) → `BackendError`.
- Readiness check fails (shell not found) → `BackendError`, sandbox killed.

**Command construction:**
- `command` mode → shell wrapping: `shell -c 'command'`.
- `args` mode → quoted command string.
- `working_directory` present → `cwd` kwarg added.
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
- `stdin` provided → command run in background, `send_stdin` called.
- `stdin` not provided → foreground run, no stdin.

**Exec — serialization:**
- Concurrent exec calls are serialized by the lock.

**Stop:**
- `kill()` called on E2B sandbox.
- Idempotent — second stop is a no-op.
- `_stop_requested` flag is set.

**Death detection:**
- Sandbox dies unexpectedly → `wait_for_death()` returns.
- `stop()` called → `wait_for_death()` blocks.
- Task cancelled → clean exit.

**Tool instructions:**
- Default template → returns description with dynamic values.
- Custom template → returns `None`.

### 8.2 Integration Tests (`tests/integration/test_e2b_integration.py`)

Integration tests run against the real E2B API. They create actual sandboxes, execute real commands, and verify end-to-end behavior. These tests require E2B credentials (skipped in environments without E2B auth).

```python
skip_without_e2b = pytest.mark.skipif(
    not _e2b_auth_available(),
    reason="E2B credentials not configured"
)

def _e2b_auth_available() -> bool:
    """Check if E2B auth is available."""
    import os
    return os.environ.get("E2B_API_KEY") is not None
```

#### Test Cases

**Lifecycle:**
- Create sandbox → readiness check passes → sandbox_id is valid.
- Stop sandbox → sandbox killed.
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
- Default: `command="curl -s http://example.com"` → fails (no network).
- With `--network`: outbound connections succeed.

**Cost note:** Integration tests create real E2B sandboxes and incur costs. They should be tagged separately (e.g., `@pytest.mark.integration`) and run only in CI or when explicitly requested.

---

## 9. Implementation Notes

### 9.1 E2B SDK as a Main Dependency

The E2B SDK (`e2b` package) is a **main dependency** of kilntainers, not an optional one. It's included in the core dependencies in `pyproject.toml`:

```toml
dependencies = [
    "e2b>=2.13.2",
    "mcp>=1.26.0",
    "modal>=1.3.2",
]
```

**Rationale:** Unlike WASM (which requires large native binaries), the E2B SDK is a lightweight Python package. Making it a main dependency simplifies installation and avoids the complexity of lazy imports and optional dependency handling.

The backend imports directly from the e2b package:

```python
from e2b import AsyncSandbox
```

### 9.2 No Polling for Death Detection

Unlike Docker (`docker wait`) and Modal (`sb.wait()`), we don't actively monitor sandbox death. Instead:

- **Death detection:** Sandbox death is detected at exec time when the E2B SDK raises an exception.
- **Benefits:** Zero API call overhead, simpler implementation, no latency from polling intervals.
- **Trade-off:** Death is detected on the next exec call rather than proactively. For the MCP use case (frequent exec calls), this is acceptable.

### 9.3 Output Limit Memory Usage

E2B's `commands.run()` returns the complete stdout/stderr as strings. For commands that produce output near or exceeding the limit (default 2 MiB), this could cause memory spikes.

Future optimization options:
1. **Streaming callbacks:** Use `on_stdout`/`on_stderr` callbacks with background execution to count bytes incrementally.
2. **Size estimation:** For known high-output commands, proactively use streaming.

For v1, the post-hoc checking approach is simpler and acceptable for the 2 MiB default limit.

### 9.4 Template vs Image CLI Design

The `--image` pattern from Docker/Modal doesn't map directly to E2B's template system. Options:

1. **Separate parameter:** Use `--e2b-template` (distinct from `--image`). This is explicit but means different backends have different primary image/template parameters.

2. **Map `--image` to template:** Accept `--image` and treat it as a template name for E2B. This provides a consistent interface but the semantics differ (Docker images are pulled; E2B templates must be pre-built).

3. **Both parameters:** Accept both `--image` (for Docker/Modal) and `--e2b-template` (for E2B). The selected backend uses its relevant parameter.

Option 1 is cleanest for v1 — explicit and avoids confusion about the different semantics.

### 9.5 CLI Argument Registration

E2B-specific CLI arguments:

| CLI Parameter | Config Field | Default | Description |
|---|---|---|---|
| `--e2b-api-key` | `api_key` | `None` (use env) | E2B API key |
| `--e2b-template` | `template` | `"base"` | Template name or ID |
| `--e2b-sandbox-timeout` | `sandbox_timeout` | `3600` | Sandbox lifetime (seconds) |
| `--e2b-metadata` | `metadata` | `None` | Metadata key=value pairs |
| `--e2b-env` | `envs` | `None` | Environment variable key=value pairs |

Shared with other backends:
| CLI Parameter | Config Field | Default | Description |
|---|---|---|---|
| `--shell` | `shell` | `"/bin/bash"` | Shell for command mode |
| `--network` | `network_enabled` | `False` | Enable network access |
| `--timeout` | `default_timeout` | `120` | Default exec timeout |

### 9.6 E2B Account Tiers

E2B has different account tiers with different limits:

| Feature | Base Tier | Pro Tier |
|---|---|---|
| Max sandbox lifetime | 1 hour | 24 hours |
| Custom templates | Yes | Yes |
| Priority support | No | Yes |

The CLI should validate `--e2b-sandbox-timeout` against the tier limit. However, detecting the tier at runtime may require additional API calls. For v1, document the limits and let E2B return an error for invalid values.

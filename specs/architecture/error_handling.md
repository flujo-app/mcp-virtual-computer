# Architecture: Error Handling & Observability

**Phase 7** of the architecture specification. Defines the exception hierarchy, error propagation paths from backend through MCP layer to client, how each error condition is caught and transformed, startup error reporting, stderr usage patterns, and the observability strategy.

**References:** D6 (no crash recovery), D22 (response schema), D23 (sandbox death), D24 (output limit), D25 (timeout), D31 (no logging), D32 (stdin limit), Functional spec §2.3–§2.5, §3.3, §4.5. Phase 2 (backend abstraction — error types), Phase 3 (Docker backend — error handling summary), Phase 4 (MCP server — isError mapping), Phase 5 (CLI — startup validation), Phase 6 (connection lifecycle — death propagation).

---

## 1. Overview

Kilntainers has a clear error philosophy: **no logging system, great error messages.** (D31)

Every error condition — from startup configuration problems to sandbox death during execution — is communicated through a well-defined path with actionable messages. There are no log files, no structured logging, no log levels. Instead, the system invests in making every error response self-explanatory so that operators and LLM agents can diagnose and respond without external context.

Two audiences read error messages:

- **Operators** (humans) see startup errors on stderr and can inspect the agent's conversation for runtime errors.
- **LLM agents** see runtime errors as MCP tool responses (`isError: true` or `isError: false` with error information in stderr/exit_code).

The error handling architecture is organized around two phases of the server's life:

1. **Startup** — Configuration parsing, validation, backend prerequisites, tool description assembly. Errors go to stderr and cause process exit.
2. **Runtime** — Tool calls, sandbox lifecycle, connection management. Errors are returned as MCP responses or trigger connection teardown.

---

## 2. Exception Hierarchy

All custom exceptions live in `src/kilntainers/errors.py` (Phase 1). The hierarchy is intentionally flat — deep exception trees add complexity without value for a project with ~3 exception types.

```python
class KilntainersError(Exception):
    """Base exception for all Kilntainers errors.

    Catching KilntainersError catches any error originating from
    Kilntainers code (as opposed to stdlib or third-party exceptions).
    """
    pass


class BackendError(KilntainersError):
    """Raised by backend operations when something goes wrong.

    Used for:
    - Prerequisite validation failures (Docker not running)
    - Sandbox startup failures (image pull failed, readiness check failed)
    - Internal backend errors (unexpected Docker CLI failure)

    The message MUST be actionable — tell the operator what to fix.
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

Additionally, the Docker backend uses one private exception internally:

```python
class _OutputLimitExceeded(Exception):
    """Internal signal: combined output exceeded the configured limit.

    Private to the Docker backend's exec flow. Never escapes _do_exec().
    Raised by the output reader, caught by the exec handler, and
    converted into an ExecResult with exit_code=1 and a limit-exceeded
    stderr message.
    """
    pass
```

### 2.1 Exception Usage Rules

| Exception | Raised by | Caught by | Meaning |
|---|---|---|---|
| `BackendError` | Backend operations (`_validate`, `_create_sandbox`, `_run_docker`) | CLI startup, MCP lifespan | Infrastructure failure — something the operator must fix |
| `SandboxDiedError` | `Sandbox.exec()` | MCP tool handler | The sandbox is gone — connection must be dropped |
| `_OutputLimitExceeded` | Docker backend output reader | Docker backend `_do_exec` | Internal signal, never crosses module boundaries |
| `ValueError` | `ExecRequest.__post_init__` | Never caught (programming error) | Bug in the MCP layer — ExecRequest was malformed |

### 2.2 Design Principles

- **Exceptions are for infrastructure failures, not command outcomes.** A command that fails (non-zero exit) is a successful tool call. A command that times out is a successful tool call. Only conditions where the tool itself cannot function raise exceptions.
- **Exception messages are for operators.** They should be actionable and specific. "Docker daemon is not running" not "connection refused on unix socket."
- **Private exceptions don't leak.** `_OutputLimitExceeded` is an implementation detail of the Docker backend's exec flow. It is caught within `_do_exec` and converted to an `ExecResult`. No code outside the Docker backend ever sees it.

---

## 3. The Error Boundary: ExecResult vs Exception

The most important design decision in the error model is the boundary between `ExecResult` (normal tool responses) and exceptions (infrastructure failures). This boundary maps directly to the MCP protocol's `isError` field.

| Condition | Mechanism | `isError` | Why |
|---|---|---|---|
| Command succeeds (exit 0) | `ExecResult` | `false` | Normal operation |
| Command fails (non-zero exit) | `ExecResult` | `false` | The tool worked — it ran the command and reported the outcome |
| Timeout fired | `ExecResult` (exit_code 124) | `false` | The command ran and hit a limit. The tool worked. |
| Output limit exceeded | `ExecResult` (exit_code 1) | `false` | Same — a limit condition, not a tool failure |
| Invalid parameters | Validation error string | `true` | Tool was called incorrectly |
| Stdin too large | Validation error string | `true` | Tool was called incorrectly |
| Sandbox died | `SandboxDiedError` | `true` | The tool itself is broken — cannot execute |
| Backend can't start sandbox | `BackendError` | N/A | Startup failure — no MCP response (session fails) |
| Invalid ExecRequest | `ValueError` | N/A | Programming error — should never happen |

**Key insight:** The backend produces `ExecResult` for timeout and output-limit conditions. These are not exceptions — the process ran, hit a limit, and was killed cleanly. The MCP layer wraps them as `isError: false`. This keeps the tool handler simple: any `ExecResult` is a successful tool call; only exceptions indicate tool-level failures.

---

## 4. Startup Error Propagation

Startup errors occur before the server accepts any connections. They are reported to stderr and cause the process to exit with a non-zero code. No MCP response is sent — there is no client connection yet.

### 4.1 Error Flow

```
CLI arguments
  │
  ├── argparse parsing error
  │     └── argparse prints to stderr, exits with code 2
  │
  ├── Config validation error (validate_config)
  │     └── _startup_error() prints to stderr, exits with code 1
  │
  ├── Backend validation error (backend.validate())
  │     └── BackendError caught → _startup_error(), exits with code 1
  │
  ├── Tool description assembly error (assemble_tool_description)
  │     └── BackendError caught → _startup_error(), exits with code 1
  │
  └── Success → server starts accepting connections
```

### 4.2 Exit Codes

| Exit code | Meaning | Source |
|---|---|---|
| 0 | Clean shutdown | Normal process exit |
| 1 | Configuration or startup error | `_startup_error()` |
| 2 | Argument parsing error | `argparse` built-in |

### 4.3 Error Reporting Function

```python
def _startup_error(message: str) -> NoReturn:
    """Print an error message to stderr and exit with code 1."""
    print(f"kilntainers: error: {message}", file=sys.stderr)
    sys.exit(1)
```

The `kilntainers: error:` prefix identifies the source when the server is launched by another process (e.g., an MCP client that captures stderr).

### 4.4 Startup Error Examples

These are representative error messages for each startup failure mode. All are actionable — they tell the operator what went wrong and what to do about it.

**argparse errors** (exit code 2, formatted by argparse):
```
kilntainers: error: argument --port: invalid int value: 'abc'
kilntainers: error: argument --backend: invalid choice: 'modal' (choose from 'docker')
```

**Config validation errors** (exit code 1):
```
kilntainers: error: --host is only valid with --transport http. In stdio mode, there is no HTTP server to bind.
kilntainers: error: --session-timeout is only valid with --transport http. In stdio mode, the session lives as long as the process.
kilntainers: error: Cannot use both --tool-instruction-override and --extended-tool-instruction. Use override to replace the description entirely, or extended to append to the backend default.
kilntainers: error: --timeout must be at least 1 second.
```

**Backend validation errors** (exit code 1):
```
kilntainers: error: Cannot connect to docker. Is the docker daemon running?
kilntainers: error: Docker command failed (exit 1): docker info
permission denied while trying to connect to the Docker daemon socket
```

**Image pull errors** (exit code 1):
```
kilntainers: error: Failed to pull image 'nonexistent:latest'. Check that the image name is correct and the registry is reachable.
```

**Readiness check errors** (exit code 1):
```
kilntainers: error: Container abc123def456 started but shell '/bin/bash' not found. Use --shell /bin/sh for images without bash.
```

**Tool description errors** (exit code 1):
```
kilntainers: error: Backend does not provide tool instructions describing the sandbox. Supply --tool-instruction-override to describe the capabilities of this sandbox (example 'a Debian Linux bash shell' or 'A minimal BusyBox shell with the following commands: ...').
```

---

## 5. Runtime Error Propagation

Runtime errors occur after the server is accepting connections. They are returned as MCP tool responses or trigger connection/session teardown.

### 5.1 Tool Call Error Flow

```
MCP tool call arrives (tools/call "terminal_execute")
  │
  ├── Input validation (server.py: _validate_inputs)
  │     ├── Both command and args → isError: true
  │     ├── Neither command nor args → isError: true
  │     ├── working_directory not absolute → isError: true
  │     ├── timeout < 1 → isError: true
  │     ├── stdin > 2 MiB → isError: true
  │     └── Valid → continue
  │
  ├── ExecRequest construction
  │     └── ValueError → should never happen (programming error)
  │
  ├── sandbox.exec(request)
  │     ├── Command succeeds → ExecResult → isError: false
  │     ├── Command fails → ExecResult → isError: false
  │     ├── Timeout → ExecResult (exit 124) → isError: false
  │     ├── Output limit → ExecResult (exit 1) → isError: false
  │     ├── SandboxDiedError → isError: true + connection drop
  │     └── Unexpected exception → isError: true
  │
  └── Response sent to client
```

### 5.2 Input Validation Errors

Input validation runs before the backend is involved. Errors are returned as `isError: true` MCP responses with actionable messages.

```python
def _validate_inputs(
    command: str | None,
    args: list[str] | None,
    stdin: str | None,
    working_directory: str | None,
    timeout: int | None,
) -> str | None:
    """Validate tool inputs. Returns error message or None if valid."""
    ...
```

The validation function returns a string (error message) or `None` (valid). The handler wraps errors in `CallToolResult(isError=True)`.

**Error messages are written for LLM agents.** They must be:

- **Actionable** — explain what to do differently.
- **Specific** — include the actual value that was wrong.
- **Concise** — shorter messages are processed better by LLMs.

Examples:
- *"Cannot provide both 'command' and 'args'. Use 'command' for shell commands or 'args' for direct execution."*
- *"working_directory must be an absolute path, got: relative/path"*
- *"stdin content exceeds the 2 MiB limit (2,500,000 bytes). Split into smaller chunks or use a different approach."*
- *"timeout must be at least 1 second."*

### 5.3 Exec Limit Conditions (Timeout and Output Limit)

Timeout and output-limit conditions are **not errors** from the MCP perspective — they are normal tool results with specific exit codes and stderr messages. This is a critical design choice (Functional spec §2.5).

**Timeout response:**

```json
{
  "stdout": "",
  "stderr": "[kilntainers: command timed out after 120s]",
  "exit_code": 124,
  "exec_duration_ms": 120045
}
```

- `isError: false` — the tool worked, the command just took too long.
- `exit_code: 124` — matches the GNU `timeout` convention.
- No partial output — truncated output is worse than no output.
- The agent reads the stderr message and adjusts (shorter command, incremental output, etc.).

**Output limit response:**

```json
{
  "stdout": "",
  "stderr": "[kilntainers: output limit exceeded (2097152 bytes). Command terminated. No output returned. Re-run with head, tail, or grep to manage output size.]",
  "exit_code": 1,
  "exec_duration_ms": 523
}
```

- `isError: false` — same rationale as timeout.
- `exit_code: 1` — generic failure.
- The stderr message explicitly guides the agent toward the fix (use `head`, `tail`, `grep`).

**Why no partial output for either condition:** Truncated output means the agent is working with data that is silently incomplete. This creates unpredictable failures downstream. Returning a clean error is predictable and forces the agent to take a different approach. (D24)

**Interaction:** If both conditions trigger simultaneously, whichever fires first takes effect. Both produce the same pattern (kill process, no output, error message in stderr) with different messages and exit codes.

### 5.4 Sandbox Death During Exec

When the sandbox dies during command execution:

1. The `docker exec` subprocess exits abnormally.
2. The Docker backend checks if the container is still running (`docker inspect`).
3. Container is gone → `SandboxDiedError` is raised.
4. The MCP handler catches it and returns `isError: true`.
5. The death monitor task also fires, triggering connection/session teardown (Phase 6 §6).

```python
# In the tool handler (Phase 4 §3.3):
try:
    result = await sandbox.exec(request)
except SandboxDiedError as e:
    return CallToolResult(
        content=[TextContent(type="text", text=str(e))],
        isError=True,
    )
```

The error message is descriptive:
```
Sandbox abc123def456 died during command execution
```

After returning this response, the connection is dropped (stdio: process exits; HTTP: session terminated). The client can reconnect to get a fresh sandbox.

### 5.5 Sandbox Death Between Exec Calls

When the sandbox dies between tool calls (no exec in progress), the death is detected by the background monitor task (`wait_for_death()`). The propagation path depends on transport:

- **stdio:** `os.kill(os.getpid(), signal.SIGTERM)` triggers process shutdown.
- **HTTP:** The session is effectively dead. Subsequent exec calls raise `SandboxDiedError`. Proactive session termination is added if the SDK supports it.

See Phase 6 §6 for the full death propagation architecture.

### 5.6 Unexpected Exceptions

If an unexpected exception occurs during a tool call (a bug in Kilntainers code, or an unanticipated backend failure), it should be caught at the handler level and returned as `isError: true` rather than crashing the server.

```python
async def terminal_execute_handler(...) -> CallToolResult:
    # ... validation ...
    try:
        result = await sandbox.exec(request)
    except SandboxDiedError as e:
        return CallToolResult(
            content=[TextContent(type="text", text=str(e))],
            isError=True,
        )
    except Exception as e:
        # Unexpected error — return as isError: true.
        # This is a bug or unanticipated failure. The message goes to
        # stderr for the operator and to the agent as an error response.
        print(
            f"kilntainers: unexpected error during exec: {e}",
            file=sys.stderr,
        )
        return CallToolResult(
            content=[TextContent(
                type="text",
                text=f"Internal error: {e}",
            )],
            isError=True,
        )
    # ... format normal response ...
```

**Design notes:**

- The `except Exception` catch-all prevents the server from crashing on unexpected errors. A single bad tool call should not kill the server (especially in HTTP mode with multiple sessions).
- The error is reported to stderr for the operator and returned to the agent as `isError: true`.
- The message includes the exception text for debugging. This is acceptable since the exception is from internal code, not user data that might contain secrets.

---

## 6. Sandbox Creation Errors

Sandbox creation failures (`BackendError` from `backend.create_sandbox()`) occur in the lifespan context manager. Their impact depends on the transport:

### 6.1 stdio Mode

Sandbox creation happens once, before any MCP messages are processed. If it fails, the lifespan raises the exception, `mcp.run()` exits, and the process terminates with an error on stderr.

```
kilntainers: error: Failed to pull image 'bad-image:v1'. Check that the image name is correct and the registry is reachable.
```

Most MCP clients display stderr output and offer to restart the server. The operator fixes the configuration and tries again.

### 6.2 HTTP Mode

Sandbox creation happens per session (on `initialize` request). If it fails, that specific session fails to initialize. Other sessions are unaffected.

The MCP SDK handles the error — the `initialize` response contains an MCP error. The client can retry, which creates a new session and attempts sandbox creation again.

**Note:** Transient failures (Docker daemon briefly unresponsive, temporary network issue during image pull) may resolve on retry. Permanent failures (wrong image name, Docker not installed) will fail consistently.

---

## 7. Error Propagation Summary

This table maps every error condition to its complete propagation path:

| Condition | Origin | Exception/Result | MCP `isError` | Connection impact | User sees |
|---|---|---|---|---|---|
| Invalid CLI args | argparse | `SystemExit` | N/A | N/A (startup) | stderr message, exit 2 |
| Config validation fail | `validate_config()` | `SystemExit` | N/A | N/A (startup) | stderr message, exit 1 |
| Backend validation fail | `backend.validate()` | `BackendError` | N/A | N/A (startup) | stderr message, exit 1 |
| Image pull failure | `_ensure_image()` | `BackendError` | N/A | N/A (startup) or session init fails | stderr message |
| Readiness check fail | `_verify_readiness()` | `BackendError` | N/A | Session init fails | stderr message |
| Tool description empty | `assemble_tool_description()` | `BackendError` | N/A | N/A (startup) | stderr message, exit 1 |
| Invalid tool params | `_validate_inputs()` | Error string | `true` | None | Error message in response |
| Stdin too large | `_validate_inputs()` | Error string | `true` | None | Error message in response |
| Command succeeds | `sandbox.exec()` | `ExecResult` | `false` | None | JSON response |
| Command fails | `sandbox.exec()` | `ExecResult` | `false` | None | JSON response |
| Timeout | `sandbox.exec()` | `ExecResult` | `false` | None | JSON with exit 124 |
| Output limit | `sandbox.exec()` | `ExecResult` | `false` | None | JSON with exit 1 |
| Sandbox died (during exec) | `sandbox.exec()` | `SandboxDiedError` | `true` | Connection drops | Error message, then disconnect |
| Sandbox died (between execs) | `wait_for_death()` | Task completes | N/A | Connection drops | Disconnect |
| Unexpected bug | Anywhere in handler | `Exception` | `true` | None | "Internal error" message |

---

## 8. Stderr Usage Patterns

Kilntainers uses stderr as its sole output channel for non-protocol communication (D31). stdout is reserved for MCP protocol messages in stdio mode.

### 8.1 What Goes to stderr

| Content | When | Example |
|---|---|---|
| Startup errors | Argument parsing, validation, backend check failures | `kilntainers: error: Cannot connect to docker...` |
| Image pull progress | During sandbox creation when image not cached | Docker's native pull progress (layer downloads, extraction) |
| Unexpected runtime errors | Bug in Kilntainers code, unanticipated failures | `kilntainers: unexpected error during exec: ...` |
| Death monitor errors | `wait_for_death()` fails unexpectedly | `kilntainers: error monitoring sandbox: ...` |

### 8.2 What Does NOT Go to stderr

| Content | Why not | Where it goes instead |
|---|---|---|
| Normal exec results | Protocol data | MCP tool response (`isError: false`) |
| Timeout/limit notices | Protocol data | MCP tool response stderr field |
| Sandbox death notice | Protocol data | MCP tool response (`isError: true`) |
| Request logging | No logging in v1 (D31) | Nowhere (use reverse proxy for HTTP) |
| Debug information | No logging in v1 (D31) | Nowhere |

### 8.3 Message Format

All stderr messages from Kilntainers use a consistent prefix:

```
kilntainers: error: {message}
```

This prefix identifies the source, which matters when the server's stderr is mixed with other output (e.g., MCP client displaying multiple server's errors). Image pull progress from Docker does not use this prefix — it's pass-through from the Docker CLI.

### 8.4 stdio Mode: stderr Ownership

In stdio mode, the MCP protocol owns stdin and stdout. stderr is the only channel available for server-initiated messages. The MCP spec and most MCP clients expect that stderr may contain server diagnostic output and typically display it to the user (or make it available in logs).

Image pull progress is deliberately sent to stderr so the user sees feedback during the potentially slow first-run image download. Without this, the user would see no output for 30+ seconds during image pull.

---

## 9. Observability Strategy

### 9.1 V1: Great Errors, No Logs

The v1 observability strategy is defined by D31: invest in error quality, not logging infrastructure.

**For operators:**
- Startup errors on stderr explain exactly what's wrong and how to fix it.
- Runtime errors are visible in the agent's conversation (the MCP tool responses).
- HTTP deployments can use a reverse proxy (nginx, Caddy) for request logging.

**For LLM agents:**
- Validation errors explain the constraint and suggest the fix.
- Timeout messages name the timeout value and imply the agent should adjust.
- Output limit messages explicitly suggest `head`, `tail`, `grep` as alternatives.
- Sandbox death messages are clear about the terminal nature of the failure.

**For developers (debugging Kilntainers itself):**
- Unexpected exceptions are printed to stderr with full exception text.
- Unit tests cover every error path — the testing sections in each phase document verify error messages and propagation.
- The exception hierarchy is minimal and traceable.

### 9.2 Why Not Structured Logging

Common arguments for structured logging and why they don't apply to v1:

| Argument | Counter |
|---|---|
| "Need logs for debugging production issues" | The server is stateless between calls. The agent's conversation history contains the full exec request and response. Operators can reproduce issues by running the same command. |
| "Need request logging for HTTP" | Use a reverse proxy. This is already recommended for auth in production HTTP deployments (Functional spec §9). |
| "Need audit trail" | The MCP client already logs tool calls and responses. The audit trail exists on the client side. |
| "Need performance monitoring" | `exec_duration_ms` is in every response. The agent can observe performance. For server-level metrics, add in a future version. |

### 9.3 Future: `--verbose` Flag

If demand arises for more runtime visibility, a `--verbose` flag can be added without changing the architecture:

- `--verbose` would enable additional stderr output: sandbox creation details, exec timing, shutdown sequence.
- All output would still go to stderr (no log files, no structured format in v1).
- The flag would be off by default, preserving the clean stderr behavior.

This is not planned for v1 but is architecturally trivial to add — just guard `print()` calls with a config flag.

---

## 10. Error Message Quality Guidelines

These guidelines apply to all error messages in the codebase, whether returned to operators (stderr) or agents (MCP responses).

### 10.1 For Operator Messages (stderr)

1. **State the problem.** "Cannot connect to docker." not "Connection refused."
2. **Suggest the fix.** "Is the docker daemon running?" or "Use --shell /bin/sh for images without bash."
3. **Include context.** "Docker command failed (exit 1): docker info\npermission denied..." — the actual stderr from Docker is included.
4. **Use the prefix.** All messages start with `kilntainers: error:` for identification.

### 10.2 For Agent Messages (MCP responses)

1. **Be actionable.** "Cannot provide both 'command' and 'args'. Use 'command' for shell commands or 'args' for direct execution."
2. **Be specific.** "working_directory must be an absolute path, got: relative/path" — include the actual bad value.
3. **Be concise.** LLMs process shorter messages better. One or two sentences maximum.
4. **Guide the recovery.** "Re-run with head, tail, or grep to manage output size." — tell the agent what to do next.

### 10.3 For Infrastructure Messages (limit conditions)

1. **Use a clear prefix.** `[kilntainers: ...]` brackets distinguish infrastructure messages from command output.
2. **Name the constraint.** "command timed out after 120s" — include the actual timeout value.
3. **No partial data.** Don't mix infrastructure messages with partial command output. The message replaces all output.

---

## 11. Docker Backend: Error Handling Details

This section consolidates the Docker backend's error handling patterns from Phase 3, providing the complete picture in one place.

### 11.1 Docker CLI Helper Error Handling

The `_run_docker` helper centralizes Docker CLI error handling:

```python
async def _run_docker(
    self, *args: str, check: bool = True, timeout: float = 30, ...
) -> tuple[int, bytes, bytes]:
```

- **`check=True` (default):** Non-zero exit raises `BackendError` with Docker's stderr output included.
- **`check=False`:** Returns the exit code for the caller to inspect (used when failure is expected, e.g., checking if an image exists).
- **Timeout:** Docker CLI command itself stalls → kill subprocess, raise `BackendError`.

### 11.2 Exec Error Handling

The exec flow uses three distinct error paths:

```python
async def _do_exec(self, request: ExecRequest) -> ExecResult:
    try:
        stdout, stderr, returncode = await asyncio.wait_for(
            self._communicate_with_limit(proc, stdin_data, request.output_limit),
            timeout=request.timeout,
        )
        # Normal result (including command failure)
        ...
    except asyncio.TimeoutError:
        # Timeout → kill, return ExecResult with exit 124
        ...
    except* _OutputLimitExceeded:
        # Output limit → kill, return ExecResult with exit 1
        ...
```

**Exception handling for `_OutputLimitExceeded`:** Because `_OutputLimitExceeded` is raised inside an `asyncio.TaskGroup`, it gets wrapped in an `ExceptionGroup`. The `except*` syntax (Python 3.11+, available in our 3.13 target) unwraps it cleanly. See Phase 3 §9.1.

**Post-exec death check:** After a non-zero exit from `docker exec`, the backend checks if the container is still running. If not (and `_stop_requested` is `False`), it raises `SandboxDiedError` instead of returning an `ExecResult`. This prevents returning a confusing result when the container was killed externally during execution.

### 11.3 Stop Error Handling

`stop()` swallows all exceptions — it is best-effort cleanup:

```python
async def stop(self) -> None:
    if self._stopped:
        return
    self._stopped = True
    self._stop_requested = True
    try:
        # docker stop -t 5 ...
        # outer 10s timeout ...
    except Exception:
        pass  # Best-effort — don't propagate errors from stop
```

**Rationale:** When `stop()` is called, the session is already ending. Errors during cleanup should not propagate to the MCP layer, prevent other sessions from shutting down, or cause the process to crash. The container is labeled `kilntainers=true` for manual cleanup if stop fails.

### 11.4 Death Monitor Error Handling

The `_monitor_death()` function in the lifespan must not raise unexpected exceptions:

```python
async def _monitor_death() -> None:
    try:
        await sandbox.wait_for_death()
    except asyncio.CancelledError:
        raise  # Normal shutdown — propagate
    except Exception as e:
        # Monitoring failed — treat as death
        print(f"kilntainers: error monitoring sandbox: {e}", file=sys.stderr)
    # Sandbox died (or monitoring failed) — trigger shutdown
    if transport == "stdio":
        os.kill(os.getpid(), signal.SIGTERM)
```

If `wait_for_death()` itself fails (e.g., `docker wait` subprocess crashes), this is treated the same as sandbox death: the monitoring is unreliable, so assume the worst and trigger shutdown.

---

## 12. Testing

### 12.1 Unit Tests (`tests/unit/test_errors.py`)

Test the exception hierarchy and basic properties:

- `KilntainersError` is a subclass of `Exception`.
- `BackendError` is a subclass of `KilntainersError`.
- `SandboxDiedError` is a subclass of `KilntainersError`.
- `BackendError` and `SandboxDiedError` are not subclasses of each other.
- All exceptions can be constructed with a message and the message is preserved.
- `except KilntainersError` catches both `BackendError` and `SandboxDiedError`.

### 12.2 Error Propagation Tests (across modules)

These tests verify that errors raised at lower layers are correctly caught and transformed at higher layers. They use mocks to trigger specific error conditions.

**Startup error tests** (`tests/unit/test_cli.py` — Phase 5 §9):

- Backend validation raises `BackendError` → `_startup_error()` called → exit 1.
- Tool description assembly raises `BackendError` → `_startup_error()` called → exit 1.
- Config validation failure → `_startup_error()` called → exit 1.

**Tool handler error tests** (`tests/unit/test_server.py` — Phase 4 §10):

- `_validate_inputs` returns error for each invalid condition → `CallToolResult(isError=True)`.
- `sandbox.exec()` returns `ExecResult` with timeout → `CallToolResult(isError=False)`, JSON has exit_code 124.
- `sandbox.exec()` returns `ExecResult` with output limit → `CallToolResult(isError=False)`, JSON has exit_code 1.
- `sandbox.exec()` raises `SandboxDiedError` → `CallToolResult(isError=True)`.
- `sandbox.exec()` raises unexpected `RuntimeError` → `CallToolResult(isError=True)` with "Internal error" message.

**Docker backend error tests** (`tests/unit/backends/test_docker.py` — Phase 3 §8):

- `docker info` fails → `BackendError` with actionable message.
- `docker pull` fails → `BackendError`.
- `docker run` fails → `BackendError`.
- Readiness check fails → `BackendError`, container is cleaned up.
- Timeout fires during exec → `ExecResult` with exit 124.
- Output limit exceeded → `ExecResult` with exit 1.
- Container dies during exec → `SandboxDiedError`.

**Death propagation tests** (`tests/unit/test_lifecycle.py` — Phase 6 §11):

- Sandbox death in stdio → `os.kill(os.getpid(), signal.SIGTERM)`.
- Sandbox death in HTTP → subsequent `exec()` raises `SandboxDiedError`.
- Death monitor failure → treated as death event (stderr message emitted).

### 12.3 Integration Tests

**End-to-end error scenarios** (`tests/integration/`):

- Real `docker exec` with `sleep 60` and timeout 2 → ExecResult with exit 124.
- Real `docker exec` with `yes` and small output limit → ExecResult with exit 1.
- Kill container externally during exec → `SandboxDiedError`.
- Kill container externally between execs → `wait_for_death()` fires.
- Start server with non-existent image → startup failure with clear message.

### 12.4 Error Message Quality Tests

These tests verify that error messages meet the quality guidelines (§10):

- Validation errors include the actual bad value (e.g., the relative path that was rejected).
- Timeout messages include the actual timeout value.
- Output limit messages include the actual limit value.
- Backend errors include Docker CLI stderr when available.
- All stderr messages use the `kilntainers: error:` prefix (except Docker pass-through like image pull).

---

## 13. Implementation Checklist

A summary of all error handling code locations and what each is responsible for:

| File | Error handling responsibility |
|---|---|
| `errors.py` | Exception class definitions |
| `cli.py` | `_startup_error()`, catch `BackendError` during startup, `KeyboardInterrupt` handling |
| `config.py` | No error handling (frozen dataclasses, construction-time validation via argparse) |
| `server.py` | Input validation (`_validate_inputs`), `SandboxDiedError` catch, unexpected exception catch-all, tool response formatting |
| `server.py` | `_monitor_death()` exception handling, death propagation logic |
| `backends/base.py` | `ExecRequest.__post_init__` validation (`ValueError`), abstract method contracts |
| `backends/docker.py` | `_run_docker` error handling, `_do_exec` timeout/limit/death handling, `stop()` exception swallowing, `wait_for_death()` cancellation handling |

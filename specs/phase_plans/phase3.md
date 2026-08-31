# Phase 3: Docker Backend — Implementation Plan

Implement `DockerBackend` and `DockerSandbox` — the full Docker container lifecycle and command execution engine.

**Architecture reference:** [docker_backend.md](../architecture/docker_backend.md) §2–§9

---

## Implementation Steps

### Step 1: Docker CLI Helper (`_run_docker`)

**Location:** `src/kilntainers/backends/docker.py`

Add a private helper method on `DockerBackend` for running Docker CLI commands with consistent error handling.

**Spec reference:** `docker_backend.md` §3

**Key points:**
- Use `asyncio.create_subprocess_exec` to run the engine command
- Support stdin piping, timeout, and check parameter
- Raise `BackendError` with actionable messages on failure
- Used by both `DockerBackend` and `DockerSandbox` (via passed engine name)

---

### Step 2: Private Exception Class

**Location:** `src/kilntainers/backends/docker.py`

Add `_OutputLimitExceeded` exception class.

**Spec reference:** `docker_backend.md` §5.4

**Key points:**
- Private exception class for signaling output limit exceeded
- Raised by `_communicate_with_limit` when combined stdout+stderr exceeds limit
- Never escapes `_do_exec` — caught and converted to ExecResult

---

### Step 3: DockerBackend — Validation

**Location:** `src/kilntainers/backends/docker.py`

Implement `_validate()` method.

**Spec reference:** `docker_backend.md` §4.1

**Key points:**
- Run `docker info` (or configured engine) to check daemon is reachable
- Raise `BackendError` with actionable message if fails
- Validates daemon is running and user has permissions

---

### Step 4: DockerBackend — Image Management

**Location:** `src/kilntainers/backends/docker.py`

Implement `_ensure_image()` method.

**Spec reference:** `docker_backend.md` §4.2

**Key points:**
- Check if image exists locally with `docker image inspect`
- If not found, pull with stderr passthrough (for progress display)
- Raise `BackendError` if pull fails
- No pull timeout (large images can take minutes)

---

### Step 5: DockerBackend — Docker Run Command Construction

**Location:** `src/kilntainers/backends/docker.py`

Implement `_build_run_command()` method.

**Spec reference:** `docker_backend.md` §6.1

**Key points:**
- Build argument list for `docker run -d --rm --label kilntainers=true`
- Add `--network none` unless network_enabled
- Add `--cpus` and `--memory` if configured
- Append custom docker_run_flags
- Use `tail -f /dev/null` as keep-alive command

---

### Step 6: DockerSandbox — Core State and Construction

**Location:** `src/kilntainers/backends/docker.py`

Implement `DockerSandbox.__init__` and `sandbox_id` property.

**Spec reference:** `docker_backend.md` §5.1

**Key points:**
- Store engine, shell, container_id
- Add `_stopped`, `_stop_requested`, `_exec_lock` fields
- `sandbox_id` returns first 12 chars of container_id

---

### Step 7: DockerSandbox — Readiness Verification

**Location:** `src/kilntainers/backends/docker.py`

Implement `_verify_readiness()` method on `DockerSandbox`.

**Spec reference:** `docker_backend.md` §4.3.1

**Key points:**
- Run trivial exec: `echo kilntainers-ready`
- Validates container accepts exec and shell exists
- Raise `BackendError` with guidance if fails

---

### Step 8: DockerBackend — Sandbox Creation

**Location:** `src/kilntainers/backends/docker.py`

Implement `_create_sandbox()` method.

**Spec reference:** `docker_backend.md` §4.3

**Key points:**
- Call `_ensure_image()`
- Build and run `docker run` command
- Create `DockerSandbox` object
- Call `_verify_readiness()` with cleanup on failure

---

### Step 9: DockerSandbox — Command Construction

**Location:** `src/kilntainers/backends/docker.py`

Implement `_build_exec_command()` method.

**Spec reference:** `docker_backend.md` §5.2.2

**Key points:**
- Build `docker exec` argument list
- Add `-i` flag if stdin provided
- Add `-w dir` if working_directory provided
- Wrap in shell (`-c`) for command mode
- Pass args directly for args mode

---

### Step 10: DockerSandbox — Output Limit Enforcement

**Location:** `src/kilntainers/backends/docker.py`

Implement `_communicate_with_limit()` method.

**Spec reference:** `docker_backend.md` §5.4

**Key points:**
- Write stdin if provided, close the stream
- Read stdout and stderr concurrently with `asyncio.TaskGroup`
- Track combined byte count with `nonlocal total_bytes`
- Raise `_OutputLimitExceeded` when limit exceeded
- Use `except*` syntax to handle `ExceptionGroup` wrapping

---

### Step 11: DockerSandbox — Core Exec Flow

**Location:** `src/kilntainers/backends/docker.py`

Implement `_do_exec()` method.

**Spec reference:** `docker_backend.md` §5.2.1

**Key points:**
- Build command, encode stdin, start subprocess
- Use `asyncio.wait_for` for timeout enforcement
- Handle `TimeoutError` → kill subprocess, return timeout ExecResult
- Handle `ExceptionGroup` with `_OutputLimitExceeded` → kill subprocess, return limit ExecResult
- Return normal ExecResult on success
- Use `time.monotonic()` for wall-clock timing

---

### Step 12: DockerSandbox — Public Exec and Lock

**Location:** `src/kilntainers/backends/docker.py`

Implement `exec()` method with lock.

**Spec reference:** `docker_backend.md` §5.2

**Key points:**
- Check if `_stopped`, raise `SandboxDiedError` if so
- Acquire `_exec_lock` for serialization
- Delegate to `_do_exec()`

---

### Step 13: DockerSandbox — Stop

**Location:** `src/kilntainers/backends/docker.py`

Implement `stop()` method.

**Spec reference:** `docker_backend.md` §5.6

**Key points:**
- Idempotent with `_stopped` flag
- Set `_stop_requested = True`
- Run `docker stop -t 5` with 10s outer timeout
- Best-effort (swallow exceptions)

---

### Step 14: DockerSandbox — Death Detection

**Location:** `src/kilntainers/backends/docker.py`

Implement `wait_for_death()` method.

**Spec reference:** `docker_backend.md` §5.7

**Key points:**
- Run `docker wait` subprocess
- On `CancelledError`, clean up and re-raise
- If `_stop_requested` is True, block forever on unresolvable Future
- Otherwise, return to signal unexpected death

---

### Step 15: DockerBackend — Tool Instructions

**Location:** `src/kilntainers/backends/docker.py`

Implement `tool_instructions()` method.

**Spec reference:** `docker_backend.md` §4.4

**Key points:**
- Return `None` if custom image (not `debian:bookworm-slim`)
- Otherwise return description with shell name and default_timeout

---

## Tests to Implement

### Unit Tests (`src/kilntainers/backends/test_docker.py`)

**Mock strategy:** Mock `asyncio.create_subprocess_exec` to simulate Docker CLI responses.

**Test cases:**
- Validation: `docker info` success/failure
- Image management: local check, pull, pull failure
- Sandbox creation: full sequence, run failure, readiness failure
- Command construction: command mode, args mode, stdin, working_directory, combinations
- Exec — normal: success, failure, empty output
- Exec — timeout: exceeds timeout → exit 124 result
- Exec — output limit: exceeds limit → exit 1 result, combined tracking, individual streams
- Exec — stdin: data written, not written
- Exec — serialization: concurrent calls serialize
- Stop: docker stop called, idempotent, flag set
- Death detection: unexpected exit → return, stop requested → block, cancellation
- Tool instructions: default image → description, custom → None
- Docker run command: defaults, network enabled, CPU/memory set, custom flags

### Integration Tests (`src/kilntainers/backends/test_docker_integration.py`)

Marked with `@pytest.mark.docker_integration`.

**Test cases:**
- Lifecycle: create, readiness, stop, idempotent stop
- Basic exec: echo, false, ls nonexistent
- Command vs args mode: shell features work in command mode
- Working directory: -w flag works
- Stdin: piping, special characters
- Timeout: sleep with timeout → exit 124
- Output limit: yes command → exit 1
- Stateless execution: exports, cd don't persist
- Network isolation: curl fails by default, works when enabled
- Death detection: docker kill → wait_for_death resolves
- Cleanup: --rm removes container, label present

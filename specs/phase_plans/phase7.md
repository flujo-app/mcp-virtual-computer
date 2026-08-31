# Phase 7: Modal Backend — Implementation Plan

Implement the Modal backend following `specs/architecture/modal_backend.md`. This adds Modal cloud sandbox support as an alternative to Docker.

## Architecture Reference
- Full spec: `specs/architecture/modal_backend.md`
- Base classes: `src/kilntainers/backends/base.py`
- Docker backend patterns: `src/kilntainers/backends/docker.py`

## Implementation Steps

### Step 1: ModalBackendConfig Dataclass
**File:** `src/kilntainers/backends/modal.py`

Create the `ModalBackendConfig` frozen dataclass following the spec:
- Inherits from `BackendConfig`
- Fields: `token_id`, `token_secret`, `app_name`, `image`, `shell`, `network_enabled`, `cpu`, `memory`, `gpu`, `region`, `sandbox_timeout`, `default_timeout`
- All fields have sensible defaults per spec §2

### Step 2: ModalBackend Class
**File:** `src/kilntainers/backends/modal.py`

Implement `ModalBackend(Backend)` with:
- `add_cli_arguments()` — register Modal-specific CLI args
- `config_from_args()` — build config from argparse namespace
- `_configure_auth()` — set MODAL_TOKEN_ID/SECRET env vars if provided
- `_validate()` — Modal auth check, app lookup, error handling
- `_build_image()` — return `modal.Image.debian_slim()` or `modal.Image.from_registry()`
- `_create_sandbox()` — create Modal sandbox, readiness check, return ModalSandbox
- `tool_instructions()` — return description or None for custom image

Handle optional modal package import:
- Try import at module level, set to `None` on ImportError
- Check in `__init__` and raise clear error if modal is None

### Step 3: ModalSandbox Class
**File:** `src/kilntainers/backends/modal.py`

Implement `ModalSandbox(Sandbox)` with:
- State: `_modal_sandbox`, `_shell`, `_stopped`, `_stop_requested`, `_exec_lock`
- `sandbox_id` property — return `self._modal_sandbox.object_id`
- `exec()` — check liveness, acquire lock, delegate to `_do_exec`
- `_do_exec()` — build args, call `modal_sandbox.exec.aio()`, write stdin, read output with limit, wait for process, handle exceptions
- `_build_exec_args()` / `_build_exec_kwargs()` — construct Modal exec call
- `_read_output_with_limit()` — read streams with combined byte limit
- `_read_streams()` — concurrent stdout/stderr reading with TaskGroup
- `_verify_readiness()` — trivial exec to confirm shell works
- `stop()` — idempotent terminate
- `wait_for_death()` — block on `modal_sandbox.wait.aio()`

### Step 4: Private Exception
**File:** `src/kilntainers/backends/modal.py`

Add `_OutputLimitExceeded` exception (same pattern as Docker).

### Step 5: Backend Registry
**File:** `src/kilntainers/backends/__init__.py`

Import and register `ModalBackend` in `BACKEND_REGISTRY`.

### Step 6: CLI Integration
**File:** `src/kilntainers/cli.py`

The CLI already has generic backend support via the registry. No changes needed — the Modal backend's `add_cli_arguments()` will be called automatically.

### Step 7: Unit Tests
**File:** `src/kilntainers/backends/test_modal.py`

Create comprehensive unit tests with mock Modal SDK:

**Mock fixtures:**
- `MockStreamReader` — iterates lines
- `MockStreamWriter` — stdin mock with drain
- `MockContainerProcess` — returncode, stdout, stderr, wait
- `MockSandbox` — object_id, exec, terminate, wait
- `mock_modal` — monkeypatches modal module

**Test classes:**
- `TestModalBackendValidation` — auth success/failure, API unreachable, custom token
- `TestModalBackendImage` — default → debian_slim, custom → from_registry
- `TestModalBackendSandboxCreation` — full sequence, creation failure, readiness failure
- `TestModalBackendToolInstructions` — default image, custom image
- `TestModalSandboxExecCommandConstruction` — command mode, args mode, workdir, timeout
- `TestModalSandboxExec` — success, failure, timeout, output limit, stdin, serialization
- `TestModalSandboxStop` — stop called, idempotent
- `TestModalSandboxDeathDetection` — unexpected exit, stop requested, cancellation
- `TestModalSandboxSandboxId` — returns object_id

**Note:** Integration tests (real Modal API) are optional and would be marked `@pytest.mark.integration`.

### Step 8: pytest Marker
**File:** `pyproject.toml`

Add marker for modal integration tests (if implementing them):
```toml
markers = [
    "docker_integration: ...",
    "modal_integration: marks tests that require Modal credentials",
]
```

## Test Execution Order
1. Write ModalBackendConfig
2. Write basic ModalBackend skeleton with CLI methods
3. Write tests for CLI argument parsing
4. Implement _validate with mock
5. Write validation tests
6. Implement ModalSandbox methods incrementally with tests
7. Run `uv run ./checks.sh` until all pass
8. Mark Phase 7 complete in implementation_plan.md

## Key Design Decisions (from architecture spec)
- Modal SDK uses async API (`.aio()` methods)
- Dual timeout enforcement: server-side (Modal) + client-side safety net
- Output limit enforced client-side by monitoring read bytes
- Line-based reading from Modal streams (different from Docker's chunk-based)
- Custom auth via environment variables (simplest, most compatible)
- Image None → `modal.Image.debian_slim()`
- Network disabled by default (`block_network=True`)

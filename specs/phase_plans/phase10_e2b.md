# Phase 10: E2B Backend Implementation

Implements `spec/architecture/e2b_backend.md` with the following simplifications:
- No polling for death detection - sandbox death is detected on next exec request
- `e2b` dependency already added via `uv add e2b`

## Files to Create/Modify

### Create: `src/kilntainers/backends/e2b.py`
- `E2BBackendConfig` dataclass
- `E2BBackend` class
- `E2BSandbox` class

### Modify: `pyproject.toml`
- Add entry point for e2b backend
- Add `e2b` to optional dependencies (already installed, need to register)

### Modify: `src/kilntainers/backends/__init__.py`
- Add `e2b` to extra mapping

### Create: `src/kilntainers/backends/test_e2b.py`
- Unit tests with mocked E2B SDK

## Implementation Steps

1. **E2BBackendConfig dataclass** - Configuration fields:
   - `api_key: str | None = None`
   - `template: str = "base"`
   - `shell: str = "/bin/bash"`
   - `network_enabled: bool = False`
   - `sandbox_timeout: int = 3600`
   - `metadata: dict[str, str] | None = None`
   - `envs: dict[str, str] | None = None`
   - `default_timeout: int = 120` (inherited)

2. **E2BBackend class**:
   - `add_cli_arguments()` - Register E2B-specific args
   - `config_from_args()` - Build config from CLI
   - `_validate()` - Check auth via `Sandbox.list()`
   - `_create_sandbox()` - Create sandbox with readiness check
   - `tool_instructions()` - Return description for default template

3. **E2BSandbox class**:
   - `sandbox_id` property
   - `_verify_readiness()` - Quick exec to confirm shell works
   - `exec()` - With lock serialization, no polling for death
   - `_do_exec()` - Core execution with stdin handling, timeout, output limit
   - `_build_command()` - Shell wrapping for command mode
   - `_build_run_kwargs()` - Build kwargs for run()
   - `stop()` - Idempotent kill
   - `wait_for_death()` - **SIMPLIFIED**: Just blocks forever, death detected on next exec

4. **Key design decisions**:
   - Use `e2b.aio` async API
   - Stdin requires background mode + `send_stdin()`
   - Output limit enforced post-hoc (E2B returns complete output)
   - Server-side timeout via `timeout` param on `commands.run()`
   - Death detection: check `_stopped` flag at exec start, no polling

5. **Tests**:
   - Mock E2B SDK classes
   - Test validation (auth success/failure)
   - Test sandbox creation and readiness
   - Test exec command construction
   - Test timeout handling
   - Test output limit
   - Test stdin piping
   - Test stop idempotency
   - Test tool instructions

## Architecture References

- Full spec: `specs/architecture/e2b_backend.md`
- Backend ABC: `specs/architecture/backend_abstraction.md`
- Similar pattern: `src/kilntainers/backends/modal.py`

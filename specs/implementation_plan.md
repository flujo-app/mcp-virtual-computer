# Kilntainers — Implementation Plan

Phased implementation plan. Each phase builds on the previous one and produces testable, working code. Phases generally follow the [architecture specification](architecture/architecture_summary.md) order.

**Scope:** V1 targets **stdio transport only**. Streamable HTTP is deferred to a future phase. The goal is a fully working end-to-end stdio MCP server before adding HTTP complexity.

**Error handling** (architecture Phase 7) is not a separate implementation phase — it is woven into every phase. Each phase implements the error paths relevant to its components, following [error_handling.md](architecture/error_handling.md).

---

## Phase 1: Project Skeleton ✅

Set up the `src/kilntainers/` package layout, entry points, and build configuration.

**What to build:**

- [x] Create `src/kilntainers/` package directory with `__init__.py`
- [x] Create `__main__.py` (placeholder for `python -m kilntainers`)
- [x] Create empty module files: `cli.py`, `config.py`, `server.py`, `errors.py`
- [x] Create `backends/` subpackage with `__init__.py`, `base.py`, `docker.py` (empty stubs)
- [x] Update `pyproject.toml`: add `[project.scripts]` entry point, configure pytest (asyncio_mode, markers for docker_integration tests), add pytest-asyncio dev dependency
- [x] Verify `uv run ./checks.sh` passes on the skeleton

**Architecture reference:** [project_structure.md](architecture/project_structure.md) §1–§10

**Convention override:** Tests are co-located beside the files they test with a `test_` prefix (e.g., `errors.py` → `test_errors.py`, `backends/docker.py` → `backends/test_docker.py`), not in a separate `tests/` directory. Integration tests that require Docker use a `_integration` suffix (e.g., `backends/test_docker_integration.py`) and are marked with `@pytest.mark.integration`.

---

## Phase 2: Core Types & Backend Abstraction ✅

Implement the foundational types that everything else depends on: exception hierarchy, shared data types, the Backend and Sandbox ABCs, configuration dataclasses, and the backend registry.

**What to build:**

- [x] `errors.py` — `KilntainersError`, `BackendError`, `SandboxDiedError`
- [x] `backends/base.py` — `ExecResult`, `ExecRequest` (with `__post_init__` validation), `Mount` (designed, not implemented), `Sandbox` ABC, `Backend` ABC (with template method pattern for validate/create_sandbox)
- [x] `config.py` — `ServerConfig` and `DockerBackendConfig` frozen dataclasses
- [x] `backends/__init__.py` — `BACKEND_REGISTRY` dict and `get_backend_class()` (pointing to Docker backend which is stubbed for now)
- [x] Unit tests (co-located `test_*.py` files beside the modules they test): ExecRequest validation, ExecResult construction, Backend ABC behavior (using a stub subclass), Sandbox context manager, Mount type, exception hierarchy, config defaults/immutability

**Architecture references:**
- [backend_abstraction.md](architecture/backend_abstraction.md) §2–§5, §7–§10
- [error_handling.md](architecture/error_handling.md) §2
- [cli_and_startup.md](architecture/cli_and_startup.md) §2 (config dataclasses)

---

## Phase 3: Docker Backend ✅

Implement `DockerBackend` and `DockerSandbox` — the full Docker container lifecycle and command execution engine.

**What to build:**

- [x] `backends/docker.py` — `DockerBackend` class: `_validate()` (docker info check), `_ensure_image()` (check-then-pull), `_create_sandbox()` (run + readiness), `_build_run_command()`, `tool_instructions()`
- [x] `backends/docker.py` — `DockerSandbox` class: `exec()` with lock, `_do_exec()` (subprocess, timeout via `asyncio.wait_for`, output limit via `_communicate_with_limit`), `_build_exec_command()`, `stop()` (idempotent, best-effort), `wait_for_death()` (docker wait + stop-requested flag), `sandbox_id` property
- [x] `backends/docker.py` — `_OutputLimitExceeded` private exception, `_run_docker` CLI helper
- [x] Unit tests (`backends/test_docker.py`): mock `asyncio.create_subprocess_exec` to test command construction, timeout handling, output limit enforcement, stdin piping, stop idempotency, death detection, tool instructions (default vs custom image), docker run command assembly
- [x] Integration tests (`backends/test_docker_integration.py`, marked `@pytest.mark.integration`): basic exec, command vs args mode, working directory, stdin piping, timeout with real sleep, output limit with real output, stateless execution, network isolation, death detection (docker kill), container cleanup after stop, label verification

**Architecture reference:** [docker_backend.md](architecture/docker_backend.md) §2–§9

---

## Phase 4: MCP Server & Tool Layer ✅

Implement the MCP server using the official `mcp` SDK with FastMCP. Register the `terminal_execute` tool, handle requests, and format responses. stdio transport only.

**What to build:**

- [x] Add `mcp` SDK dependency to `pyproject.toml` (`mcp>=1.0,<2.0`)
- [x] `server.py` — `SessionContext` dataclass, `create_lifespan()` factory (stdio death propagation via SIGTERM self-signal), `create_server()` factory function, `assemble_tool_description()`
- [x] `server.py` — `terminal_execute_handler()` with input validation (`_validate_inputs`), ExecRequest construction (default resolution), sandbox.exec() call, ExecResult → JSON response formatting, SandboxDiedError catch, unexpected exception catch-all
- [x] `server.py` — `_create_handler()` to bind server config via closure
- [x] Unit tests (`test_server.py`, using MockBackend/MockSandbox): tool description assembly (all rules from functional spec §6), input validation (all error cases), handler normal responses, handler error responses (SandboxDiedError, unexpected exceptions), ExecRequest construction (default resolution, all parameter combos), server factory returns configured FastMCP instance

**Architecture references:**
- [mcp_server.md](architecture/mcp_server.md) §1–§9
- [connection_lifecycle.md](architecture/connection_lifecycle.md) §8 (lifespan factory with transport-aware death propagation)
- [error_handling.md](architecture/error_handling.md) §5 (runtime error propagation)

---

## Phase 5: CLI & Startup ✅

Implement argument parsing, startup validation, and the main entry point. Wire everything together so `kilntainers` runs as a stdio MCP server.

**What to build:**

- [x] `cli.py` — `build_parser()` with argument groups (core options, tool description, docker backend options). Include HTTP args in parser for `--help` completeness, but they will error in validation since only stdio is supported in this milestone.
- [x] `cli.py` — `build_configs()` mapping argparse namespace to `ServerConfig` + `DockerBackendConfig`
- [x] `cli.py` — `validate_config()` for cross-cutting constraints (HTTP-only args in stdio mode, mutual exclusivity of tool description params, value ranges)
- [x] `cli.py` — `main()` (sync entry point), `_async_main()` (backend creation, validation, server creation, `mcp.run(transport="stdio")`)
- [x] `cli.py` — `_startup_error()` for stderr error reporting with `kilntainers: error:` prefix
- [x] `__main__.py` — wire to `cli.main()` (already done in Phase 1)
- [x] Update `pyproject.toml` — `[project.scripts] kilntainers = "kilntainers.cli:main"` (already done in Phase 1)
- [x] Unit tests (`test_cli.py`): parser defaults, custom args, config construction, validation (HTTP-only args in stdio, mutual exclusivity, value ranges), startup flow with mock backend

**Architecture reference:** [cli_and_startup.md](architecture/cli_and_startup.md) §1–§10

---

## Phase 6: End-to-End stdio & Integration Testing ✅

Verify the full pipeline works end-to-end: CLI → startup → FastMCP → tool call → Docker backend → response. Add integration tests and polish.

**What to build:**

- [x] Verify end-to-end: install the package, run `kilntainers` as an MCP server, connect a client, execute commands, receive responses
- [x] Stdio lifecycle integration tests: full session (start → exec → stop), sandbox creation failure handling, graceful shutdown (stdin EOF, SIGTERM)
- [x] Death propagation integration test: kill container externally, verify server exits
- [x] Polish: verify `--help` output, test with a real MCP client (e.g. Claude Desktop or similar), fix any rough edges

**Architecture references:**
- [connection_lifecycle.md](architecture/connection_lifecycle.md) §2 (stdio lifecycle), §6 (death propagation), §7 (graceful shutdown)
- [error_handling.md](architecture/error_handling.md) §4 (startup errors), §7 (full propagation summary)

---

## Phase 7: Modal Backend ✅

Implement `spec/architecture/modal_backend.md`

- [x] Create `src/kilntainers/backends/modal.py` — `ModalBackendConfig`, `ModalBackend`, `ModalSandbox`, `_OutputLimitExceeded`
- [x] Add `modal` package dependency (already in pyproject.toml)
- [x] Update `src/kilntainers/backends/__init__.py` — register `ModalBackend` in `BACKEND_REGISTRY`
- [x] Unit tests (`src/kilntainers/backends/test_modal.py`): mock Modal SDK, test all backend and sandbox methods

**Architecture reference:** [modal_backend.md](architecture/modal_backend.md)


---

## Phase 8: Streamable HTTP Transport ✅

Add Streamable HTTP support with per-session sandbox management. Deferred until stdio is fully working and tested.

**What to build:**

- [x] HTTP session lifecycle: per-session sandbox creation via lifespan, session timeout (`--session-timeout`) configuration (FastMCP SDK handles internal session management)
- [x] Death propagation for HTTP: request-time detection (baseline), proactive session termination if SDK supports it
- [x] Validate HTTP-specific CLI args (`--host`, `--port`, `--session-timeout`) only error in stdio mode, work correctly in HTTP mode
- [x] Transport selection: `--transport http` maps to `mcp.run(transport="streamable-http")`
- [x] Server shutdown: SIGTERM tears down all active sessions concurrently (via lifespan cleanup)
- [x] Unit tests: HTTP lifespan, server creation, death propagation (no SIGTERM in HTTP mode)
- [x] concurrent session e2e test, live http server test, session-scoped sandbox isolation 
- [ ] Full integration tests: idle timeout (SDK manages sessions), server SIGTERM with active sessions (deferred - requires complex MCP protocol test setup)

**Architecture references:**
- [connection_lifecycle.md](architecture/connection_lifecycle.md) §3–§5, §7.3, §9
- [mcp_server.md](architecture/mcp_server.md) §5.2 (Streamable HTTP wiring)
- [cli_and_startup.md](architecture/cli_and_startup.md) §4.2 (HTTP-only arg detection)

---

## Phase 9: WASM backend

 - [x] Implement the plan described in `specs/architecture/wasm_backend.md`

---

## Phase 10: E2B Backend ✅

Implement `spec/architecture/e2b_backend.md`

- [x] Create `src/kilntainers/backends/e2b.py` — `E2BBackendConfig`, `E2BBackend`, `E2BSandbox`
- [x] Add `e2b` package dependency (main dependency, not optional)
- [x] Update `pyproject.toml` — register entry point for e2b backend
- [x] Unit tests (`src/kilntainers/backends/test_e2b.py`): mock E2B SDK, test all backend and sandbox methods

**Architecture reference:** [e2b_backend.md](architecture/e2b_backend.md)

**Key design decisions:**
- E2B SDK is a main dependency (not optional like WASM)
- No polling for death detection — sandbox death detected on next exec call
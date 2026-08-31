# Architecture Summary

This document is the entry point for the Kilntainers architecture specification. It lists each sub-document with a brief description of what it covers. For functional behavior (what the system does, not how it's built), see the [functional spec](../functional_spec.md).

---

## Architecture Documents

### [Phase 1: Project Structure & Packaging](project_structure.md)

Python package layout (`src/kilntainers/`), module responsibilities, entry points (`kilntainers` CLI and `python -m kilntainers`), dependency management with `uv` and `pyproject.toml`, Python 3.13 minimum, dev tooling (ruff, pyright, pytest), `checks.sh` for local development, and GitHub Actions CI configuration.

### [Phase 2: Backend Abstraction Layer](backend_abstraction.md)

The `Backend` and `Sandbox` ABCs that define the central interface between the MCP server and any backend implementation. Covers `ExecResult` and `ExecRequest` dataclasses, the template method pattern for validation and sandbox creation, async method contracts, the `wait_for_death()` death detection mechanism, concurrency model (multi-sandbox support, serial exec within a sandbox), the `Mount` type (designed for future use), the error types (`KilntainersError`, `BackendError`, `SandboxDiedError`), and the CLI argument classmethods (`add_cli_arguments()`, `config_from_args()`) that allow each backend to own its argument definitions and config construction.

### [Phase 3: Docker Backend Implementation](docker_backend.md)

How the Docker backend implements the abstraction layer: subprocess calls to the Docker CLI via a shared helper, container lifecycle (`docker run` with `--rm` and `kilntainers=true` label), image pull mechanics, readiness verification, command execution with streaming output monitoring, timeout enforcement (`asyncio.wait_for`), output limit enforcement (combined stdout+stderr byte counter with `asyncio.TaskGroup`), stdin piping, container death detection (`docker wait`), stop with idempotency, network isolation, resource limits, custom Docker flags (`--docker-run-flag`), and tool description generation.

### [Phase 4: MCP Server & Tool Layer](mcp_server.md)

MCP library evaluation (official `mcp` SDK v1.x with built-in FastMCP), server architecture with lifespan context for per-session sandbox management, `terminal_execute` tool registration with dynamic description, the tool handler implementation (input validation, `ExecRequest` construction, `ExecResult` → JSON response formatting, `isError` mapping), tool description assembly rules, transport configuration (stdio and Streamable HTTP), and the `create_server()` factory function.

### [Phase 5: CLI, Configuration & Startup](cli_and_startup.md)

Argument parsing with `argparse` (no third-party CLI libraries), the `ServerConfig`, `BackendConfig`, and `DockerBackendConfig` frozen dataclasses, backend-owned argument registration and config construction via classmethods, startup validation (HTTP-only args in stdio mode, mutual exclusivity, value ranges), the backend registry, the full startup flow from `main()` through `mcp.run()`, exit codes (1 for config errors, 2 for argparse errors), `--help` output organization, and stderr-only output conventions.

### [Phase 6: Connection & Session Lifecycle](connection_lifecycle.md)

How stdio and Streamable HTTP transports map to sandbox lifecycles: stdio runs one sandbox for the process lifetime, HTTP runs one per `Mcp-Session-Id` session. Covers session creation and request routing, idle session timeout (`--session-timeout`) and its SDK integration, the one-sandbox-per-session ownership model, sandbox death propagation (SIGTERM self-signal for stdio, request-time detection for HTTP), graceful shutdown orchestration (cancel death task → stop sandbox), force-kill timeouts, and edge cases (concurrent death and exec, rapid reconnection, SIGTERM during creation).

### [Modal Backend Implementation](modal_backend.md)

How the Modal backend implements the abstraction layer using the Modal Python SDK: token-based authentication, `modal.Image` construction (debian_slim default, `from_registry` for custom images), sandbox creation with resource configuration (CPU, memory, GPU, region), readiness verification, command execution with server-side timeout enforcement and client-side output limit monitoring via `asyncio.TaskGroup`, stdin piping through Modal's `StreamWriter`, sandbox death detection (`sb.wait.aio()`), stop via `terminate()`, network isolation (`block_network`), sandbox lifetime management, and the optional dependency pattern for the `modal` package.

### [WASM Backend Implementation](wasm_backend.md)

How the WASM backend implements the abstraction layer using the wasmtime Python package for in-process WebAssembly execution: entry-point-based backend discovery (replacing the hard-coded registry), `WasmBackend` and `WasmSandbox` classes, the `GoBusyBoxBackend` subclass with a bundled `busybox.wasm` binary, file-based I/O capture, epoch-based timeout interruption, post-execution output limit enforcement, temp-directory-as-filesystem model, WASI configuration, resource limits (memory, fuel), optional dependency packaging (`kilntainers[wasm]`), and the `--shell` integration for command mode support.

### [Phase 7: Error Handling & Observability](error_handling.md)

The complete error handling architecture: exception hierarchy (`KilntainersError` → `BackendError` / `SandboxDiedError`), the ExecResult-vs-exception boundary (limit conditions are results, not errors), startup error propagation (stderr + exit codes), runtime error propagation (input validation → `isError: true`, exec results → `isError: false`, sandbox death → `isError: true` + connection drop), unexpected exception catch-all, stderr usage patterns, the "great errors, no logs" observability strategy (D31), error message quality guidelines for both operators and LLM agents, and Docker backend error handling details.

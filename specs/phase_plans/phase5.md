# Phase 5: CLI & Startup — Implementation Plan

**Objective:** Wire everything together so `kilntainers` runs as a stdio MCP server.

## Overview

Phase 5 implements the argument parsing, startup validation, and main entry point that connects all the components built in previous phases (config, backend, server). The CLI uses argparse (stdlib) with flat arguments organized into groups. HTTP args are included for `--help` completeness but will error during validation in stdio mode (only stdio transport is supported in this milestone).

**Architecture reference:** [cli_and_startup.md](../architecture/cli_and_startup.md) §1–§10

## Implementation Steps

### Step 1: Update `ServerConfig` Port Default

**File:** `src/kilntainers/config.py`

The current default is 8435 but the architecture spec specifies 8435.

- Change `port: int = 8435` to `port: int = 8435`

### Step 2: Implement `build_parser()`

**File:** `src/kilntainers/cli.py`

Create the argument parser with grouped arguments:

```python
def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="kilntainers",
        description="MCP server providing isolated Linux sandboxes for LLM agent shell execution.",
    )

    # Core options group
    core = parser.add_argument_group("core options")
    core.add_argument("--backend", default="docker", choices=["docker"], help="...")
    # ... all core args: transport, host, port, timeout, output-limit, session-timeout

    # Tool description group
    desc = parser.add_argument_group("tool description")
    desc.add_argument("--tool-instruction-override", ...)
    desc.add_argument("--extended-tool-instruction", ...)

    # Docker backend options group
    docker = parser.add_argument_group("docker backend options")
    docker.add_argument("--engine", ...)
    # ... all docker args: image, shell, network, cpu, memory, docker-run-flag

    return parser
```

**Key details from cli_and_startup.md §3.1:**
- `--backend` choices limited to `["docker"]` for now
- `--transport` choices are `["stdio", "http"]` but only stdio works in this milestone
- `--docker-run-flag` uses `action="append"` for repeatable flag
- HTTP-only args (`--host`, `--port`, `--session-timeout`) are included for help completeness

### Step 3: Implement `build_configs()`

**File:** `src/kilntainers/cli.py`

Map argparse namespace to config dataclasses:

```python
def build_configs(args: argparse.Namespace) -> tuple[ServerConfig, DockerBackendConfig]:
    """Build config dataclasses from parsed arguments."""
    server_config = ServerConfig(
        transport=args.transport,
        host=args.host,
        port=args.port,
        default_timeout=args.timeout,
        output_limit=args.output_limit,
        tool_instruction_override=args.tool_instruction_override,
        extended_tool_instruction=args.extended_tool_instruction,
        session_timeout=args.session_timeout,
    )

    docker_config = DockerBackendConfig(
        engine=args.engine,
        image=args.image,
        shell=args.shell,
        network_enabled=args.network,
        cpu=args.cpu,
        memory=args.memory,
        docker_run_flags=args.docker_run_flags or [],
        default_timeout=args.timeout,
    )

    return server_config, docker_config
```

**Key details from cli_and_startup.md §3.2:**
- Coalesce `docker_run_flags` `None` to empty list
- `default_timeout` goes to both configs from the same source

### Step 4: Implement `validate_config()`

**File:** `src/kilntainers/cli.py`

Validate cross-cutting constraints:

```python
def validate_config(server_config: ServerConfig, docker_config: DockerBackendConfig) -> None:
    """Validate configuration constraints that span multiple parameters."""
    # HTTP-only parameters in stdio mode
    if server_config.transport == "stdio":
        if server_config.host != "127.0.0.1":
            _startup_error("--host is only valid with --transport http...")
        if server_config.port != 8435:
            _startup_error("--port is only valid with --transport http...")
        if server_config.session_timeout != 300:
            _startup_error("--session-timeout is only valid with --transport http...")

    # Mutual exclusivity: tool description params
    if (server_config.tool_instruction_override is not None
            and server_config.extended_tool_instruction is not None):
        _startup_error("Cannot use both --tool-instruction-override and --extended-tool-instruction...")

    # Timeout must be positive
    if server_config.default_timeout < 1:
        _startup_error("--timeout must be at least 1 second.")

    # Output limit must be positive
    if server_config.output_limit < 1:
        _startup_error("--output-limit must be at least 1 byte.")
```

**Key details from cli_and_startup.md §4.1:**
- Use sentinel approach for detecting explicitly-set HTTP-only args
- Compare against defaults to detect explicit setting
- Call `_startup_error()` which exits with code 1

### Step 5: Implement `_startup_error()`

**File:** `src/kilntainers/cli.py`

```python
def _startup_error(message: str) -> NoReturn:
    """Print an error message to stderr and exit with code 1."""
    print(f"kilntainers: error: {message}", file=sys.stderr)
    sys.exit(1)
```

**Key details from cli_and_startup.md §4.3 and error_handling.md §4.3:**
- Use `kilntainers: error:` prefix
- Exit with code 1 (argparse uses code 2 for its errors)

### Step 6: Implement `_async_main()`

**File:** `src/kilntainers/cli.py`

Async startup flow:

```python
async def _async_main(server_config: ServerConfig, docker_config: DockerBackendConfig) -> None:
    """Async startup: validate backend, build server, run."""
    from kilntainers.backends import get_backend_class
    from kilntainers.server import create_server

    # Create and validate backend
    backend_class = get_backend_class("docker")  # Only docker for now
    backend = backend_class(docker_config)
    try:
        await backend.validate()
    except BackendError as e:
        _startup_error(str(e))

    # Create the MCP server
    try:
        mcp = create_server(backend, server_config)
    except BackendError as e:
        _startup_error(str(e))

    # Run the transport (blocks until shutdown)
    transport = "stdio" if server_config.transport == "stdio" else "streamable-http"
    mcp.run(transport=transport)
```

**Key details from cli_and_startup.md §6.1:**
- Backend validation happens before server creation
- Tool description assembly happens inside `create_server()`
- Transport string mapping: `"http"` → `"streamable-http"`

### Step 7: Implement `main()`

**File:** `src/kilntainers/cli.py`

Synchronous entry point:

```python
def main() -> None:
    """CLI entry point. Parses args, configures, and runs the server."""
    parser = build_parser()
    args = parser.parse_args()

    server_config, docker_config = build_configs(args)
    validate_config(server_config, docker_config)

    try:
        asyncio.run(_async_main(server_config, docker_config))
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C
```

**Key details from cli_and_startup.md §6.2:**
- Sync entry point required for `console_scripts`
- `asyncio.run()` creates the event loop
- KeyboardInterrupt handling for clean Ctrl+C exit

### Step 8: Verify `__main__.py` and `pyproject.toml`

**Files:** `src/kilntainers/__main__.py`, `pyproject.toml`

These should already be correct from Phase 1:

- `__main__.py` should wire to `cli.main()`
- `pyproject.toml` `[project.scripts]` should have `kilntainers = "kilntainers.cli:main"`

Verify and confirm no changes needed.

## Testing

### Unit Tests: `src/kilntainers/test_cli.py`

Create comprehensive unit tests for CLI behavior:

**Test fixtures:**
- Mock `os.kill` for death propagation tests (won't be hit in phase 5 but prepare for it)
- Mock `asyncio.run` to avoid actually running the server
- Mock backend `validate()` method

**Test groups:**

1. **Parser defaults** (`test_parser_defaults`):
   - No arguments → all defaults match spec

2. **Parser custom args** (`test_parser_custom_args`):
   - Core args: `--transport http`, `--host 0.0.0.0`, `--port 9090`, etc.
   - Tool description args: `--tool-instruction-override`, `--extended-tool-instruction`
   - Docker args: `--engine podman`, `--image alpine:latest`, etc.
   - Repeatable flag: `--docker-run-flag` multiple times

3. **Config construction** (`test_build_configs`):
   - Default args → expected default values
   - Custom args → values propagated correctly
   - `--timeout` appears in both configs
   - `--docker-run-flag` not provided → empty list

4. **Validation** (`test_validate_config`):
   - HTTP-only args in stdio mode → error
   - Both tool description params → error
   - Value ranges: timeout < 1, output_limit < 1

5. **Startup flow** (`test_async_main`):
   - Backend validation fails → `_startup_error`
   - Tool description fails → error
   - Success → server created with correct args, transport mapping correct

**Note:** These tests should NOT require Docker. All backend/server interactions are mocked.

## Acceptance Criteria

1. ✅ `build_parser()` produces correct defaults and accepts all valid arguments
2. ✅ `build_configs()` correctly maps args to config dataclasses
3. ✅ `validate_config()` catches all cross-cutting constraint violations
4. ✅ `main()` runs through complete startup flow with mocked backend
5. ✅ All unit tests pass (`test_cli.py`)
6. ✅ `uv run ./checks.sh` passes all checks (format, lint, typecheck, tests)
7. ✅ `--help` output is well-organized by argument groups
8. ✅ Entry point works: `uv run kilntainers --help` displays help

## Notes

- **Sentinel for HTTP-only arg detection:** Consider using `_UNSET = object()` sentinel for HTTP-only args to precisely detect when they were explicitly set vs. having their default value. See cli_and_startup.md §4.2.
- **Port default fix:** Change from 8435 to 8435 to match architecture spec.
- **Backend registry:** Use existing `get_backend_class()` from `backends/__init__.py`.
- **Only stdio works:** HTTP args parse but validation rejects them in stdio mode. Full HTTP support is Phase 7.

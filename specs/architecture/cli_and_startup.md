# Architecture: CLI, Configuration & Startup

**Phase 5** of the architecture specification. Defines argument parsing, configuration dataclasses, startup validation, backend-specific arg routing, and the full startup flow from process launch to "ready to accept connections."

**References:** D12 (flat CLI args), D16 (tool description assembly), D25 (timeout), D24 (output limit), D31 (no logging — stderr only), Functional spec §3, §4.3, §6. Phase 1 (project structure), Phase 2 (backend abstraction), Phase 3 (Docker backend), Phase 4 (MCP server).

---

## 1. Argument Parsing Library

### Decision: `argparse` (stdlib)

Kilntainers uses Python's built-in `argparse` module for CLI argument parsing. No third-party CLI libraries (click, typer, etc.).

**Why argparse:**

- **Zero dependencies.** The project philosophy is minimal dependencies — `argparse` is stdlib, always available, no version pinning or supply-chain concerns.
- **Sufficient expressiveness.** The CLI schema is flat with ~15 arguments, mostly strings and integers with simple defaults. No nested subcommands (v1), no complex types, no multi-level help. `argparse` handles this comfortably.
- **Familiar.** Every Python developer knows `argparse`. No learning curve for contributors.
- **Repeatable args.** `action="append"` supports `--docker-run-flag` (the only repeatable argument).

**Why NOT click/typer:**

- They add a dependency for something `argparse` handles well at this scale.
- Click's decorator-based API is harder to test (functions are wrapped). `argparse` produces a namespace that's trivially inspectable.
- Typer depends on click and adds another layer. Overkill for a flat argument schema.

If the CLI grows significantly in the future (subcommands for `cleanup`, `status`, etc.), click or typer could be reconsidered. For v1, argparse is the right fit.

---

## 2. Configuration Dataclasses

Parsed CLI arguments are converted into typed, immutable configuration dataclasses before being used anywhere in the system. These live in `src/kilntainers/config.py`.

### 2.1 ServerConfig

Core server configuration, independent of which backend is running.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ServerConfig:
    """Core server configuration from CLI arguments.

    Consumed by the MCP server layer (Phase 4) and the startup
    orchestration logic. Does not contain backend-specific config.
    """
    # Transport
    transport: str = "stdio"          # "stdio" or "http"
    host: str = "127.0.0.1"          # HTTP bind address
    port: int = 8435                  # HTTP listen port

    # Exec defaults
    default_timeout: int = 120        # seconds
    output_limit: int = 2_097_152     # bytes (2 MiB)

    # Tool description
    tool_instruction_override: str | None = None
    extended_tool_instruction: str | None = None

    # Session management (HTTP only)
    session_timeout: int = 300        # seconds (5 minutes)
```

**Notes:**

- `transport` is stored as a string (`"stdio"` or `"http"`). Validation rejects other values during argument parsing.
- `host` and `port` are always populated (have defaults). The startup logic validates that they're only meaningful in HTTP mode.
- `output_limit` is in bytes, matching the functional spec and the `ExecRequest.output_limit` field.
- `default_timeout` is the server-wide default. Per-call `timeout` overrides happen in the tool handler (Phase 4 §6.2).

### 2.2 BackendConfig Base Class

All backend config dataclasses inherit from `BackendConfig`, which contains fields shared across all backends:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class BackendConfig:
    """Base class for all backend configurations.

    Contains fields shared across all backends. Backend-specific
    config classes inherit from this.
    """
    # Passed through for tool description generation
    default_timeout: int = 120
```

### 2.3 DockerBackendConfig

Docker-specific configuration. Inherits from `BackendConfig`. This was previewed in Phase 3 §2 — here is the authoritative definition.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class DockerBackendConfig(BackendConfig):
    """Configuration for the Docker backend.

    Populated from CLI args by DockerBackend.config_from_args().
    Consumed by DockerBackend (Phase 3).
    """
    engine: str = "docker"
    image: str = "debian:bookworm-slim"
    shell: str = "/bin/bash"
    network_enabled: bool = False
    cpu: str | None = None
    memory: str | None = None
    docker_run_flags: list[str] = field(default_factory=list)
```

**Why `default_timeout` is in both configs:** `ServerConfig.default_timeout` is used by the MCP handler to resolve per-call timeout defaults. `BackendConfig.default_timeout` (inherited by `DockerBackendConfig`) is used by `DockerBackend.tool_instructions()` to embed the actual timeout value in the tool description text. Both are set from the same `--timeout` CLI arg.

### 2.4 Why Frozen Dataclasses

Configuration is immutable after construction:

- **No accidental mutation.** Config is passed to multiple components (backend, server, tool handler). Immutability guarantees they all see the same values.
- **Thread safety.** HTTP mode has concurrent sessions — frozen config is safe to share without locking.
- **Clear data flow.** Config is constructed once during startup and flows downward. No "config changed at runtime" bugs.

---

## 3. Argument Definitions

### 3.1 Parser Construction

The argument parser is constructed in `cli.py` with core/shared arguments defined directly, and backend-specific arguments delegated to each backend class. The parser iterates the `BACKEND_REGISTRY` and calls each backend's `add_cli_arguments()` classmethod to register its own argument group.

```python
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kilntainers",
        description=(
            "MCP server providing isolated Linux sandboxes "
            "for LLM agent shell execution."
        ),
    )

    # --- Core parameters (shared across all backends) ---
    core = parser.add_argument_group("core options")
    core.add_argument(
        "--backend",
        default="docker",
        choices=list(BACKEND_REGISTRY.keys()),
        help="Backend to use (default: docker)",
    )
    core.add_argument(
        "--transport",
        default="stdio",
        choices=["stdio", "http"],
        help="MCP transport (default: stdio)",
    )
    core.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP bind address (default: 127.0.0.1, HTTP mode only)",
    )
    core.add_argument(
        "--port",
        type=int,
        default=8435,
        help="HTTP listen port (default: 8435, HTTP mode only)",
    )
    core.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Default exec timeout in seconds (default: 120)",
    )
    core.add_argument(
        "--output-limit",
        type=int,
        default=2_097_152,
        help="Max combined stdout+stderr bytes per exec (default: 2097152 = 2 MiB)",
    )
    core.add_argument(
        "--session-timeout",
        type=int,
        default=300,
        help="Idle session timeout in seconds (default: 300, HTTP mode only)",
    )

    # --- Tool description ---
    desc = parser.add_argument_group("tool description")
    desc.add_argument(
        "--tool-instruction-override",
        default=None,
        help="Replace the entire terminal_execute tool description",
    )
    desc.add_argument(
        "--extended-tool-instruction",
        default=None,
        help="Append to the backend's default tool description",
    )

    # --- Backend-specific parameters (delegated to each backend) ---
    for name, backend_cls in BACKEND_REGISTRY.items():
        group = parser.add_argument_group(f"{name} backend options")
        backend_cls.add_cli_arguments(group)

    return parser
```

Each backend class owns its argument definitions via the `add_cli_arguments()` classmethod (see §3.3). For example, `DockerBackend.add_cli_arguments()` registers `--engine`, `--image`, `--shell`, `--network`, `--cpu`, `--memory`, and `--docker-run-flag`.

### 3.2 Argument-to-Config Mapping

After parsing, arguments are split into the appropriate config dataclasses. `ServerConfig` is constructed directly from shared args. The backend config is constructed by the selected backend's `config_from_args()` classmethod:

```python
def build_configs(
    args: argparse.Namespace,
) -> tuple[ServerConfig, BackendConfig]:
    """Build config dataclasses from parsed arguments.

    This function maps flat CLI arguments to the typed config objects
    consumed by the server and backend layers. Server config is built
    here; backend config is delegated to the backend class.
    """
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

    backend_cls = get_backend_class(args.backend)
    backend_config = backend_cls.config_from_args(args)

    return server_config, backend_config
```

**Notes:**

- Each backend's `config_from_args()` knows how to read its own args from the namespace and construct its own config type.
- Both configs get `default_timeout` from the same `args.timeout` source (backends access `args.timeout` in their `config_from_args()`).
- This function is pure (no side effects) and easy to test.

### 3.3 Backend-Owned CLI Arguments

Each backend class provides two classmethods that own its CLI argument lifecycle:

1. **`add_cli_arguments(group)`** — Registers backend-specific arguments on an `argparse._ArgumentGroup`. Called during parser construction (§3.1) for every backend in the registry, so `--help` shows all available options.

2. **`config_from_args(args)`** — Reads parsed arguments from the `argparse.Namespace` and constructs the backend's own config dataclass. Called during config construction (§3.2) for the selected backend only.

This design means `cli.py` has zero backend-specific knowledge — no backend-specific `add_argument()` calls, no backend-specific config construction. Adding a new backend is entirely self-contained: implement the class, add it to the registry, and it works.

**Argument namespace sharing:** All backends' args live in the same `argparse.Namespace`. Backend-specific args for non-selected backends are ignored (they still have defaults in the namespace — they're simply not read). Backend authors should use distinctive arg names to avoid collisions (e.g., `--docker-run-flag`, not `--extra-flag`).

See the Backend ABC definition in `backend_abstraction.md` §4 for the abstract method signatures.

---

## 4. Startup Validation

Validation runs after argument parsing, before creating the backend or server. It catches configuration errors early with clear messages.

### 4.1 Validation Function

```python
def validate_config(
    server_config: ServerConfig,
    backend_config: BackendConfig,
) -> None:
    """Validate configuration constraints that span multiple parameters.

    Raises SystemExit with a descriptive message on failure.
    Individual argument type validation is handled by argparse.
    Cross-cutting constraints are checked here.
    """
    # HTTP-only parameters in stdio mode
    if server_config.transport == "stdio":
        # Check if user explicitly passed HTTP-only args
        # (We check against defaults — if the value differs from
        # the default, the user must have set it explicitly)
        if server_config.host != "127.0.0.1":
            _startup_error(
                "--host is only valid with --transport http. "
                "In stdio mode, there is no HTTP server to bind."
            )
        if server_config.port != 8435:
            _startup_error(
                "--port is only valid with --transport http. "
                "In stdio mode, there is no HTTP server to bind."
            )
        if server_config.session_timeout != 300:
            _startup_error(
                "--session-timeout is only valid with --transport http. "
                "In stdio mode, the session lives as long as the process."
            )

    # Mutual exclusivity: tool description params
    if (server_config.tool_instruction_override is not None
            and server_config.extended_tool_instruction is not None):
        _startup_error(
            "Cannot use both --tool-instruction-override and "
            "--extended-tool-instruction. Use override to replace "
            "the description entirely, or extended to append to "
            "the backend default."
        )

    # Timeout must be positive
    if server_config.default_timeout < 1:
        _startup_error("--timeout must be at least 1 second.")

    # Output limit must be positive
    if server_config.output_limit < 1:
        _startup_error("--output-limit must be at least 1 byte.")
```

### 4.2 HTTP-Only Arg Detection

Detecting whether the user explicitly passed an HTTP-only argument (vs. it having its default value) is a common CLI design challenge. The approach above compares against known defaults — if `--host` is `127.0.0.1`, we assume it wasn't explicitly set.

**Tradeoff:** This means `kilntainers --transport stdio --host 127.0.0.1` silently succeeds, even though the user explicitly passed `--host`. This is acceptable — the value is the default anyway, so no harm is done. The alternative (tracking which args were explicitly passed) adds complexity for an edge case with no practical impact.

An alternative implementation uses argparse's ability to track which args were explicitly provided — by using a custom default sentinel:

```python
_UNSET = object()

core.add_argument("--host", default=_UNSET, ...)
core.add_argument("--port", type=int, default=_UNSET, ...)
core.add_argument("--session-timeout", type=int, default=_UNSET, ...)
```

Then in validation, check `args.host is _UNSET` to distinguish "not provided" from "explicitly set to the default." The config constructor fills in actual defaults after validation passes. This approach is more precise and is the recommended implementation.

### 4.3 Error Reporting

```python
def _startup_error(message: str) -> NoReturn:
    """Print an error message to stderr and exit with code 1.

    Used for all startup/configuration errors. Follows D31 (no logging —
    stderr for error reporting).
    """
    print(f"kilntainers: error: {message}", file=sys.stderr)
    sys.exit(1)
```

**Message format:** `kilntainers: error: {message}`. Matches the conventional pattern for CLI error reporting. The `kilntainers:` prefix identifies the source when the server is launched by another process (e.g., an MCP client that captures stderr).

---

## 5. Backend Registry

The backend registry maps `--backend` string values to backend classes. It lives in `src/kilntainers/backends/__init__.py`.

```python
from kilntainers.backends.docker import DockerBackend

# Maps --backend CLI values to (backend_class, config_class) pairs
BACKEND_REGISTRY: dict[str, type] = {
    "docker": DockerBackend,
}


def get_backend_class(name: str) -> type:
    """Look up a backend class by name.

    Raises KeyError if the backend name is not registered.
    """
    if name not in BACKEND_REGISTRY:
        available = ", ".join(sorted(BACKEND_REGISTRY.keys()))
        raise KeyError(
            f"Unknown backend: '{name}'. Available backends: {available}"
        )
    return BACKEND_REGISTRY[name]
```

**Registration is static.** New backends are added by importing the class and adding an entry to the dict. No dynamic discovery, no entry points, no plugin system. For a project with ~2–3 backends total, explicit registration is simpler and more debuggable.

**The `choices` parameter on `--backend`** in `argparse` is kept in sync with `BACKEND_REGISTRY.keys()`. This provides argument-level validation before the registry is consulted. The registry lookup is defense-in-depth.

---

## 6. Full Startup Flow

The complete startup sequence from process launch to "ready to accept connections." This is the `main()` function in `src/kilntainers/cli.py`.

```
┌─────────────────────────────────────────────────┐
│  1. Parse CLI arguments (argparse)              │
│     └── Invalid args → argparse prints error,   │
│         exits with code 2                        │
├─────────────────────────────────────────────────┤
│  2. Build config objects (ServerConfig,          │
│     BackendConfig via backend classmethod)       │
├─────────────────────────────────────────────────┤
│  3. Validate cross-cutting constraints           │
│     └── HTTP-only args in stdio mode            │
│     └── Mutual exclusivity checks               │
│     └── Value range checks                       │
│     └── Failure → stderr message, exit 1        │
├─────────────────────────────────────────────────┤
│  4. Create backend with config (no validation)   │
│     └── backend_cls(backend_config)             │
├─────────────────────────────────────────────────┤
│  5. Assemble tool description                    │
│     └── backend.tool_instructions() + overrides │
│     └── Failure → stderr message, exit 1        │
├─────────────────────────────────────────────────┤
│  6. Create FastMCP server                        │
│     └── create_server(backend, server_config)   │
├─────────────────────────────────────────────────┤
│  7. Run transport (blocking)                     │
│     └── mcp.run(transport="stdio" | "http")     │
│     └── Blocks until shutdown signal            │
└─────────────────────────────────────────────────┘
```

**No eager backend validation.** The previous step 5 ("Validate backend prerequisites") has been removed. Backend validation (e.g., `docker info`) is deferred to the first `terminal_execute` call, as part of lazy sandbox creation. This allows the server to start and respond to `tools/list` immediately. See `connection_lifecycle.md` §8 for the lazy creation design.

### 6.1 Implementation

```python
def main() -> None:
    """CLI entry point. Parses args, configures, and runs the server."""
    parser = build_parser()
    args = parser.parse_args()

    server_config, backend_config = build_configs(args)
    validate_config(server_config, backend_config)

    # Create backend from registry using selected --backend
    # No validation here — validation is deferred to first terminal_execute
    backend_cls = get_backend_class(args.backend)
    backend = backend_cls(backend_config)

    try:
        mcp = create_server(backend, server_config)
    except BackendError as e:
        _startup_error(str(e))

    transport = (
        "stdio" if server_config.transport == "stdio"
        else "streamable-http"
    )
    try:
        mcp.run(transport=transport)
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C
```

The startup flow is fully backend-agnostic — no backend class is imported or referenced directly in `cli.py`. The `get_backend_class()` lookup and the backend's own `config_from_args()` classmethod handle all backend-specific concerns.

### 6.2 Sync/Async Boundary

The `main()` function is synchronous (required for `console_scripts` entry points). It calls `asyncio.run()` to enter the async world. This is the only place in the codebase where `asyncio.run()` is called — the event loop is created once and runs until the server shuts down.

**KeyboardInterrupt handling:** `asyncio.run()` raises `KeyboardInterrupt` on Ctrl+C. The `except KeyboardInterrupt: pass` suppresses the traceback for a clean exit. Cleanup (sandbox stop) is handled by the lifespan context manager's `finally` block (Phase 4 §2.2) and anyio's shutdown sequence.

### 6.3 Transport String Mapping

The CLI uses `--transport http` (user-friendly) which maps to `"streamable-http"` (the FastMCP transport identifier). This translation happens once in `_async_main`.

| CLI value | FastMCP transport string |
|---|---|
| `stdio` | `"stdio"` |
| `http` | `"streamable-http"` |

### 6.4 Error Handling During Startup

Every step that can fail has a clear error path:

| Step | Failure mechanism | User sees |
|---|---|---|
| Arg parsing | argparse built-in | `error: argument --port: invalid int value` |
| Config validation | `_startup_error()` | `kilntainers: error: --host is only valid with...` |
| Tool description | `BackendError` caught | `kilntainers: error: Backend does not provide...` |
| Transport run | Unhandled (crashes) | Stack trace — indicates a bug |

Steps 1–5 are the "startup gauntlet" — all error-prone operations complete before the server starts accepting connections. Once `mcp.run()` is called, the server is in a known-good state for accepting MCP protocol messages.

**Backend validation errors are deferred.** Errors like "Docker daemon is not running" are not caught at startup. They surface on the first `terminal_execute` call as `isError: true` MCP responses. This tradeoff is intentional — it allows the server to start immediately and respond to `tools/list`, while errors that require a running backend are reported when the backend is actually needed.

---

## 7. Module Structure

### 7.1 `cli.py`

```python
"""CLI entry point and startup orchestration.

This module has zero backend-specific knowledge. All backend
argument definitions and config construction are delegated to
the backend classes via classmethods.
"""

# Public
def main() -> None: ...              # Entry point (sync)
def build_parser() -> ArgumentParser: ...  # Core args + delegates to backends
def build_configs(args) -> tuple[ServerConfig, BackendConfig]: ...
def validate_config(server_config, backend_config) -> None: ...

# Private
def _startup_error(message: str) -> NoReturn: ...
```

**Note:** `_validate_backend()` was removed — backend validation is deferred to the first `terminal_execute` call (lazy sandbox creation).

### 7.2 `config.py`

```python
"""Configuration dataclasses."""

@dataclass(frozen=True, slots=True, kw_only=True)
class BackendConfig:
    """Base class for all backend configurations.
    
    Contains fields shared across all backends. Backend-specific
    config classes inherit from this.
    """
    default_timeout: int = 120

@dataclass(frozen=True, slots=True, kw_only=True)
class ServerConfig: ...

@dataclass(frozen=True, slots=True, kw_only=True)
class DockerBackendConfig(BackendConfig): ...
```

### 7.3 `backends/__init__.py`

```python
"""Backend registry."""

BACKEND_REGISTRY: dict[str, type[Backend]] = { "docker": DockerBackend }
def get_backend_class(name: str) -> type[Backend]: ...
```

### 7.4 `__main__.py`

```python
"""Support for python -m kilntainers."""
from kilntainers.cli import main
main()
```

This was established in Phase 1 §5 — no changes needed.

---

## 8. `--help` Output

The `--help` output is organized by argument groups for readability. The functional spec (§3.2) notes that the help page should be organized by backend.

**Expected output:**

```
usage: kilntainers [-h] [--backend {docker}] [--transport {stdio,http}]
                   [--host HOST] [--port PORT] [--timeout TIMEOUT]
                   [--output-limit OUTPUT_LIMIT]
                   [--session-timeout SESSION_TIMEOUT]
                   [--tool-instruction-override TOOL_INSTRUCTION_OVERRIDE]
                   [--extended-tool-instruction EXTENDED_TOOL_INSTRUCTION]
                   [--engine ENGINE] [--image IMAGE] [--shell SHELL]
                   [--network] [--cpu CPU] [--memory MEMORY]
                   [--docker-run-flag DOCKER_RUN_FLAGS]

MCP server providing isolated Linux sandboxes for LLM agent shell execution.

options:
  -h, --help            show this help message and exit

core options:
  --backend {docker}    Backend to use (default: docker)
  --transport {stdio,http}
                        MCP transport (default: stdio)
  --host HOST           HTTP bind address (default: 127.0.0.1, HTTP mode only)
  --port PORT           HTTP listen port (default: 8435, HTTP mode only)
  --timeout TIMEOUT     Default exec timeout in seconds (default: 120)
  --output-limit OUTPUT_LIMIT
                        Max combined stdout+stderr bytes per exec
                        (default: 2097152 = 2 MiB)
  --session-timeout SESSION_TIMEOUT
                        Idle session timeout in seconds
                        (default: 300, HTTP mode only)

tool description:
  --tool-instruction-override TOOL_INSTRUCTION_OVERRIDE
                        Replace the entire terminal_execute tool description
  --extended-tool-instruction EXTENDED_TOOL_INSTRUCTION
                        Append to the backend's default tool description

docker backend options:
  --engine ENGINE       Container CLI binary (default: docker).
                        Supports podman.
  --image IMAGE         Docker image (default: debian:bookworm-slim)
  --shell SHELL         Shell binary for command mode (default: /bin/bash)
  --network             Enable network access in sandboxes
                        (default: disabled)
  --cpu CPU             Docker CPU limit (e.g., "1.5")
  --memory MEMORY       Docker memory limit (e.g., "512m")
  --docker-run-flag DOCKER_RUN_FLAGS
                        Additional flag passed to docker run. Repeatable.
```

The grouping makes it easy to identify which parameters are core vs. backend-specific, matching the functional spec's organization.

---

## 9. Testing

### 9.1 Unit Tests (`tests/unit/test_cli.py`)

CLI tests validate argument parsing, config construction, and startup validation without running the server or Docker.

#### Parser Tests

Test that `build_parser()` produces correct defaults and accepts valid arguments:

- **Defaults:** No arguments → all defaults match functional spec (§3.1, §3.2).
- **Core args:** `--transport http`, `--host 0.0.0.0`, `--port 9090`, `--timeout 300`, `--output-limit 1048576`, `--session-timeout 600`.
- **Tool description args:** `--tool-instruction-override "custom"`, `--extended-tool-instruction "extra"`.
- **Docker args:** `--engine podman`, `--image alpine:latest`, `--shell /bin/sh`, `--network`, `--cpu 1.5`, `--memory 512m`.
- **Repeatable flag:** `--docker-run-flag "--pids-limit=256" --docker-run-flag "--read-only"` → list of two strings.
- **Invalid choices:** `--backend unknown` → argparse error. `--transport websocket` → argparse error.
- **Invalid types:** `--port abc` → argparse error. `--timeout -5` parsed (caught by validation).

#### Config Construction Tests

Test that `build_configs()` correctly maps parsed args to config dataclasses:

- **Default args** → `ServerConfig` and `DockerBackendConfig` with expected default values.
- **Custom args** → values propagated to correct config fields.
- **`--timeout`** → appears in both `ServerConfig.default_timeout` and `DockerBackendConfig.default_timeout`.
- **`--docker-run-flag` not provided** → `docker_config.docker_run_flags` is empty list (not None).
- **`--network` flag** → `docker_config.network_enabled` is True.
- **Backend delegation** → `build_configs()` calls the selected backend's `config_from_args()` classmethod.

#### Validation Tests

Test that `validate_config()` catches constraint violations:

- **HTTP-only args in stdio mode:**
  - `--transport stdio --host 0.0.0.0` → error mentioning `--host` and HTTP.
  - `--transport stdio --port 9090` → error.
  - `--transport stdio --session-timeout 600` → error.
  - `--transport http --host 0.0.0.0` → no error (valid).

- **Mutual exclusivity:**
  - Both `--tool-instruction-override` and `--extended-tool-instruction` → error.
  - Only override → no error.
  - Only extended → no error.
  - Neither → no error.

- **Value ranges:**
  - `--timeout 0` → error.
  - `--timeout -1` → error.
  - `--timeout 1` → no error (minimum valid).
  - `--output-limit 0` → error.
  - `--output-limit 1` → no error.

#### Startup Flow Tests

Test the `_async_main` function with a mock backend:

- **Backend validation fails** → `_startup_error` called with backend's error message.
- **Tool description assembly fails** (no backend instructions, no override) → error.
- **Successful startup** → `create_server` called with correct args, `mcp.run` called with correct transport.
- **Transport mapping** → `"stdio"` → `"stdio"`, `"http"` → `"streamable-http"`.

### 9.2 Unit Tests (`tests/unit/test_config.py`)

Test the config dataclasses themselves:

- **Frozen:** Assignment to fields raises `FrozenInstanceError`.
- **Defaults:** Default construction produces expected values.
- **kw_only:** Positional construction raises `TypeError`.
- **DockerBackendConfig.docker_run_flags default:** Empty list (via `field(default_factory=list)`).

### 9.3 Testing Strategy

CLI tests do not need Docker, a running server, or any I/O. They test pure argument parsing and validation logic:

```python
def test_default_config():
    parser = build_parser()
    args = parser.parse_args([])
    server_config, docker_config = build_configs(args)
    assert server_config.transport == "stdio"
    assert server_config.default_timeout == 120
    assert docker_config.image == "debian:bookworm-slim"


def test_http_only_args_rejected_in_stdio():
    server_config = ServerConfig(
        transport="stdio",
        host="0.0.0.0",  # non-default = explicitly set
    )
    docker_config = DockerBackendConfig()
    with pytest.raises(SystemExit):
        validate_config(server_config, docker_config)
```

For startup flow tests, the backend is mocked (using `MockBackend` from Phase 2 §11) and `mcp.run` is patched to avoid actually starting a server.

---

## 10. Implementation Notes

### 10.1 argparse Exit Codes

`argparse` uses exit code **2** for argument parsing errors (invalid type, unknown argument, missing required arg). The startup validation uses exit code **1** for configuration constraint violations. This distinction is intentional — it matches UNIX convention:

- **1** = general error (our validation failures).
- **2** = usage error (argparse's built-in behavior for malformed commands).

### 10.2 Stderr-Only Output

Following D31 (no logging system), all startup messages go to stderr:

- **argparse errors** go to stderr by default.
- **`_startup_error()`** prints to stderr explicitly.
- **Image pull progress** goes to stderr (Phase 3 §4.2).
- **stdout is reserved for MCP protocol messages** in stdio mode.

In HTTP mode, stdout is not used for protocol messages (the server listens on a socket), but maintaining the stderr convention keeps behavior consistent across transports.

### 10.3 Signal Handling

The default Python signal handling is sufficient for v1:

- **SIGTERM/SIGINT (Ctrl+C):** `asyncio.run()` handles cancellation. The FastMCP server's shutdown logic runs, which triggers the lifespan cleanup (Phase 4 §2.2), which stops sandboxes.
- **SIGKILL:** Unhandleable. Sandbox may be orphaned. Docker `--rm` + labels provide recovery (Phase 3 §6.6).

No custom signal handlers are needed. The layered cleanup (asyncio → FastMCP lifespan → sandbox stop) handles graceful shutdown naturally.

### 10.4 Future: Subcommands

If `kilntainers cleanup` or other subcommands are added, the parser structure evolves to use `argparse` subparsers:

```python
subparsers = parser.add_subparsers(dest="command")
serve_parser = subparsers.add_parser("serve")  # current behavior
cleanup_parser = subparsers.add_parser("cleanup")
```

The current `main()` behavior (run the server) would become the default when no subcommand is given, or the `serve` subcommand. This is a straightforward argparse evolution that doesn't require restructuring the existing code.

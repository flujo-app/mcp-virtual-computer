# Task: Backend-Owned CLI Arguments

## Goal

Refactor CLI argument handling so each backend owns its own argument definitions and config construction, instead of having all backend-specific code centralized in `cli.py`. This makes `cli.py` backend-agnostic and makes adding new backends entirely self-contained.

## Motivation

Currently, `cli.py` hardcodes:
- Docker-specific `add_argument()` calls (lines 89–131)
- Docker-specific config construction in `build_configs()` (lines 166–174)
- Hardcoded `choices=["docker"]` on `--backend`

Every new backend would add more args and more parsing logic to this single file, making it a sprawling catch-all. The backend registry (`BACKEND_REGISTRY`) already exists but isn't leveraged for argument handling.

## Design

The architecture specs have been updated with the full design:
- `specs/architecture/backend_abstraction.md` §4 — CLI Argument Classmethods on the Backend ABC
- `specs/architecture/cli_and_startup.md` §3 — Parser Construction and Backend-Owned CLI Arguments

### Summary

Two new abstract classmethods are added to the `Backend` ABC:

1. **`add_cli_arguments(cls, group)`** — Each backend registers its own arguments on an `argparse._ArgumentGroup`.
2. **`config_from_args(cls, args)`** — Each backend constructs its own config dataclass from parsed `argparse.Namespace`.

A new `BackendConfig` base class is added to `config.py` that all backend configs inherit from.

## Files to Change

### 1. `src/kilntainers/config.py`

- Add a `BackendConfig` base dataclass with `default_timeout: int = 120`.
- Make `DockerBackendConfig` inherit from `BackendConfig`.
- Remove `default_timeout` from `DockerBackendConfig` (it's now inherited).

### 2. `src/kilntainers/backends/base.py`

Add two abstract classmethods to `Backend`:

```python
import argparse
from kilntainers.config import BackendConfig

class Backend(ABC):
    @classmethod
    @abstractmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        """Register backend-specific CLI arguments on the given argparse group."""
        ...

    @classmethod
    @abstractmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        """Build backend config from parsed CLI arguments."""
        ...

    # ... existing methods unchanged ...
```

### 3. `src/kilntainers/backends/docker.py`

Implement both classmethods on `DockerBackend`:

```python
@classmethod
def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
    group.add_argument("--engine", default="docker",
        help="Container CLI binary (default: docker). Supports podman.")
    group.add_argument("--image", default="debian:bookworm-slim",
        help="Docker image (default: debian:bookworm-slim)")
    group.add_argument("--shell", default="/bin/bash",
        help="Shell binary for command mode (default: /bin/bash)")
    group.add_argument("--network", action="store_true", default=False,
        help="Enable network access in sandboxes (default: disabled)")
    group.add_argument("--cpu", default=None,
        help='Docker CPU limit (e.g., "1.5")')
    group.add_argument("--memory", default=None,
        help='Docker memory limit (e.g., "512m")')
    group.add_argument("--docker-run-flag", action="append", default=None,
        dest="docker_run_flags",
        help='Additional flag passed to docker run. Repeatable. '
             '(e.g., --docker-run-flag "--pids-limit=256")')

@classmethod
def config_from_args(cls, args: argparse.Namespace) -> DockerBackendConfig:
    return DockerBackendConfig(
        engine=args.engine,
        image=args.image,
        shell=args.shell,
        network_enabled=args.network,
        cpu=args.cpu,
        memory=args.memory,
        docker_run_flags=args.docker_run_flags or [],
        default_timeout=args.timeout,
    )
```

### 4. `src/kilntainers/backends/__init__.py`

- Update `BACKEND_REGISTRY` type hint from `dict[str, type]` to `dict[str, type[Backend]]`.
- Update `get_backend_class` return type from `type` to `type[Backend]`.

### 5. `src/kilntainers/cli.py`

**Remove:**
- The entire "Docker backend parameters" argument group (lines 89–131).
- The `DockerBackendConfig` construction inside `build_configs()`.
- Any direct import of `DockerBackendConfig`.

**Change:**
- `build_parser()`: Replace hardcoded `choices=["docker"]` with `choices=list(BACKEND_REGISTRY.keys())`. Add a loop that iterates `BACKEND_REGISTRY` and calls `backend_cls.add_cli_arguments(group)` for each.
- `build_configs()`: Change return type from `tuple[ServerConfig, DockerBackendConfig]` to `tuple[ServerConfig, BackendConfig]`. Delegate backend config construction to `backend_cls.config_from_args(args)`.
- `validate_config()`: Change `docker_config` parameter to `backend_config: BackendConfig`.
- `main()`: Use `get_backend_class(args.backend)` to get the backend class, then `backend_cls(backend_config)` to create the instance.
- Import `BACKEND_REGISTRY` and `get_backend_class` from backends, import `BackendConfig` from config.

### 6. Tests

Update existing tests in `tests/unit/test_cli.py` (and any related test files):
- Tests that construct `DockerBackendConfig` directly are fine — the class still exists.
- Tests that call `build_configs()` should verify it returns `BackendConfig` (specifically `DockerBackendConfig` for `--backend docker`).
- Tests that call `build_parser()` should verify the Docker backend args are still present (they're now registered via classmethod, but the result is the same).
- Add tests for the new classmethods:
  - `DockerBackend.add_cli_arguments()` registers the expected arguments.
  - `DockerBackend.config_from_args()` produces a `DockerBackendConfig` with correct values.

## Verification

After implementation:
1. `kilntainers --help` should produce the same output as before (core options + docker backend options, same grouping).
2. All existing CLI tests pass unchanged (behavior is identical, only internal structure changed).
3. `build_parser()` and `build_configs()` in `cli.py` contain zero docker-specific code.
4. Type checking passes (`pyright`).
5. Linting passes (`ruff`).

## Non-Goals

- Do not add any new backends in this task.
- Do not change any CLI behavior or argument names.
- Do not change the `--help` output.
- Do not modify the MCP server layer or any runtime behavior.

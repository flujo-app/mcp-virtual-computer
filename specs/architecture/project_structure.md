# Architecture: Project Structure & Packaging

**Phase 1** of the architecture specification. Establishes the Python package layout, directory structure, entry points, dependency management, and conventions that everything else is built on.

**References:** D1 (Python), D12 (flat CLI args), D17 (MCP library — to be evaluated in Phase 4).

---

## 1. Repository Layout

```
kilntainers/                        # repo root (rename from container_mcp)
├── pyproject.toml                  # project config (PEP 621), managed by uv
├── uv.lock                         # lockfile, managed by uv
├── README.md
├── CLAUDE.md
├── checks.sh                       # local dev checks: formatting, linting, type checking, tests
├── .github/
│   └── workflows/
│       └── ci.yml                  # GitHub Actions — same checks as checks.sh
├── specs/                          # specifications (existing)
│   ├── architecture/               # architecture docs (this series)
│   ├── decisions.md
│   ├── functional_spec.md
│   ├── planning_qa.md
│   ├── project_overview.md
│   └── spec_queue.md
├── src/
│   └── kilntainers/                # the Python package
│       ├── __init__.py             # version, top-level exports
│       ├── __main__.py             # `python -m kilntainers` entry point
│       ├── cli.py                  # argument parsing and startup orchestration
│       ├── config.py               # configuration dataclasses
│       ├── server.py               # MCP server setup, tool registration, request handling
│       ├── errors.py               # exception hierarchy
│       └── backends/
│           ├── __init__.py         # backend registry / lookup
│           ├── base.py             # ABC definitions: Backend, Sandbox
│           └── docker.py           # Docker backend implementation
└── tests/
    ├── conftest.py                 # shared fixtures
    ├── unit/
    │   ├── conftest.py
    │   ├── test_cli.py
    │   ├── test_config.py
    │   ├── test_server.py
    │   └── backends/
    │       ├── test_base.py        # ABC contract tests
    │       └── test_docker.py      # Docker backend with mocked subprocess
    └── integration/
        ├── conftest.py
        └── test_docker_integration.py   # real Docker required
```

### Why `src/` layout

The `src/` layout (as opposed to a flat `kilntainers/` package at the repo root) is the modern Python recommendation:

- **Prevents accidental imports** — you can't accidentally `import kilntainers` from the source tree without installing the package. This catches packaging bugs early (e.g., missing files in the distribution).
- **Clean separation** — source code, tests, specs, and config files occupy distinct top-level directories.
- **Standard** — recommended by the Python Packaging Authority (PyPA) and supported by all modern build backends.

### Module responsibilities (brief)

Detailed designs for each module come in later architecture phases. This is the mapping from concern to file:

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing, startup validation orchestration, entry point logic. (Phase 5) |
| `config.py` | Typed configuration objects populated from CLI args. Passed to server and backend. (Phase 5) |
| `server.py` | MCP server construction, `terminal_execute` tool registration, request validation, response formatting, tool description assembly. (Phase 4) |
| `errors.py` | Exception classes used across the codebase. (Phase 7) |
| `backends/base.py` | `Backend` ABC, `Sandbox` ABC, shared types (`ExecResult` dataclass). (Phase 2) |
| `backends/docker.py` | `DockerBackend` and `DockerSandbox` implementations. (Phase 3) |
| `backends/__init__.py` | Backend registry — maps `--backend` string values to backend classes. |

---

## 2. Package Identity

| Attribute | Value |
|---|---|
| **PyPI package name** | `kilntainers` |
| **Importable name** | `kilntainers` |
| **CLI command** | `kilntainers` (via `console_scripts` entry point) |
| **Repo name** | `kilntainers` (renamed from `container_mcp`) |

The CLI command is the primary interface. `python -m kilntainers` is supported as an alternative via `__main__.py`.

---

## 3. Python Version

**Minimum: Python 3.13**

Rationale:
- This is a new project with no legacy compatibility requirements. Targeting the current stable release keeps the codebase simple and lets us use the latest language features without compatibility shims.
- 3.13 gives us improved `asyncio` (task groups, better error messages), better error messages across the board, and the latest typing features.
- Avoids accumulating conditional logic for older Python versions that would need to be cleaned up later anyway.

---

## 4. Build System & Dependencies

### uv

**`uv`** is the project manager and package tool. The project is initialized with `uv init` which creates and manages `pyproject.toml`. uv handles dependency resolution, lockfile management (`uv.lock`), virtual environments, and running commands (`uv run`).

No `setup.py`, `setup.cfg`, or `requirements.txt` at the top level. `pyproject.toml` is the single project config (PEP 621), and `uv.lock` pins exact versions for reproducible installs.

### Core dependencies

| Dependency | Purpose | Notes |
|---|---|---|
| MCP library (TBD) | MCP protocol implementation | `mcp` or `fastmcp` — evaluated in Phase 4 |

The dependency list is intentionally minimal. The project is a thin orchestrator (per project_overview.md) — no Docker SDK (D10), no HTTP framework beyond what the MCP library provides. `asyncio` and `subprocess` are stdlib.

### Dev dependencies

| Dependency | Purpose |
|---|---|
| `pytest` | Test framework |
| `pytest-asyncio` | Async test support (backend and server are async) |
| `ruff` | Linting and formatting (single tool, fast, replaces flake8 + black + isort) |
| `pyright` | Static type checking |

### pyproject.toml sketch

The actual file is created by `uv init` and managed by `uv add`. This sketch shows the target state after setup:

```toml
[project]
name = "kilntainers"
version = "0.1.0"
description = "MCP server providing isolated Linux sandboxes for LLM agents"
requires-python = ">=3.13"
dependencies = [
    # MCP library TBD (Phase 4)
]

[project.scripts]
kilntainers = "kilntainers.cli:main"

[tool.pytest.ini_options]
testpaths = ["tests"]
asyncio_mode = "auto"

[tool.ruff]
target-version = "py313"
src = ["src"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "RUF"]

[tool.pyright]
pythonVersion = "3.13"
venvPath = "."
venv = ".venv"
```

---

## 5. Entry Points

### CLI entry point

`kilntainers` command invokes `kilntainers.cli:main`. This is the primary way users run the server. The `main()` function:

1. Parses CLI arguments.
2. Constructs the configuration.
3. Validates and starts the server.

Detailed design in Phase 5 (CLI & startup).

### `__main__.py`

Enables `python -m kilntainers` as an alternative to the `kilntainers` command. Contents are minimal:

```python
from kilntainers.cli import main

main()
```

This is useful when the package is installed in an environment where console_scripts aren't on PATH, or during development.

---

## 6. Installation Methods

The distribution story matters more for Python than for Go (acknowledged in D1). Supported installation methods, in priority order:

1. **`uv tool install kilntainers`** — recommended execution via `uv`. Becoming the common pattern for MCP servers.
`uv add kilntainers` / **`pip install kilntainers`** / **`pipx install kilntainers`** — traditional distribution via PyPI. `pipx` is recommended for CLI tools since it creates an isolated environment. `uv add kilntainers` is recommended for adding to a project.
3. **Development install** — `uv sync` from the repo root for contributors (installs all deps including dev into a local venv).

Docker-image-of-the-server-itself is a potential future distribution method but not in v1 scope.

---

## 7. Conventions

### Naming

- **Modules:** `snake_case` (standard Python).
- **Classes:** `PascalCase`. Backend implementations are `{Name}Backend` and `{Name}Sandbox` (e.g., `DockerBackend`, `DockerSandbox`).
- **Constants:** `UPPER_SNAKE_CASE`.
- **CLI args:** `--kebab-case` (standard CLI convention). Mapped to `snake_case` config attributes internally.

### Async

The codebase is async-first. Backend operations (start, stop, exec) are `async` methods because they involve subprocess calls and I/O waits. The MCP library's request handling is also async.

Sync callers (like the CLI entry point) use `asyncio.run()` at the top level.

### Type hints

All public APIs are fully type-hinted. `pyright` in strict mode is the goal. Internal helpers should also be typed but can use less strict settings if needed.

### Imports

Absolute imports only (`from kilntainers.backends.base import Backend`), no relative imports. This is clearer and less error-prone in a small codebase.

---

## 8. Code Quality: `checks.sh`

A `checks.sh` script at the repo root is the single entry point for all local development checks. This is a pattern the maintainer uses across projects — one script that runs formatting, linting, type checking, and tests in sequence.

The script runs the same checks that CI enforces, so developers catch issues before pushing. Contents will be ported from an existing reference project, adapted for this codebase. Expected checks:

1. **Formatting** — `ruff format --check` (or `ruff format` to auto-fix)
2. **Linting** — `ruff check`
3. **Type checking** — `pyright`
4. **Unit tests** — `pytest tests/unit/`
5. **Integration tests** — `pytest tests/integration/` (may be skippable locally if Docker isn't available)

All commands run via `uv run` so they use the project's managed environment.

---

## 9. CI: GitHub Actions

GitHub Actions runs the same checks as `checks.sh`, broken into separate jobs for clear feedback on what failed. The workflow configuration will be adapted from the maintainer's reference files for other Python projects.

### Workflow structure

**`.github/workflows/ci.yml`** with these jobs:

| Job | What it does | Docker required? |
|---|---|---|
| **format** | `ruff format --check` | No |
| **lint** | `ruff check` | No |
| **typecheck** | `pyright` | No |
| **unit-tests** | `pytest tests/unit/` | No |
| **integration-tests** | `pytest tests/integration/` | Yes |

### Key details

- **Python version:** 3.13 (matching `requires-python`).
- **uv** is used in CI for dependency installation (`uv sync`) and running commands (`uv run`).
- **Integration tests** run on a runner with Docker available (GitHub's default `ubuntu-latest` runners include Docker).
- **Separation** into jobs means a formatting issue doesn't block seeing whether tests pass. Each job has a clear pass/fail signal.

---

## 10. Testing

Testing structure and conventions for the project. Each later architecture phase adds test details specific to that layer; this section establishes the overall approach.

### Framework

**pytest** with **pytest-asyncio** for async tests. pytest is the standard Python test framework — mature, extensible, great fixture system.

### Test organization

```
tests/
├── conftest.py                 # shared fixtures (e.g., mock backend, sample configs)
├── unit/                       # fast, no external dependencies
│   ├── conftest.py
│   ├── test_cli.py
│   ├── test_config.py
│   ├── test_server.py
│   └── backends/
│       ├── test_base.py        # ABC contract verification
│       └── test_docker.py      # Docker backend with mocked subprocess calls
└── integration/
    ├── conftest.py
    └── test_docker_integration.py   # requires real Docker daemon
```

### Unit vs integration split

- **Unit tests** (`tests/unit/`): Fast, no Docker required. Mock subprocess calls for Docker backend tests, mock backend for MCP server tests. These run in CI without Docker-in-Docker.
- **Integration tests** (`tests/integration/`): Require a real Docker daemon. Spin up actual containers, run real exec calls, verify end-to-end behavior. Run on GitHub Actions `ubuntu-latest` which includes Docker.

### Running tests

```bash
# All unit tests (fast, no Docker needed)
uv run pytest tests/unit/

# Integration tests (requires Docker)
uv run pytest tests/integration/

# Everything
uv run pytest
```

### Markers

pytest markers to control which tests run:

```python
# in pyproject.toml
@pytest.mark.integration  # Integration Test - Requires External Service
```

`checks.sh` and CI run unit and docker integration tests as separate steps for clear signal.

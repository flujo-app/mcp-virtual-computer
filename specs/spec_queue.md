# Kilntainers - Spec Queue

Checklist of items that need to be fully specified in the functional spec. These have been identified during planning Q&A but intentionally deferred from early decision-making to get proper treatment in the spec.

Items marked with [x] have been decided (see [decisions.md](decisions.md)) but may still need detailed spec treatment. Items marked [ ] are still open.

---

## Exec Behavior

- [x] **Command interface format**: Both string (`command`) and array (`args`), mutually exclusive. Backend receives whichever was provided and decides how to invoke (e.g., which shell for `command` mode). (Decision D15 revised, D20)
- [x] **Exec parameters**: `command` or `args`, `working_directory` (optional), `timeout` (optional, seconds). No `env` param. (Decision D21)
- [x] **Exec timeout policy**: 120s default, configurable at startup (`--timeout`), per-call override. Communicated via exit code + stderr. (Decision D25)
- [x] **Output size limits**: 2MB default, configurable at startup (`--output-limit`). Exceeding limit = error (kill process, return error, no partial output). Agent retries with head/tail/grep. (Decision D24)
- [x] **Binary output handling**: No special handling. UTF-8 mangling is fine, 2MB limit protects. (Decision D27)
- [x] **Stdin policy**: Optional `stdin` parameter pipes content to command's standard input, 2 MiB limit. When absent, stdin not connected (EOF). (Decision D30, Functional spec §2.1, §2.6)
- [x] **Environment variables**: No `env` param. Use inline `FOO=bar cmd` syntax. (Decision D21)
- [x] **Working directory default**: Container's WORKDIR, falling back to `/`. (Decision D13)
- [x] **Exec response schema**: `{stdout, stderr, exit_code, exec_duration_ms}`. No custom fields for timeout/truncation -- these are communicated through output text. (Decision D22)

## Error Model

- [x] **Sandbox death handling**: Drop the MCP connection. In-flight exec gets an error response first, then connection drops. (Decision D23)
- [x] **Error contract for exec failures**: Normal results (including timeout/output limit) use `isError: false`. Infrastructure failures use `isError: true`. Exit code 124 for timeout, 1 for output limit. Messages in stderr. Output limit scope: combined stdout+stderr. (Functional spec §2.5)
- [x] **Graceful shutdown behavior**: In-flight exec killed immediately on disconnect. Sandbox torn down with 10s force-kill timeout. (Functional spec §4.4)

## Backend Interface

- [x] **Backend abstraction design**: ABC (Decision D19). Five required operations: validate, start, stop, exec, tool_instructions. Behavioral contract defined. (Functional spec §5.1)
- [x] **Backend responsibilities**: Timeout enforcement, output limit enforcement, shell selection, lifecycle management, death detection, resource isolation. (Functional spec §5.3)
- [x] **Orphaned sandbox cleanup**: V1: Docker `--rm` flag + `kilntainers=true` label for manual identification. `kilntainers cleanup` subcommand deferred to FUTURE. (Functional spec §5.4)
- [x] **Backend parameter passing**: Flat CLI args, no namespacing. (Decision D12)
- [x] **Backend abstraction boundaries**: Compatibility contract defines what must be consistent and what may vary. (Functional spec §5.2)
- [x] **Backend-provided tool instructions**: Override replaces everything; extended appends to backend with double newline; both provided = error; backend null + no override = fail to start. (Decision D16)
- [x] **Shell selection**: Backend responsibility, not MCP layer. Backend's tool description must indicate what shell/command syntax is supported. (Decision D20)

## Docker Backend (V1)

- [x] **Default base image**: Debian slim (bookworm-slim or current stable). (Decision D11)
- [x] **Container naming convention**: Docker containers labeled `kilntainers=true` for identification by cleanup command. (Functional spec §5.4)
- [x] **Resource defaults**: No explicit limits by default (Docker defaults). Operators set limits for production. (Functional spec §3.2)
- [x] **Docker SDK vs CLI**: CLI via subprocess. (Decision D10)
- [x] **Container startup flow**: Pull → create/start → verify readiness (trivial exec) → accept calls. Pull failure = startup error. (Functional spec §4.3)
- [x] **Docker config complexity**: Flat CLI args for v1 with `--docker-run-flag` escape hatch for uncovered Docker options. (Functional spec §3.2)

## MCP Interface

- [x] **Transport**: stdio and Streamable HTTP. No SSE (deprecated, different transport). (Decision D8 corrected, Functional spec §1)
- [x] **Tool name**: `terminal_execute`. No global default description -- backend provides or user overrides. (Decision D9)
- [x] **Tool description text**: Drafted for Docker backend with dynamic timeout/output-limit values. (Functional spec §7)
- [x] **MCP server startup parameters**: Full CLI parameter schema including core params and Docker backend params. (Functional spec §3.1, §3.2)
- [x] **Connection lifecycle details**: stdio: one sandbox per process. Streamable HTTP: one sandbox per session (Mcp-Session-Id), 5min idle timeout (configurable). Startup: pull → start → verify → accept. Shutdown: kill in-flight, stop sandbox, 10s force-kill. Death: drop connection. (Functional spec §4)

## Security

- [x] **Resource limits as part of interface**: Purely backend-specific config, not part of the ABC. (Decision D26)
- [x] **Security model documentation**: Threat model covering exfiltration, resource abuse, container escape, host filesystem access, and HTTP exposure. Operator responsibilities documented. (Functional spec §9)

## Testing

- [ ] **Testing strategy**: Unit test approach, integration test approach (real Docker), CI/CD requirements (Docker-in-Docker or alternatives), mock backend for testing MCP layer independently.

## Future-Proofing

- [x] **Mapped working directory hooks**: Design optional `mounts`/`volumes` parameter in backend interface spec. Document but don't implement in v1. (Decision D14)
- [x] **Parallel exec interface**: API says nothing about serialization. Internal v1 implementation queues and serializes. No API change needed for future parallelism. (Decision D29)
- [x] **Multi-sandbox support**: Required in v1 (Streamable HTTP has concurrent sessions). No global sandbox state. Each sandbox is an independent object with explicit handle. (Decision D28)

---

*Last updated after functional spec. All items resolved except Testing Strategy, which is deferred to the architecture/implementation phase.*

---

## Architecture Specification

This section tracks the creation of detailed architecture documents. Unlike the functional spec (which defines *what* the system does), architecture docs define *how* it is built — module structure, class hierarchies, data flow, and implementation patterns.

### Process

1. **Create `specs/architecture/` folder** for all architecture sub-documents.
2. **Work through each phase below in order.** Each phase produces a focused architecture document in `specs/architecture/`. Phases build on each other, so earlier phases inform later ones.
3. **Each phase document should include a testing section** covering how that layer/component is tested (unit tests, mocks, integration tests as appropriate). Testing is not a separate phase — it is part of every phase.
4. **Review each phase with the user before marking complete.** Present key decisions and design details for confirmation. When confirmed, mark the phase `[x]` below.
5. **After all phases are complete**, write `specs/architecture/architecture_summary.md` — a short index document that points to each sub-document with a high-level description of what it covers.

### Spec Phases

Note: if you're asked to implement a "phase" it's referring to a phase from `specs/implementation_plan.md` NOT THESE

- [x] **Phase 1: Project structure & packaging** (`project_structure.md`)
  Python package layout, directory structure, entry points, dependency management (pyproject.toml / requirements), and naming conventions. Establishes the skeleton everything else lives in.

- [x] **Phase 2: Backend abstraction layer** (`backend_abstraction.md`)
  The Python ABC definitions for Backend and Sandbox, method signatures, type contracts, async patterns, and how the MCP layer interacts with backends without knowing which one is running. This is the central interface — get it right before implementing either side.

- [x] **Phase 3: Docker backend implementation** (`docker_backend.md`)
  How the Docker backend implements the abstraction: subprocess calls to the Docker CLI, container create/start/exec/stop flows, `--rm` and labeling, image pull mechanics, readiness checks, timeout and output-limit enforcement at the process level, death detection, and resource limit passthrough.

- [x] **Phase 4: MCP server & tool layer** (`mcp_server.md`)
  How the MCP server is structured. Tool registration, request validation, response formatting, tool description assembly, and how the server delegates to the backend. Covers both stdio and Streamable HTTP wiring. Note: the MCP Python ecosystem has two libraries — `mcp` (official low-level SDK) and `FastMCP` (higher-level, separate project). This phase should evaluate and decide which to use.

- [x] **Phase 5: CLI, configuration & startup** (`cli_and_startup.md`)
  Argument parsing (library choice, arg definitions), startup validation sequence, how backend-specific args are routed, configuration objects, and the full startup flow from process launch to "ready to accept connections."

- [x] **Phase 6: Connection & session lifecycle** (`connection_lifecycle.md`)
  How stdio and Streamable HTTP transports map to sandbox lifecycles. Session tracking for HTTP (creation, idle timeout, teardown), sandbox ownership, graceful shutdown orchestration, and sandbox death propagation to the MCP layer.

- [x] **Phase 7: Error handling & observability** (`error_handling.md`)
  Error propagation paths from backend through MCP layer to client. How timeout, output-limit, sandbox death, and validation errors are caught, transformed, and reported. Startup error reporting. Stderr usage patterns. Any structured error types or exception hierarchy.

### Completion

- [x] **Architecture summary** (`architecture_summary.md`)
  Written after all phases above are complete. A short document that lists each sub-document with a 1–2 sentence description of what it covers, serving as the entry point for anyone reading the architecture docs.

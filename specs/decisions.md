# Kilntainers - Decision Log

Decisions made during planning, with rationale. This is the source of truth for "what did we decide and why."

---

## D1: Tech Stack -- Python

**Decision:** Python

**Rationale:** The MCP ecosystem is most mature in Python, with the best SDKs and community examples. For a project whose primary interface *is* MCP, this matters more than Go's distribution advantages. The MCP server is a thin orchestrator calling Docker -- Python's performance is more than sufficient. Python is also the dominant language in the AI/LLM community, which is our target user base.

**Trade-off acknowledged:** Distribution is harder than Go (no single binary). We'll need to manage packaging (pip, pipx, Docker image of the server itself, etc.).

---

## D2: V1 Backend Scope -- Docker Only, Designed for Pluggability

**Decision:** V1 ships with Docker as the only backend. The backend interface is designed as a clean abstraction so additional backends (Modal, E2B, WASI, etc.) can be added later without changing the MCP layer.

**Rationale:** Shipping all backends at once risks building an abstraction before validating it. Docker-first lets us prove the interface with a real, well-understood backend. The abstraction can be refined when the second backend is added.

**Implication:** The backend interface (Python ABC/Protocol) must be thoughtfully designed even though only Docker implements it in v1. "Design for pluggability" means we don't hardcode Docker assumptions in the MCP layer.

---

## D3: File I/O -- Deferred to Mapped Working Directory Phase

**Decision:** No dedicated file upload/download tools in v1. File I/O into/out of the sandbox is deferred to the "Mapped Working Directory" feature.

**Rationale:** The core v1 use case is "agent has a sandbox to work in." The agent creates and manipulates files via `exec` (heredocs, echo, python scripts). Mapped working directory is the right long-term solution for host<->sandbox file transfer.

**V1 limitation acknowledged:** Without mapped directories, there is no clean way to seed the sandbox with external data (datasets, codebases) or extract generated artifacts. Users who need this will have to wait for mapped working directory or use workarounds (base64 via exec for small files, pre-baked into custom Docker images for larger ones).

---

## D4: No Parallel Exec -- V1 Simplification

**Decision:** Exec calls are queued and run serially in v1. This is a simplification, not a permanent design constraint.

**Rationale:** Serial execution avoids filesystem race conditions and simplifies the implementation. For most agent workloads (sequential tool calls), this is fine.

**Future:** The interface should not *prevent* parallelism -- it's a backend implementation choice. A future version could allow concurrent exec with an opt-in flag or simply remove the serialization.

---

## D5: Network Enabled by Default

**Decision:** The MCP server sets `network_enabled` to `true` by default. Users can
explicitly opt out with `--no-network`.

**Rationale:** Most practical agent tasks need network access for package installation,
API calls, and downloads. Operators handling untrusted data or secrets can explicitly
disable it at startup.

**Implication:** Operators must treat sandbox network access as part of the threat model
and use `--no-network` when isolation is required.

---

## D6: No Crash Recovery in V1

**Decision:** If the sandbox crashes (OOM, container killed, Docker daemon restart), it's dead. The MCP server does not attempt to restart it. The connection is effectively over.

**Rationale:** Crash recovery adds significant complexity (state restoration, in-flight exec handling, reconnection logic). For ephemeral sandboxes, "start over" is an acceptable v1 answer.

**What we do need:** Clear error reporting when the sandbox dies (not a hang), and orphaned container cleanup so crashed containers don't accumulate.

---

## D7: Output Limits, Timeouts, Command Interface, Error Model, Backend Abstraction -- Queued for Spec

**Decision:** These are important but need proper specification, not quick decisions. They are tracked in the [Spec Queue](spec_queue.md) and will be addressed during functional spec writing.

Items queued:
- Exec output limits and truncation strategy
- Exec timeout policy  
- Command interface (string vs array vs both)
- Error contract (command failure vs infrastructure failure)
- Backend abstraction boundaries and compatibility expectations
- Backend responsibilities (lifecycle, cleanup, resource limits)
- Orphaned container cleanup strategy

---

## D8: MCP Transport -- stdio and Streamable HTTP (No SSE)

**Decision:** Support both stdio and Streamable HTTP transports from v1. The legacy SSE transport is **not supported** — SSE and Streamable HTTP are different transports, and SSE is deprecated in the MCP protocol.

**Rationale:** We're using a Python MCP library, so supporting both transports is minimal incremental work. stdio is what most desktop MCP clients use today. Streamable HTTP is needed for remote/server deployments. Both are current standard MCP transports.

**Lifecycle implication:** stdio = one sandbox for the lifetime of the process. Streamable HTTP = one sandbox per session.

---

## D9: Tool Name -- `terminal_execute`

**Decision:** The MCP tool is named `terminal_execute`.

**Tool description:** No global/MCP-level default description. The backend must provide a tool description via its `tool_instructions()` method, or the user must provide `tool_instruction_override` at startup. If neither is provided, the server fails to start.

**Rationale:** Each backend has different capabilities and constraints. A generic description would be misleading for limited backends (e.g., WASI/BusyBox). The backend knows its own capabilities best. Exact description wording to be finalized during spec.

---

## D10: Docker Integration -- CLI via Subprocess

**Decision:** The Docker backend calls the `docker` CLI via subprocess, not the `docker-py` SDK.

**Rationale:** CLI is more debuggable (users can reproduce commands manually), avoids SDK version drift, and matches what users expect. The MCP server is a thin orchestrator -- subprocess calls to a well-known CLI are appropriate.

**Trade-off acknowledged:** Parsing CLI output is slightly more fragile than SDK objects. We accept this since Docker CLI output formats are stable and well-known.

---

## D10a: Container Engine -- Swappable CLI Name via `--engine`

**Decision:** The Docker backend accepts an `--engine` parameter (default: `docker`) that controls which container CLI binary is invoked. This enables drop-in support for Docker-compatible engines like Podman (`--engine podman`) without a separate backend.

**Rationale:** Podman is explicitly designed as a CLI-compatible replacement for Docker — commands, flags, and output formats are intentionally identical. A swappable CLI name is the lowest-effort approach and avoids adding a new backend, an SDK dependency, or an abstraction layer. This stays consistent with D10's rationale (subprocess CLI calls are debuggable and reproducible). Using `docker-py` was considered and rejected: it would contradict D10, introduce SDK version drift, and Podman's Docker-compatible API socket requires extra setup (`podman system service`) that users don't expect.

**Scope:** This is a Docker backend parameter, not a core parameter. It changes the binary name in subprocess calls — nothing else. If a rare flag differs between engines, a targeted `if` branch is acceptable, but the expectation is near-zero divergence for the container lifecycle commands used (run, exec, cp, stop, rm).

---

## D11: Default Base Image -- Debian Slim

**Decision:** The Docker backend defaults to `debian:slim` (specifically `debian:bookworm-slim` or current stable) when no image is specified.

**Rationale:** Debian slim is a good balance of size, compatibility, and "just works." Alpine's musl libc causes real breakage with many binaries and pip packages. Ubuntu is heavier without meaningful benefit over Debian for a sandbox use case.

---

## D12: Backend Parameters -- Flat CLI Args

**Decision:** Backend-specific parameters are passed as flat CLI args (e.g., `--image`, `--cpu`), not namespaced (e.g., `--docker-image`).

**Rationale:** Since only one backend runs at a time, there's no collision risk. Flat args are simpler and align with how MCP clients typically configure server startup commands. Individual backends may accept a config file path as one of their flat args if complex configuration is needed, but this is a backend implementation detail.

---

## D13: Working Directory Default -- Container's WORKDIR

**Decision:** When `working_directory` is omitted from an `exec` call, the command runs in the container's WORKDIR as defined by the image (Dockerfile). If no WORKDIR is set in the image, it defaults to `/`.

**Rationale:** This is predictable, standard Docker behavior, and lets image authors control it. No invented convention needed.

**Backend contract:** All backends must support this semantic -- "use the image/environment's default working directory."

---

## D14: Mapped Working Directory -- Design in Spec, Defer Implementation

**Decision:** The backend interface spec will include an optional `mounts`/`volumes` parameter in the sandbox start method. This parameter is documented and designed in v1 but not implemented. Implementation comes in a future phase (mapped working directory feature).

**Rationale:** Designing the parameter now ensures the interface won't need breaking changes later. Since it's optional with no implementation, there's zero v1 cost. The spec should validate that the design works for Docker bind mounts, and note considerations for future backends (Modal volumes, E2B file sync, etc.).

---

## D15: Command Interface -- Both String and Array (Revised)

**Decision:** The `terminal_execute` MCP tool accepts two mutually exclusive parameters:
- `command` (string): A shell command string. The backend is responsible for executing this through an appropriate shell (e.g., Docker/Debian uses `bash -c`, a BusyBox backend uses `sh -c`, etc.).
- `args` (string array): Passed directly to the process as exec arguments, no shell involved. Use for programmatic calls where argument integrity matters (e.g., text editor passing file contents).

Providing both is an error. Providing neither is an error.

An optional `stdin` parameter (D30) can be combined with either mode to pipe data to the command's standard input, solving the shell escaping problem for data passing.

**Rationale:**
- Docker and Modal are natively array-based. For `command` mode, the backend wraps in its shell of choice.
- The text editor use case requires passing multi-line strings with quotes, newlines, and special characters. With string-only mode, this requires painful multi-layer escaping (JSON -> shell -> argument). Array mode passes arguments cleanly via JSON only.
- Implementation cost is minimal: the backend knows which shell is available and how to invoke it.
- E2B is string-based natively (fits `command` mode directly).

**Backend contract:** The backend `exec` method receives either a `command` string or an `args` list, plus an optional `stdin` string. The backend decides how to invoke string commands (which shell, what flags). The backend's tool description should indicate what shell/command syntax is supported (bash, POSIX sh, etc.). This is a key reason we require backend-provided tool descriptions rather than having a global default.

---

## D16: Tool Description Assembly Order

**Decision:** The exec tool description shown to the LLM is assembled as follows:
1. If `tool_instruction_override` is provided: use it as the entire description. Ignore backend and `extended_tool_instruction`.
2. Otherwise: `backend.tool_instructions()` + `"\n\n"` + `extended_tool_instruction` (if provided).
3. If the backend returns null/empty and no `tool_instruction_override` is provided: fail to start with a clear error.
4. Providing both `tool_instruction_override` and `extended_tool_instruction` is an error (invalid configuration -- the user is both replacing and extending, which is contradictory).

**Rationale:** Clean precedence. Override replaces everything. Extension appends to backend defaults. Providing both is a user config error.

---

## D17: MCP Python Library -- Official `mcp` Package

**Decision:** Use the official `mcp` Python package (from Anthropic/modelcontextprotocol).

**Rationale:** It's the canonical implementation, well-maintained, supports both stdio and Streamable HTTP transports, and includes the FastMCP convenience layer.

---

## D18: Docker Image Pull -- Inline, Blocking

**Decision:** When the configured Docker image isn't present locally, pull it inline during sandbox startup. The MCP client waits.

**Rationale:** Simplest approach. Requiring pre-pulled images adds a setup step that users will forget. The pull only happens once (Docker caches the image). First-run latency is acceptable and expected.

**Note:** The pull may take 30+ seconds for large images. Consider logging progress to stderr so the user sees something is happening. Future optimization: pre-pull in background, warm pools, etc.

---

## D19: Backend Interface Style -- ABC

**Decision:** The backend interface is defined as a Python ABC (Abstract Base Class).

**Rationale:** ABC is more explicit than Protocol for this use case. Missing method implementations are caught at class definition time, not at runtime. For a pluggable backend system where correctness is important, the strictness of ABC is a feature. Backend authors get clear errors immediately if they forget to implement a method.

---

## D20: Shell Selection is a Backend Responsibility

**Decision:** The MCP layer does not decide which shell to use. When the agent provides a `command` (string), the MCP layer passes it to the backend as-is. The backend decides how to execute it -- Docker/Debian might use `bash -c`, a BusyBox backend uses `sh -c`, an emulator might have something else entirely.

**Rationale:** Different backends have different shells available. The caller shouldn't need to know or specify the shell. The backend's tool description (which is mandatory) indicates what command syntax is supported, giving the LLM the right context.

**Implication:** This reinforces why backend-provided tool descriptions are mandatory -- the description must tell the LLM whether it's working with bash, POSIX sh, or something else.

---

## D21: Exec Parameters -- Add Optional Timeout, Stdin, No Env

**Decision:** The `terminal_execute` tool parameters are:
- `command` (string, mutually exclusive with `args`)
- `args` (string array, mutually exclusive with `command`)
- `stdin` (optional string) -- piped to the command's standard input. Usable with either `command` or `args`. (D30)
- `working_directory` (optional string)
- `timeout` (optional int, seconds) -- per-call override of the default timeout

No `env` parameter. Environment variables can be set inline in the command (`FOO=bar some_command`) or via the shell.

**Rationale:** Timeout override is useful for known-long tasks (e.g., `apt-get install`, large builds). Stdin enables safe data passing without shell escaping -- critical for writing files and piping data to commands (D30). Env is not worth the interface complexity since inline syntax works fine and is natural for shell commands.

---

## D22: Exec Response Schema -- Minimal, With Duration

**Decision:** The exec response includes:
- `stdout` (string)
- `stderr` (string)
- `exit_code` (int)
- `exec_duration_ms` (int)

No custom fields for `timed_out` or `output_limit_exceeded`. These conditions are communicated through exit codes and stderr messages:
- **Timeout:** Process is killed, returns a timeout exit code (e.g., 124 or 137), stderr includes a clear message like `[TIMED OUT after 120s]`.
- **Output limit exceeded:** Process is killed, returns a non-zero exit code, stderr explains the limit was hit and no output is returned. Agent can re-run with `head`/`tail`/`grep` to get manageable output. (Decision D24)

**Rationale:** Keeping the schema minimal and communicating exceptional conditions through the output text is simpler, and it puts the information where agents actually read it (stdout/stderr). Agents don't parse response metadata fields, they read the text.

---

## D23: Sandbox Death -- Drop the MCP Connection

**Decision:** If the sandbox dies (OOM, killed, Docker daemon crash), the MCP connection is terminated.

- If death happens *between* exec calls: connection drops, client sees server disconnected.
- If death happens *during* an exec call: return the error for the in-flight call (with stderr explaining what happened), then drop the connection.

**Rationale:** Sandbox death is unrecoverable (D6). Dropping the connection signals this cleanly to the client. Most MCP clients will offer to restart the server, which gives the user a fresh sandbox. Returning errors within the connection (option 2) would leave the agent in a broken state where every subsequent call fails -- worse UX than a clean break.

**For stdio:** Connection drop = process exit. Client restarts the server process.
**For Streamable HTTP:** Connection drop = session terminated. Client can reconnect and get a new sandbox.

---

## D24: Output Limit -- Error, Don't Truncate

**Decision:** If command output (stdout or stderr) exceeds a configurable limit (default 2 MiB), the command is killed and an error is returned: non-zero exit code with a clear stderr message explaining the output exceeded the limit. The oversized output is *not* returned (not even partially).

Configurable at startup via `--output-limit` flag.

**Rationale:** Truncating silently or even with a message means the agent is working with incomplete data it didn't ask for. Returning an error forces the agent to be intentional: it can re-run with `head`, `tail`, `grep`, `wc -l`, or decide it doesn't need that output at all. This is a better feedback loop and avoids blowing up LLM context windows with data the agent can't fully use.

---

## D25: Default Exec Timeout -- 120 Seconds

**Decision:** Default per-exec timeout is 120 seconds. Configurable at startup via `--timeout` flag. Overridable per-call via the optional `timeout` parameter on `terminal_execute`.

**Rationale:** 120s is long enough for package installs, builds, and test suites. Short enough to catch infinite loops and hung processes. Per-call override lets the agent request more time for known-long operations.

---

## D26: Resource Limits -- Purely Backend-Specific

**Decision:** Resource limits (CPU, memory, disk, process count) are not part of the backend ABC interface. They are backend-specific configuration, passed as flat CLI args to each backend.

**Rationale:** Resource limits vary wildly across backends (Docker cgroups vs Modal pricing tiers vs E2B plans vs WASI capabilities). Standardizing a resource limit interface would be premature and would either be too generic to be useful or too specific to fit all backends. Each backend defines its own resource-related args.

---

## D27: Binary Output -- No Special Handling

**Decision:** No special detection or handling of binary output. Binary data will be mangled through UTF-8 string encoding in stdout/stderr. The 2 MiB output limit (D24) protects against massive binary blobs.

**Rationale:** Not worth the complexity for v1. Agents will see garbage and learn not to cat binary files. The output limit keeps it bounded.

---

## D28: Multi-Sandbox Support -- Required in V1

**Decision:** The backend API and MCP server must support multiple concurrent sandboxes in v1. This is required because Streamable HTTP mode creates one sandbox per session, and multiple clients can connect simultaneously.

The API must be clean: no global state, no singleton sandbox. Each sandbox is an independent object. Calling exec on a specific sandbox must be explicit and clear.

**Rationale:** This isn't a future-proofing concern -- it's a v1 requirement. A Streamable HTTP MCP server will have concurrent sessions, each needing its own sandbox. The design must handle this from day one.

**Implication:** Backend `start()` returns a sandbox handle/object. `exec()`, `stop()` operate on that handle. No global "the sandbox" concept.

---

## D29: No Parallel Exec -- Internal Implementation Detail

**Decision:** Serial execution of exec calls within a single sandbox is a v1 implementation detail, not an API-level constraint. The API does not mention or enforce serialization. Internally, the implementation queues concurrent exec calls to the same sandbox and runs them serially.

**Rationale:** The API should not prevent future parallelism. If we later allow concurrent exec within a sandbox, nothing in the external interface changes -- it's just a backend behavior change.

---

## D30: Add `stdin` Parameter for Safe Data Passing

**Decision:** Add an optional `stdin` (string) parameter to `terminal_execute`, usable with either `command` or `args` mode. The string is piped to the command's standard input, completely bypassing the shell's argument parsing. Subject to a 2 MiB size limit (matching the output limit default). `command` and `args` remain mutually exclusive (D15 unchanged).

**Problem solved:** The existing interface forces a choice between shell features (`command` mode — pipes, redirects) and safe data passing (`args` mode — no escaping needed). There's no way to combine both. An LLM writing a large file with nested quotes, newlines, and JSON into the container faces a multi-layer escaping nightmare (JSON → shell → argument) in `command` mode, and can't redirect to a file in `args` mode (no shell). The LLM doesn't even know which shell is running (D20), so it can't escape correctly.

**How `stdin` solves it:**
- Write a file: `command: "cat > output.txt"`, `stdin: "big content with 'quotes' and {\"json\": true}"` — only needs `cat` and a shell, POSIX universal. No container-specific tools required.
- Pipe to a program: `command: "python3 process.py"`, `stdin: "input data..."`
- Pass to grep: `command: "grep -c 'error'"`, `stdin: "<log content>"`

**Why one data stream is sufficient:** `stdin` carries one blob, which initially seems limiting for multi-parameter operations (str_replace needs old + new). But LLMs naturally decompose these into multiple calls: (1) read the file, (2) transform in LLM context, (3) write back via `stdin`. This is how coding agents already work. We cannot assume containers have Python, sed, or any specific tool — solutions must work with minimal images, which rules out `args`-based multi-parameter patterns.

**Alternatives considered:**
- *Parameterized shell commands* (`command` + `args` together as `$1`, `$2`): Re-introduces shell quoting — the LLM must understand positional parameter syntax and remember to double-quote `$N` references.
- *Separate `write_file` tool*: Only solves file writing, not general data passing. Adds a second MCP tool.
- *Keep current design*: Doesn't solve the problem. `args: ["bash", "-c", ...]` re-introduces shell escaping inside the array and pushes shell knowledge to the LLM (contradicts D20).

**Size limit:** `stdin` content is limited to 2 MiB (same default as the output limit). If exceeded, the call returns an MCP error (`isError: true`) without executing the command.

**Backend contract:** All backends must support piping `stdin` content to the process (Docker: `docker exec -i`, subprocess: `communicate(input=...)`).

**Tool description:** Must be updated to explain `stdin` usage patterns, particularly `cat > file` + `stdin` for file writing. Exact wording to be finalized during implementation.

---

## D31: No Additional Logging -- Focus on Great Errors

**Decision:** No dedicated logging system in v1. The MCP server does not write log files or structured log output beyond what is needed for startup errors and image pull progress (both on stderr).

**Rationale:** In stdio mode — the primary transport — stderr is the only available channel, and it's shared with the MCP protocol's own error reporting. Adding logging infrastructure creates noise without clear value. The better investment is making every error response (MCP errors, timeout messages, output limit messages, sandbox death reports) clear, actionable, and self-explanatory. If the error responses are good enough, operators don't need logs to diagnose issues — the agent's conversation history contains everything.

**For Streamable HTTP mode:** The same principle applies. Standard HTTP request logging (if desired) can be handled by a reverse proxy in front of the server, which is already recommended for production deployments (D8, §9).

**Future:** If demand arises, a `--verbose` or `--log-level` flag can be added without changing the architecture. But the v1 bet is that great errors are more valuable than great logs.

---

## D32: Stdin Size Limit -- Fixed at 2 MiB

**Decision:** The `stdin` parameter size limit is always 2 MiB (2,097,152 bytes). It is not configurable and does not track the `--output-limit` setting.

**Rationale:** The stdin limit and output limit serve different purposes. The output limit protects LLM context windows from oversized responses and varies by use case (some users want 10 MiB output for data processing). The stdin limit protects the server from oversized input payloads — 2 MiB is generous for any reasonable stdin use case (file writes, data piping). Users needing to transfer larger data into the sandbox should use alternative approaches (split across multiple calls, pre-bake into custom images, or the future mapped working directory feature). A fixed limit keeps the interface simple — one fewer knob to configure.

# Kilntainers - Planning Q&A and Design Challenges

This document captures open questions, design challenges, and underspecified areas in the project overview. The goal is to sharpen the spec before moving to a functional spec and architecture.

---

## 1. Design Points to Challenge

### 1.1 Is a Single "exec" Tool Really Sufficient?

The spec proposes one MCP tool: `exec`. This is elegantly simple, but may be *too* simple for practical agent use.

- **File transfer:** How does an agent get files *into* or *out of* the sandbox? For example, an agent wants to analyze a CSV the user uploaded, or return a generated PDF. With only `exec`, the agent would need to `base64`-encode content and echo it -- extremely clunky and token-expensive for large files. Should we have `upload` and `download` tools (or at least one)?
- **Reading large files:** `cat bigfile.txt` returns the entire thing through stdout. There's no built-in pagination. The text editor tool is planned but "later." Should basic file read/write be in v1?
- **Observation vs Action:** Many agent frameworks distinguish between "read" tools (cheap, low-risk) and "write" tools (expensive, higher-risk). A single `exec` conflates both. Does this matter for how MCP clients present approval UIs?

**Decision needed:** Define the minimal tool surface for v1. Is it truly just `exec`, or do we need `upload`/`download` at minimum?

### 1.2 No Parallel Exec -- Why, and At What Cost?

The spec says "queue any calls and issue serially." This needs justification.

- Serial execution makes the sandbox simpler (no race conditions on the filesystem), but it means the agent can't do anything else while a long-running command executes.
- Many real workloads benefit from parallelism: run tests in one call while linting in another.
- If the concern is filesystem races, we could document that parallel exec is "at your own risk" rather than preventing it.
- If the concern is implementation complexity, that's fair, but should be stated.

**Decision needed:** Is serial execution a hard requirement, or a v1 simplification we want to revisit? Should we design the interface to allow parallelism later even if we serialize today?

### 1.3 One Sandbox Per Connection -- Is This Too Rigid?

The spec ties sandbox lifecycle to connection lifecycle. This means:

- **No multi-sandbox workflows.** An agent can't spin up a Python sandbox *and* a Node sandbox and have them interact. Is that a real use case?
- **No sandbox reuse.** If the connection drops, the sandbox is gone. For long-running tasks over HTTP streaming, connection flakiness = lost work.
- **No explicit lifecycle control.** The agent can't say "I'm done, tear it down" or "keep it warm." It's purely implicit.

Counter-argument: simplicity is a feature, and multi-sandbox is a different product. But we should consider whether the backend API should *allow* multiple sandboxes even if MCP exposes one.

**Decision needed:** Should the Backend API support multiple concurrent sandboxes (even if MCP v1 doesn't expose it)?

### 1.4 Network-Off Default vs. Real-World Utility

Network disabled by default is good security. But many practical agent tasks need network:

- `uv add`, `npm install`, `apt-get install` -- all need network.
- Fetching URLs, calling APIs, downloading datasets.
- The base image must be *very* complete if agents can't install anything.

This creates tension: either we ship fat base images (slow to start, large to store), or we accept that many users will need `network_enabled: true` (weakening the security story).

**Discussion point:** Should we consider a middle ground? For example: network enabled only during a "setup" phase, then disabled? Or a whitelist of allowed domains/IPs?

### 1.5 Extended Tool Instructions -- Who Is the Audience?

`extended_tool_instruction` and `tool_instruction_override` are interesting, but:

- These become part of the MCP tool description that LLMs read. LLM tool description space is limited and precious.
- Should these be structured (capabilities list) rather than freeform strings?
- Who writes these? The end user? That's asking a lot. The backend? That makes more sense.
- What's the interaction between backend-provided `Tool Instructions` and user-provided `extended_tool_instruction`? Are they concatenated? In what order?

**Decision needed:** Define the instruction assembly order: backend defaults + user extensions + overrides.

---

## 2. Technical Points to Challenge

### 2.1 Command Interface: String vs Array vs Both

This is explicitly called out as open. Let's frame the tradeoffs:

| Approach | Pros | Cons |
|----------|------|------|
| **String only** | Simple for LLMs to generate; matches how humans type commands | Shell injection risk if not handled; relies on shell for parsing; shell must exist in sandbox |
| **Array only** | Clean escaping; no shell injection; works without a shell | LLMs are worse at generating arrays for complex pipelines; no pipes/redirects without shell |
| **Both** | Flexibility | Complexity; two code paths to test; LLMs may choose the wrong one |

Key insight: if we use string mode, we're *always* invoking through a shell (`sh -c "..."`). If we use array mode, we're calling `exec` directly (no shell). These have very different semantics for pipes, redirects, env vars, etc.

**Recommendation to discuss:** String-only is probably the right call. LLMs are *very* good at shell commands. Array mode for a single tool call adds complexity without clear benefit -- the agent can always quote/escape within a string. The shell will exist in every non-WASI backend.

### 2.2 Exec Timeouts and Hung Commands

The spec doesn't mention timeouts at all. This is critical.

- What happens if the agent runs `while true; do echo x; done`?
- What if a command hangs waiting for stdin (e.g., `python` with no script)?
- Should there be a per-exec timeout? A global sandbox timeout?
- Who configures the timeout -- the MCP user at startup, or is it hardcoded?
- Should the agent be able to request a longer timeout for known-long tasks?

**Decision needed:** Define timeout behavior. Suggested: per-exec default timeout (e.g., 120s), configurable at startup, with a hard max. Return a clear timeout error to the agent.

### 2.3 Output Handling: Size Limits, Binary, Streaming

The exec returns `{stdout, stderr, status_code}`. But:

- **Size limits:** What if stdout is 50MB? Do we truncate? Error? Stream? LLMs have context limits -- sending 50MB of text as a tool response will break things.
- **Binary output:** What if the command outputs binary data? Do we return it? Base64 encode it? Error?
- **Streaming:** Long-running commands produce output over time. Does the agent see output only after the command completes? Some MCP implementations support streaming tool results -- do we want to leverage that?

**Decision needed:** Define output truncation strategy (suggest: truncate at a configurable limit, default ~1MB, with a clear message "output truncated, N bytes omitted"). Binary output should be detected and replaced with a message.

### 2.4 Stdin Support

The spec doesn't mention stdin. Many commands expect it:

- `python script.py < input.txt` (can be done via shell redirect -- fine)
- Interactive commands that prompt for input (these will just hang -- see timeout issue)

**Decision needed:** Explicitly state that stdin is not supported (commands run non-interactively). This should be documented in the tool description so LLMs know.

### 2.5 Environment Variables

Can the agent set environment variables? Since each exec is independent, `export FOO=bar` in one call won't persist to the next. Options:

- Allow env vars as a parameter to exec (like `docker exec -e`)
- Allow startup-time env vars as a backend config option
- Just tell agents to use `FOO=bar command` inline syntax

**Decision needed:** Should exec accept an `env` parameter?

### 2.6 Sandbox Crash Recovery

What happens when:

- The container/VM crashes mid-exec?
- The container OOMs?
- The Docker daemon restarts?
- The Modal/E2B session expires?

Is the sandbox just "dead"? Does the MCP server try to restart it? Does it report a special error to the agent?

**Decision needed:** Define failure modes and whether recovery is automatic, manual, or just "connection over."

### 2.7 Tech Stack Decision: Python vs Go

This needs to be decided early as it affects everything.

| Factor | Python | Go |
|--------|--------|----|
| MCP ecosystem | Better -- most MCP SDKs/examples are Python | MCP SDK exists but less mature |
| Docker/container SDKs | docker-py, well maintained | Docker SDK for Go, very mature (Docker itself is Go) |
| Async/concurrency | asyncio works but can be tricky | Goroutines are natural and excellent |
| Deployment | Needs Python runtime, venv, etc. | Single static binary -- huge advantage for distribution |
| Performance | Fine for this use case | Better, but doesn't matter much here |
| WASI/sandbox integration | Harder | More natural |
| Community | Larger AI/LLM community | Smaller but strong infra community |

**Recommendation to discuss:** Go has a compelling advantage for this project: single binary distribution, excellent concurrency, and natural fit for infrastructure tooling. Python has the edge in MCP ecosystem maturity. Consider: how much does the MCP SDK quality matter vs. rolling our own thin MCP layer?

---

## 3. Potential Issues

### 3.1 Container Cold-Start Latency

When a connection is established, we need to start a sandbox. Docker container startup is typically 1-5 seconds, but:

- Custom images may need pulling first (30s+)
- Modal/E2B have their own cold start characteristics
- WASI sandboxes should be near-instant

This latency happens *before the agent can do anything*. For MCP, the client is waiting for the server to be ready.

**Mitigation to discuss:** Pre-pull images? Warm pool? Or just accept latency and document it?

### 3.2 Resource Cleanup on Crash

If the MCP server process crashes or is killed (SIGKILL), the sandbox may be left running. For Docker, this means orphaned containers consuming resources.

**Decision needed:** Naming convention for containers so they can be cleaned up? A cleanup script? Docker `--rm` flag? Periodic garbage collection?

### 3.3 Backend Abstraction Leakiness

The spec wants one interface across Docker, Modal, E2B, WASI, etc. But these are *very* different:

- Docker: local, fast, full Linux, your machine's resources
- Modal: remote, serverless, has its own persistent volume model
- E2B: remote, their sandbox model, their API constraints
- WASI: local, limited syscalls, no real networking, restricted filesystem

Can `exec` really mean the same thing across all of these? A shell command that works in Docker may fail in WASI because the binary doesn't exist or a syscall is blocked. The abstraction may leak badly.

**Discussion point:** Should we commit to a primary backend (Docker) and treat others as best-effort? Or define a "compatibility profile" that backends must support?

### 3.4 Security: Beyond Networking

The security model focuses on network exfiltration. But agents can also:

- Mine cryptocurrency (CPU abuse)
- Fill up disk (DoS on host)
- Fork bomb (resource exhaustion)
- Read mounted volumes they shouldn't see
- Exploit container escapes (rare but real)

**Decision needed:** Should the spec define resource limits (CPU, memory, disk, process count) as part of the backend interface? Or leave it to per-backend config?

### 3.5 Multi-Instance Configuration Burden

The spec says "you can simply run several instances" for different backends/configs. But managing multiple MCP servers is painful:

- Each needs its own config in the MCP client settings
- The agent sees them as separate tools (`exec_1`, `exec_2`?)
- How does the agent know which sandbox to use for what?

**Discussion point:** Is there a better multi-sandbox story, or do we accept this as out of scope?

---

## 4. Underspecified Areas

### 4.1 Error Model

What errors can exec return beyond `{stdout, stderr, status_code}`?

- Sandbox not running / crashed
- Timeout exceeded
- Output truncated
- Command not found (is this just a status code?)
- Backend-specific errors

Should there be a structured error type separate from command output? E.g., `{stdout, stderr, status_code, error?: string, error_code?: string}`.

### 4.2 Sandbox State Inspection

Can the agent (or user) inspect sandbox state beyond exec?

- Is the sandbox still running?
- How much disk/memory is used?
- How long has it been running?

These could be additional MCP tools or part of exec's metadata.

### 4.3 Logging and Observability

The spec doesn't mention logging at all.

- Should the MCP server log all exec calls and their outputs?
- Where do logs go?
- Is there a debug mode?
- How does an operator diagnose issues?

### 4.4 Testing Strategy

How do we test this?

- Unit tests for each backend
- Integration tests that actually spin up containers
- CI/CD -- do we need Docker-in-Docker?
- Testing across backends (Modal/E2B require real accounts)

### 4.5 Base Image Strategy

The spec mentions Alpine as a default for Docker. But:

- Alpine uses musl libc, which breaks some binaries
- What packages should be pre-installed?
- Do we maintain our own base images?
- What about architecture (arm64 vs amd64)?
- How does the text editor tool get into the image?

### 4.6 The `working_directory` Semantic

The spec says: *"If omitted, whatever this container considers its root dir (could be '~')"*

This is vague. Is it:
- The container's WORKDIR from the Dockerfile?
- The user's home directory?
- The filesystem root `/`?

This needs to be consistent across backends.

### 4.7 Mapped Working Directory (Future Feature)

This is noted as future but has architectural implications now:

- Docker volumes/bind mounts have permission issues (uid/gid mapping)
- Modal/E2B have their own file sync mechanisms
- If we design the backend API without considering this, retrofitting could be hard

**Decision needed:** Should the backend API include hooks for volume mapping even if we don't expose them in v1?

---

## 5. Things to Decide Now

These decisions have cascading impact and should be settled before writing the functional spec.

### 5.1 Tech Stack

**Python or Go?** This affects library choices, distribution model, contributor pool, and development speed. See analysis in 2.7.

### 5.2 Command Interface

**String, array, or both?** This affects every backend implementation, the tool description shown to LLMs, and how we handle escaping. See analysis in 2.1.

### 5.3 Output Limits and Truncation

**What's the max output size?** This needs to be defined in the interface contract, not left to backends. A 50MB stdout will crash LLM clients. Suggest: define a default limit (~512KB-1MB), allow override at startup, always include truncation metadata.

### 5.4 Timeout Policy

**What's the default exec timeout? Is there a sandbox-level timeout?** These need to be in the interface so backends implement them consistently.

### 5.5 Error Contract

**What does the exec response look like on infrastructure failures (not just command failures)?** Define the difference between "command returned exit code 1" and "sandbox crashed."

### 5.6 Backend Priority and Scope for v1

**Which backends are in v1?** Trying to ship Docker + Modal + E2B + WASI all at once is ambitious. Suggest: Docker as the primary backend for v1, with the abstraction designed to support others. This lets us validate the interface before committing to it across many backends.

### 5.7 File I/O Story

**How do files get in and out of the sandbox?** Even if the mapped working directory is "future," we need *some* answer for v1. Options:
- Base64 over exec (ugly but works for small files)
- Additional MCP tools (`upload`, `download`)
- Mapped volumes (Docker-specific, but pragmatic)
- Defer entirely (limits use cases significantly)

### 5.8 The "Sandbox Handle" Concept

The backend API returns a "sandbox handle" from `start`. What is this?

- A container ID? A UUID? An opaque string?
- Does the agent ever see it? (Sounds like no, since lifecycle is implicit.)
- If the agent never sees it, why is it in the backend API at all? For internal bookkeeping?

Clarify who uses handles and for what.

---

## Summary: Top 5 Priorities to Resolve

1. **Tech stack (Python vs Go)** -- blocks all implementation work
2. **V1 backend scope** -- Docker-first vs. multi-backend; determines how much abstraction is needed now
3. **File I/O story** -- `exec`-only is limiting; decide if v1 needs more tools
4. **Output limits + timeout policy** -- critical for reliability; must be in the interface contract
5. **Command interface (string vs array)** -- affects tool description, backend implementation, and agent experience

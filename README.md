<h1 align="center">MCP Sandbox Computer VM for AI</h1>
<h3 align="center">
  Named, manageable Linux computers for AI agents — via MCP
</h3>

<p align="center">
  <a href="https://github.com/flujo-app/mcp-sandbox-computer-vm-for-ai/actions/workflows/build_and_test.yml"><img src="https://github.com/flujo-app/mcp-sandbox-computer-vm-for-ai/actions/workflows/build_and_test.yml/badge.svg" alt="Build and Test"></a>
  <a href="https://pypi.org/project/mcp-sandbox-computer-vm-for-ai/"><img src="https://img.shields.io/pypi/v/mcp-sandbox-computer-vm-for-ai.svg?logo=pypi&label=PyPI&logoColor=gold" alt="PyPI"></a>
</p>

MCP Sandbox Computer VM for AI is a lifecycle-focused fork of [Kilntainers](https://github.com/Kiln-AI/Kilntainers). It gives agents isolated Linux computers, stable IDs, temporary or persistent lifecycles, an interactive MCP App dashboard, and first-class Docker and Fly Machines backends.

<!-- mcp-name: io.github.flujo-app/mcp-sandbox-computer-vm-for-ai -->

- 🖥️ **MCP App dashboard:** List computers, run commands, restart, factory reset, and delete from FLUJO or another stable MCP Apps host.
- 🏷️ **Named computers:** Reconnect with a stable `computer_id`, or omit it to receive a readable random slug.
- 💾 **Explicit lifecycle:** Temporary computers are removed with their MCP session; permanent computers survive and can be reattached later.
- 🧰 **Multiple backends:** Docker/Podman, native Fly Machines, [Modal](https://modal.com), [E2B](https://e2b.dev), and WebAssembly.
- 🏝️ **Isolated per agent:** Every agent gets its own dedicated sandbox — no shared state, no cross-contamination.
- 🔒 **Secure by design:** The agent communicates *with* the sandbox over MCP — it doesn’t run *inside* it. No agent API keys, code, or prompts are exposed to the sandbox.
- 🔌 **Tool and UI access:** `terminal_execute` stays simple, while optional provider-neutral lifecycle tools power both models and the dashboard.
- 📈 **Scalable:** Scale from a few agents on your laptop to thousands running in parallel in the cloud.

## Why sandbox computers?

Agents are already excellent at using terminals and can save thousands of tokens with common Linux utilities like `grep`, `find`, `jq`, and `awk`. Giving an agent access to the host OS is dangerous, while provisioning large numbers of isolated environments is operationally painful. MCP Sandbox Computer VM for AI gives every agent a dedicated sandbox with an explicit lifecycle.

## Quick Start

Run the released package directly from PyPI. Docker and stdio are the defaults:

```bash
uvx mcp-sandbox-computer-vm-for-ai
```

Add it to Claude Code:

```bash
claude mcp add --scope user sandbox-computer -- uvx mcp-sandbox-computer-vm-for-ai
```

Or add it to a JSON-based MCP client such as Claude Desktop:

```json
{
  "mcpServers": {
    "sandbox-computer": {
      "command": "uvx",
      "args": ["mcp-sandbox-computer-vm-for-ai"]
    }
  }
}
```

By default, the server exposes only `terminal_execute`. Set `ENABLE_LIFECYCLE_TOOLS=true` before starting the server to expose the `computer_*` tools and MCP App dashboard. For a JSON-based stdio client, add it to the server configuration:

```json
{
  "env": {
    "ENABLE_LIFECYCLE_TOOLS": "true"
  }
}
```

Then call `computer_dashboard` to open the App. The dashboard has no external browser dependencies. Its internal resource URI remains `ui://kilntainers/computers` for compatibility with the upstream implementation.

## Named computer lifecycle

`terminal_execute` accepts two additional optional inputs:

- `computer_id`: a 1–63 character lowercase slug. The first call without one creates a readable random ID and reuses it as that MCP session's default.
- `temporary`: defaults to `true`. Temporary computers are removed when the owning MCP session closes. Set it to `false` for a computer that survives server/session shutdown and can be reattached later by ID.

Every execution result includes `computer_id` and `temporary` next to stdout, stderr, exit code, and duration:

```json
{
  "computer_id": "steady-otter-a31f",
  "temporary": false,
  "stdout": "persistent\n",
  "stderr": "",
  "exit_code": 0,
  "exec_duration_ms": 84
}
```

Lifecycle tools are provider-neutral and are disabled unless `ENABLE_LIFECYCLE_TOOLS=true`:

| Tool | Purpose |
|---|---|
| `computer_dashboard` | Open the MCP App and return the current inventory |
| `computer_list` | List state, backend, image, provider ID, and lifecycle mode |
| `computer_create` | Create/attach by ID; omission always generates a new slug |
| `computer_restart` | Restart while preserving writable state |
| `computer_factory_reset` | Erase writable state and recreate from the base image |
| `computer_delete` | Permanently remove the computer |

## How It Works

```
┌─────────────┐   MCP   ┌──────────────┐      ┌─────────────────────────┐
│  LLM Agent  │◄───────►│  Sandbox MCP │◄────►│  Sandboxes              │
│  (client)   │         │  MCP Server  │      │  - Docker/Podman        │
│             │         │              │      │  - Cloud VM (Modal,E2B) │
│             │         │              │      │  - WASM Sandbox         │
└─────────────┘         └──────────────┘      └─────────────────────────┘
```

1. An MCP client starts MCP Sandbox Computer VM for AI over stdio or connects over HTTP
2. On the first `terminal_execute` call, the server creates a named isolated computer. Each connection gets its own random default unless it explicitly attaches by ID.
3. Commands run inside the sandbox; stdout, stderr, and exit code are returned
4. When the session ends, temporary computers are destroyed; permanent computers remain provider-side.

**Security:** The agent communicates *with* the sandbox over MCP — it doesn't run *inside* it. This is intentional: agents often need secrets (API keys, system prompts, code), and those should never be exposed inside a sandbox where a prompt injection could exfiltrate them.

**Agent Isolation & Sandbox Lifecycle:** An omitted ID gives each MCP connection an isolated default computer. Explicit IDs make reconnection intentional. Docker labels and Fly Machine metadata make permanent computers discoverable after the MCP server itself restarts.

## Backend Examples

See the [CLI Reference](#cli-reference) for all arguments.

### Docker and Podman (default)

Local containers via Docker or Podman. Any OCI image works.

```bash
uvx mcp-sandbox-computer-vm-for-ai                                # Docker + Debian (defaults)
uvx mcp-sandbox-computer-vm-for-ai --image alpine --engine podman # Podman + Alpine
uvx mcp-sandbox-computer-vm-for-ai --image node:22                # Node.js with networking
uvx mcp-sandbox-computer-vm-for-ai --no-network                   # Disable networking
```

### Docker Compose HTTP server

The included image contains the Docker CLI and talks to the host daemon through its socket:

```bash
docker compose up --build
# Streamable HTTP MCP endpoint: http://127.0.0.1:8080/mcp
```

Set `ENABLE_LIFECYCLE_TOOLS=true` in the Compose service environment when you want the optional dashboard and `computer_*` tools.

`compose.yaml` binds only to loopback. For a remote listener, set `KILNTAINERS_AUTH_TOKEN` and send it as an `Authorization: Bearer …` header. Mounting the Docker socket grants the service control of the host Docker daemon; use a dedicated host or a restricted remote daemon in production.

### Fly Machines

Fly.io deploys a Docker image as a VM root filesystem and does not run a nested Docker daemon. The `fly` backend therefore provisions real [Fly Machines](https://fly.io/docs/machines/) through `flyctl`: temporary Machines use disposable root filesystems, while permanent Machines use `persist_rootfs=always`.

```bash
fly apps create mcp-sandbox-computer-vm-for-ai

# Use an app-scoped token for Machine list/create/exec/destroy operations.
fly secrets set -a mcp-sandbox-computer-vm-for-ai \
  FLY_API_TOKEN="$(fly tokens create deploy -a mcp-sandbox-computer-vm-for-ai)" \
  KILNTAINERS_AUTH_TOKEN="$(openssl rand -hex 32)"

fly deploy
```

The MCP endpoint is `https://mcp-sandbox-computer-vm-for-ai.fly.dev/mcp`. Configure the same `KILNTAINERS_AUTH_TOKEN` as a bearer header in the MCP client. `fly.toml` keeps the controller Machine running because it owns MCP sessions and cleanup; sandbox Machines are standalone Machines distinguished by project metadata and are not part of the controller process group.

The included `fly.toml` defaults to Fly's São Paulo region (`gru`). Change `primary_region` and, when needed, `FLY_REGION` if you want the controller and newly created sandbox Machines in another supported region.

### Cloud Containers & VMs

#### Modal.com

Hosted containers with sub-second startup via [Modal.com](https://modal.com). Scales to thousands of parallel sandboxes. Supports GPUs.

```bash
uvx mcp-sandbox-computer-vm-for-ai --backend modal
uvx mcp-sandbox-computer-vm-for-ai --backend modal --gpu A10G --region us-east
```

Authenticate via `modal setup` CLI or `--modal-token-id` / `--modal-token-secret` flags.

#### E2B

Cloud hosted micro-VM sandboxes from [E2B](https://e2b.dev).

```bash
uvx mcp-sandbox-computer-vm-for-ai --backend e2b
uvx mcp-sandbox-computer-vm-for-ai --backend e2b --e2b-api-key ABCD --e2b-template my-custom-alpine
```

Authenticate with `--e2b-api-key` CLI arg, or `E2B_API_KEY` environment variable.

### WASM Go BusyBox (Experimental)

Runs [go-busybox](https://github.com/rcarmo/go-busybox) in a WebAssembly sandbox. Not a full Linux environment, but provides common utilities (`grep`, `awk`, `sed`, `ls`, `wc`, `sort`, etc.) in a very lightweight and secure sandbox.

```bash
uvx --from "mcp-sandbox-computer-vm-for-ai[wasm]" mcp-sandbox-computer-vm-for-ai --backend go_busybox
```

### WASM Runner

Run a custom WASM module as the sandbox backend. Provides agents a set tools compiled to WebAssembly, and an isolated filesystem.

```bash
uvx --from "mcp-sandbox-computer-vm-for-ai[wasm]" mcp-sandbox-computer-vm-for-ai --backend wasm --wasm-path ./my_tool.wasm
```

## Installation

```bash
uvx mcp-sandbox-computer-vm-for-ai                    # run without installing
uv tool install mcp-sandbox-computer-vm-for-ai        # recommended
uv tool install mcp-sandbox-computer-vm-for-ai[wasm]  # include WASM backends (+15MB)
pip install mcp-sandbox-computer-vm-for-ai            # also works with pip
```

Requires Python 3.13+. Docker backend requires Docker or Podman. The Modal and E2B backends require accounts to those services.

## Releasing

Node is used only as the cross-platform release task runner; the published package remains Python. The release command synchronizes all package and registry metadata.

```bash
npm run release:check                 # credential-free command self-check
npm run check                         # lint, types, tests, and package build
npm run release -- --dry-run          # full main-branch preflight, no changes
npm run release                       # patch version; GitHub publishes PyPI via OIDC
npm run release -- minor              # minor version release
npm run release -- 1.0.0              # exact version release
```

PyPI publication uses [Trusted Publishing](https://docs.pypi.org/trusted-publishers/), so no PyPI token is stored locally or in GitHub. Configure the PyPI publisher once with owner `flujo-app`, repository `mcp-sandbox-computer-vm-for-ai`, workflow `release.yml`, and environment `pypi`. The release command pushes the version commit and tag, dispatches `.github/workflows/release.yml`, and waits for PyPI and the GitHub Release.

After the PyPI version is visible, validate and publish its immutable metadata to the official MCP Registry:

```bash
npm run registry:validate             # downloads pinned publisher; publishes nothing
npm run registry:release              # GitHub login, then publish server.json
```

The registry command verifies the published PyPI README ownership marker before authenticating. `mcp:validate` and `mcp:publish` are retained as aliases matching the sibling MCP App repositories.

## CLI Reference

```
usage: mcp-sandbox-computer-vm-for-ai [-h] [--backend {docker,e2b,fly,go_busybox,modal,wasm}] [--transport {stdio,http}] [...]

MCP server providing isolated Linux sandboxes for LLM agent shell execution.

options:
  -h, --help            show this help message and exit

core options:
  --backend {docker,e2b,fly,go_busybox,modal,wasm}
                        Backend to use (default: docker)
  --transport {stdio,http}
                        MCP transport (default: stdio)
  --host HOST           HTTP bind address (default: 127.0.0.1, HTTP mode only)
  --port PORT           HTTP listen port (default: 8435, HTTP mode only)
  --timeout TIMEOUT     Default exec timeout in seconds (default: 120)
  --output-limit OUTPUT_LIMIT
                        Max combined stdout+stderr bytes per exec (default: 2097152 = 2 MiB)
  --session-timeout SESSION_TIMEOUT
                        Idle session timeout in seconds (default: 300, HTTP mode only)
  --auth-token AUTH_TOKEN
                        Bearer token for /mcp (default: KILNTAINERS_AUTH_TOKEN)
  --allow-unauthenticated-http
                        Explicitly allow a non-loopback listener without built-in auth
  --shell SHELL         Shell binary for command mode (e.g., /bin/bash, ash). Default: /bin/bash.
  --network, --no-network
                        Enable network access in sandboxes (default: enabled)

tool description:
  --tool-instruction-override TOOL_INSTRUCTION_OVERRIDE
                        Replace the entire terminal_execute tool description
  --extended-tool-instruction EXTENDED_TOOL_INSTRUCTION
                        Append to the backend's default tool description

docker backend options:
  --engine ENGINE       Container CLI binary (default: docker). Supports podman.
  --docker-host DOCKER_HOST
                        Docker daemon socket/address, passed as -H to the Docker CLI (e.g., "ssh://user@remote-host", "tcp://host:2375")
  --image IMAGE         Docker image (default: debian:bookworm-slim)
  --cpu CPU             Docker CPU limit (e.g., "1.5")
  --memory MEMORY       Docker memory limit (e.g., "512m")
  --docker-run-flag DOCKER_RUN_FLAGS
                        Additional flag passed to docker run. Repeatable. (e.g., --docker-run-flag "--pids-limit=256")

fly backend options:
  --fly-cli FLY_CLI     flyctl/fly executable (default: fly)
  --fly-app FLY_APP     Fly App that owns sandbox Machines (default: FLY_APP_NAME)
  --fly-token FLY_TOKEN Fly API token (default: FLY_API_TOKEN or FLY_TOKEN)
  --fly-image FLY_IMAGE Base OCI image for sandbox Machines
  --fly-region FLY_REGION
                        Region for newly created Machines
  --fly-cpu-kind {shared,performance}
  --fly-cpus FLY_CPUS
  --fly-memory FLY_MEMORY
                        Memory per Machine in MB
  --fly-rootfs-size FLY_ROOTFS_SIZE
                        Optional root filesystem size in GB

e2b backend options:
  --e2b-api-key E2B_API_KEY
                        E2B API key (overrides E2B_API_KEY environment variable)
  --e2b-template E2B_TEMPLATE
                        E2B template name or ID (default: base)
  --e2b-sandbox-timeout E2B_SANDBOX_TIMEOUT
                        Sandbox lifetime timeout in seconds (default: 3600)
  --e2b-metadata E2B_METADATA
                        Metadata key=value pairs (can be used multiple times)
  --e2b-env E2B_ENV     Environment variable key=value pairs (can be used multiple times)

modal backend options:
  --modal-token-id MODAL_TOKEN_ID
                        Modal token ID (overrides environment/default auth)
  --modal-token-secret MODAL_TOKEN_SECRET
                        Modal token secret (overrides environment/default auth)
  --modal-app-name MODAL_APP_NAME
                        Modal app name
  --modal-cpu MODAL_CPU
                        CPU cores (fractional, default: 1.0)
  --modal-memory MODAL_MEMORY
                        Memory in MiB (default: 512)
  --gpu GPU             GPU type (e.g., "A10G", "H100")
  --region REGION       Geographic region (e.g., "us-east")
  --sandbox-timeout SANDBOX_TIMEOUT
                        Sandbox lifetime timeout in seconds (default: 3600, max 86400)

wasm backend options:
  --wasm-path WASM_PATH
                        Path to the .wasm file to execute (required for wasm backend)
  --wasm-max-memory WASM_MAX_MEMORY
                        Max WASM memory in MiB (default: 256)
  --wasm-fuel WASM_FUEL
                        WASM instruction fuel limit (default: unlimited)
```

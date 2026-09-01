# MCP Virtual Computer

<!-- mcp-name: io.github.flujo-app/mcp-virtual-computer -->

A local, persistent Docker computer presented as a Three.js laptop on a desk. The MCP server keeps terminal automation from Kilntainers and adds UTF-8 file tools with a computer-screen view.

This first slice is Docker-only. It does not provision online or temporary machines.

## What it exposes

- `terminal_execute` — run a command in the configured Docker computer.
- `read_file` — read a UTF-8 text file relative to `/workspace` or by absolute path.
- `write_file` — atomically write a UTF-8 text file.
- `edit_file` — replace one exact text match, or all matches when requested.
- `computer_ui` — open or attach the Three.js computer view.

With `DESKTOP_ENVIRONMENT=true`, it additionally exposes:

- `look_at_screen` — return the current PNG framebuffer, AT-SPI snapshot, or both.
- `click`, `type`, and `scroll` — interact by AT-SPI element reference or coordinates.
- `list_windows` and `switch_window` — enumerate and activate real Xfce windows.
- `move_window`, `maximize_window`, `restore_window`, `minimize_window`, and `close_window` — control a selected window by ID, title, or class.
- `computer://screen/current.png` — current live framebuffer resource.
- `computer://screen/accessibility.json` — Playwright-like AT-SPI tree with element refs, roles, names, actions, focus state, and screen bounds.

The computer ID is server configuration, not a tool argument. Lifecycle tools and temporary-computer arguments are not exposed.

## Required configuration

```env
COMPUTER_ID=agent-workstation
DESKTOP_ENVIRONMENT=false
NETWORK_ACCESS=true
AUTO_INSTALL_DOCKER=true
```

`COMPUTER_ID` is required and selects the one persistent Docker computer. `DESKTOP_ENVIRONMENT` accepts `true`, `false`, `1`, `0`, `yes`, `no`, `on`, or `off`, and defaults to `false`. `NETWORK_ACCESS` accepts the same values and defaults to `true`. On Windows, `AUTO_INSTALL_DOCKER` accepts the same values and defaults to `true`.

### Lazy Docker setup on Windows

The MCP transport and App start without waiting for Docker. The first tool that needs the computer checks the default `docker` engine. When Docker is missing, the server uses WinGet's exact `Docker.DockerDesktop` package, requests Docker's recommended per-user install, accepts the Docker license for unattended startup, adds the discovered CLI directory to both the current process and the current user's `PATH`, starts Docker Desktop, and resumes the original tool call.

The Three.js screen shows the observed setup phase. While WinGet downloads Docker it says `Downloading Docker...`; when WinGet supplies byte totals, the bar and percentage use those real totals, otherwise the bar is explicitly indeterminate. Calls that include an MCP progress token also receive standard `notifications/progress`. Installation is shielded from client cancellation, so it continues if a client's roughly five-minute tool timeout expires; retrying the call waits for or uses the same setup task.

Set `AUTO_INSTALL_DOCKER=false` to require a preinstalled runtime. `DOCKER_INSTALL_TIMEOUT` defaults to 1200 seconds and `DOCKER_START_TIMEOUT` defaults to 240 seconds. A fresh PC can still require one elevated `wsl --install`/`wsl --update`, a Windows restart, or BIOS/UEFI virtualization; the App reports that condition instead of looping or claiming success.

These environment values are startup defaults. New default computers always use the bundled desktop-capable image, even when Xfce starts off. In the Three.js scene, click the LAN cable to really enable or disable the running container's outbound network; the container keeps its loopback-published noVNC transport and applies an outbound firewall inside its network namespace. Click the two-sided mug (`I <3 virtual desktops` / `I <3 real desktops`) to stop or start Xfce inside that same container. The container ID and its entire writable filesystem—not only `/workspace`—stay intact.

## The two screen modes

With `DESKTOP_ENVIRONMENT=false`, Xfce is stopped and the laptop shows a virtual desktop. The desktop services remain installed so Xfce can be enabled without recreating the computer. The screen stays idle until a genuine MCP invocation arrives, then renders Terminal, Files, or Text Editor actions from that invocation's actual arguments and result.

The Three.js laptop has a labeled QWERTY keyboard whose physical keys follow genuine `write_file` text, plus a wireless desk mouse that mirrors the on-screen pointer during real file navigation, selection, and reading.

The display is also directly controllable. Pointer input is raycast onto the angled 3D screen: double-click Files or Workspace, navigate real Docker folders, open text files, edit with the physical keyboard, and press Ctrl+S to save through `write_file`. Right-click inside Files to create a new text file; it opens unsaved in the editor until Ctrl+S is pressed. The red title-bar control closes the active virtual window. The virtual terminal executes typed commands on Enter. With the real desktop enabled, the same pointer and keyboard input is forwarded to the live VNC session. Filesystem and terminal interaction requires the App to be opened through an MCP host; the standalone static preview never invents directory contents.

With `DESKTOP_ENVIRONMENT=true`, the server builds the bundled Debian Bookworm/Xfce image on first use. The laptop then displays that container's real X11 framebuffer through noVNC. File tools drive real Thunar and Mousepad input through AT-SPI/Dogtail, `xdotool`, and `wmctrl`; the file operation itself remains deterministic and completes before the MCP result is returned. `terminal_execute` runs exactly once in a visible Xfce terminal, streams the same stdout and stderr to that window, and returns the captured exit status and output through MCP.

Sound from Xfce applications is routed through a 48 kHz stereo PulseAudio sink and streamed from the same loopback-only desktop endpoint into the dashboard. Browsers require a user gesture before playing audio, so click anywhere in the Three.js scene once after loading or refreshing it. Audio is sourced only from the real desktop; virtual mode does not synthesize sound.

Accessibility element references use paths such as `atspi:8/0/0/2`. They describe the current tree, so clients should call `look_at_screen` again after navigation or major window changes before reusing a reference.

The desktop video/input and audio WebSockets are published on `127.0.0.1` only.

## Run locally

Requirements:

- Python 3.13+
- `uv`
- Windows 10/11: WinGet (Docker Desktop is installed lazily when needed)
- Other platforms: Docker Engine or Docker Desktop with a running daemon

For stdio:

```bash
COMPUTER_ID=agent-workstation uv run mcp-virtual-computer
```

For streamable HTTP:

```bash
COMPUTER_ID=agent-workstation uv run mcp-virtual-computer \
  --transport http \
  --host 127.0.0.1 \
  --port 8080 \
  --allow-unauthenticated-http
```

Enable the real desktop:

```bash
COMPUTER_ID=agent-workstation DESKTOP_ENVIRONMENT=true \
  uv run mcp-virtual-computer
```

Or use Compose:

```bash
COMPUTER_ID=agent-workstation DESKTOP_ENVIRONMENT=true docker compose up --build
```

The Compose controller mounts `/var/run/docker.sock` so it can create and reattach the persistent workstation container.

## MCP client example

```json
{
  "mcpServers": {
    "virtual-computer": {
      "command": "uvx",
      "args": ["mcp-virtual-computer"],
      "env": {
        "COMPUTER_ID": "agent-workstation",
        "DESKTOP_ENVIRONMENT": "false",
        "NETWORK_ACCESS": "true",
        "AUTO_INSTALL_DOCKER": "true"
      }
    }
  }
}
```

## Build the MCP App

The bundled dashboard is generated from `src/virtual-computer` and packaged as `src/kilntainers/dashboard.html`.

```bash
npm install
npm run build:app
```

Opening `dashboard.html` directly shows an idle virtual desktop. MCP-hosted operation visuals begin only when the host supplies a real tool event.

## File semantics

- Paths without a leading slash resolve below `/workspace`.
- File content must be valid UTF-8 text.
- Reads and writes default to a 1 MiB text limit.
- Writes use a temporary file plus rename.
- Edits require an exact match and reject ambiguous single replacements.
- Internal SHA-256 checks prevent stale writes during edits.

## Development checks

```bash
npm run build:app
uv run pytest src/kilntainers/test_file_tools.py \
  src/kilntainers/test_server.py \
  src/kilntainers/test_cli.py \
  src/kilntainers/test_config.py \
  src/kilntainers/test_dashboard.py \
  src/kilntainers/backends/test_virtual_docker.py
uv run ruff check src/kilntainers
uv run ty check src/kilntainers
```

The real desktop image needs a working Docker daemon for an end-to-end build and framebuffer test.

## License and origin

MIT licensed. This project is a Docker-only fork of [Kilntainers](https://github.com/Kiln-AI/Kilntainers).

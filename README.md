# MCP Virtual Computer

<!-- mcp-name: io.github.flujo-app/mcp-virtual-computer -->

<img width="1183" height="827" alt="image" src="https://github.com/user-attachments/assets/beb3c5ed-4b27-4217-abb4-8607a2cc626e" />

A VM for your agent. And for you.
- click the mug to switch between a virtual desktop or a real one
- unplug the network cable and cut network access
- use the computer screen like a normal screen
- copy and paste text between the host browser and the Xfce desktop
- model can use terminal, create/read/edit files, see the screen, click, type
- little fun thing: if either you or the llm types text, you see that on the keyboard; if one uses the cursor, you see the mouse move on the table.
- auto installs docker and all prerequresits (hopefully)

## MCP client example

```json
{
  "mcpServers": {
    "virtual-computer": {
      "command": "uvx",
      "args": ["mcp-virtual-computer"],
      "env": {
        "COMPUTER_ID": "agent-workstation",
        "DESKTOP_ENVIRONMENT": "true",
        "NETWORK_ACCESS": "true",
        "AUTO_INSTALL_DOCKER": "true",
        "EXPOSE_LIFECYCLE_TOOLS": "false"
      }
    }
  }
}
```
<img width="807" height="787" alt="image" src="https://github.com/user-attachments/assets/1a8fe86f-fab4-49aa-8740-2e06044344af" />

## What it exposes

- `terminal_execute` — run a command in the configured Docker computer.
- `read_file` — read a UTF-8 text file relative to `/workspace` or by absolute path.
- `write_file` — atomically write a UTF-8 text file.
- `edit_file` — replace one exact text match, or all matches when requested.
- `computer_ui` — open or attach the Three.js computer view. Its result includes
  both the MCP App `resource_uri` and a real loopback `url`/`dashboard_url` that
  opens in an external browser, including for stdio clients.

`DESKTOP_ENVIRONMENT` defaults to `true`. With the real desktop enabled it
additionally exposes:

- `look_at_screen` — return the current PNG framebuffer, AT-SPI snapshot, or both.
- `click`, `type`, and `scroll` — interact by AT-SPI element reference or coordinates.
- `list_windows` and `switch_window` — enumerate and activate real Xfce windows.
- `move_window`, `maximize_window`, `restore_window`, `minimize_window`, and `close_window` — control a selected window by ID, title, or class.
- `computer://screen/current.png` — current live framebuffer resource.
- `computer://screen/accessibility.json` — Playwright-like AT-SPI tree with element refs, roles, names, actions, focus state, and screen bounds.

The MCP App can always call `runtime_status`, `set_network_access`, and `set_desktop_environment`. Set `EXPOSE_LIFECYCLE_TOOLS=true` to additionally expose those lifecycle controls to the model; they remain model-hidden by default.
  
## License and origin

MIT licensed. This project is a Docker-only fork of [Kilntainers](https://github.com/Kiln-AI/Kilntainers).

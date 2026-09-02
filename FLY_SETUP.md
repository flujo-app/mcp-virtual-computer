# Fly.io setup

Fly mode runs the real Xfce desktop on one permanent Fly Machine. Docker is not required. The server automatically downloads `flyctl`, chooses an organization, creates and remembers a Fly App, lets Fly choose placement, and creates a shared-CPU Machine with 1 GB RAM.

## MCP client configuration

Add this server entry to your MCP client:

```json
{
  "mcpServers": {
    "virtual-computer-fly": {
      "command": "uvx",
      "args": ["mcp-virtual-computer", "--backend", "fly"],
      "env": {
        "COMPUTER_ID": "agent-workstation",
        "DESKTOP_ENVIRONMENT": "true",
        "NETWORK_ACCESS": "true",
        "AUTO_INSTALL_FLYCTL": "true",
        "EXPOSE_LIFECYCLE_TOOLS": "false"
      }
    }
  }
}
```

The repository [`.mcp.json`](.mcp.json) runs the local checkout instead of the published package. [`server.json`](server.json) contains the MCP Registry package declaration.

## Authenticate once

Authentication is the only required account input. Use either of these methods:

1. Start or restart the MCP server and call `computer_ui`. If `flyctl` is absent, the server downloads it into `~/.fly/bin` without administrator access. The first unauthenticated call reports the exact installed executable path.
2. Run the reported `<path-to-flyctl> auth login` command once, finish the browser login, and restart the MCP client.

Alternatively, set `FLY_API_TOKEN` in the MCP client's secret environment. Do not put a real token into a committed `.mcp.json` or `server.json` file.

## First start

Call `computer_ui`. The first call can take several minutes while Fly builds the bundled Xfce image and creates the Machine. Later MCP sessions attach to the Machine identified by `COMPUTER_ID`.

The returned dashboard URL is local to the MCP server. It proxies the token-protected Fly noVNC and audio WebSockets so the upstream secret URL is not exposed to the browser. `look_at_screen`, `click`, `type`, and the other desktop tools operate on the same Xfce session.

The Machine and its writable root filesystem are permanent. It keeps running when the MCP client exits and can incur Fly usage. Stop it in Fly when you do not need the desktop, or delete/factory-reset the computer when you intentionally want to remove its persistent filesystem.

Optional overrides are available through `FLY_ORG`, `FLY_APP_NAME`, `FLY_REGION`, `FLY_API_TOKEN`, and the `--fly-*` CLI flags. Set `AUTO_INSTALL_FLYCTL=false` to require a preinstalled CLI.

"""Packaged Three.js MCP App resource for the virtual computer."""

from importlib.resources import files
from typing import Any

DASHBOARD_URI = "ui://virtual-computer/computer.html"
DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"
_DASHBOARD_CONNECT_DOMAINS = [
    "ws://127.0.0.1:*",
    "http://127.0.0.1:*",
]
DASHBOARD_RESOURCE_META: dict[str, Any] = {
    "ui": {
        "csp": {
            "connectDomains": _DASHBOARD_CONNECT_DOMAINS,
        },
        "permissions": {"clipboardWrite": {}},
        "prefersBorder": False,
    },
    # Compatibility aliases for ChatGPT hosts predating the stable MCP Apps keys.
    "openai/widgetCSP": {
        "connect_domains": _DASHBOARD_CONNECT_DOMAINS,
        "resource_domains": [],
    },
    "openai/widgetPrefersBorder": False,
}


def dashboard_html() -> str:
    """Load the dependency-free dashboard embedded in the Python wheel."""
    return files("kilntainers").joinpath("dashboard.html").read_text(encoding="utf-8")

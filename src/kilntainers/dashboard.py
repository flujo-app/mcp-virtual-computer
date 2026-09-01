"""Packaged Three.js MCP App resource for the virtual computer."""

from importlib.resources import files
from typing import Any

DASHBOARD_URI = "ui://virtual-computer/computer.html"
DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"
DASHBOARD_RESOURCE_META: dict[str, Any] = {
    "ui": {
        "csp": {
            "connectDomains": [
                "ws://127.0.0.1:*",
                "http://127.0.0.1:*",
            ]
        },
        "prefersBorder": False,
    }
}


def dashboard_html() -> str:
    """Load the dependency-free dashboard embedded in the Python wheel."""
    return files("kilntainers").joinpath("dashboard.html").read_text(encoding="utf-8")

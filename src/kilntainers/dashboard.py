"""Packaged MCP App resource for sandbox computer management."""

from importlib.resources import files
from typing import Any

DASHBOARD_URI = "ui://kilntainers/computers"
DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"
DASHBOARD_RESOURCE_META: dict[str, Any] = {
    "ui": {
        "csp": {},
        "prefersBorder": True,
    }
}


def dashboard_html() -> str:
    """Load the dependency-free dashboard embedded in the Python wheel."""
    return files("kilntainers").joinpath("dashboard.html").read_text(encoding="utf-8")

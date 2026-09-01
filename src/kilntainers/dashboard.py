"""Packaged Three.js MCP App resource for the virtual computer."""

from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

DASHBOARD_URI = "ui://virtual-computer/computer.html"
DASHBOARD_MIME_TYPE = "text/html;profile=mcp-app"
_DASHBOARD_CONNECT_DOMAINS = [
    "ws://127.0.0.1:*",
    "http://127.0.0.1:*",
]


def dashboard_resource_meta(*connect_urls: str | None) -> dict[str, Any]:
    """Build resource metadata with exact origins for strict MCP App hosts.

    Wildcard loopback ports are valid CSP host sources and remain useful for
    hosts such as Claude. Goose currently filters wildcard ports, so the live
    desktop and standalone dashboard origins are also included explicitly.
    """
    connect_domains = list(_DASHBOARD_CONNECT_DOMAINS)
    for value in connect_urls:
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https", "ws", "wss"}:
            continue
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            continue
        if not parsed.netloc:
            continue
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in connect_domains:
            connect_domains.append(origin)

    return {
        "ui": {
            "csp": {
                "connectDomains": connect_domains,
            },
            "permissions": {"clipboardWrite": {}},
            "prefersBorder": False,
        },
        # Compatibility aliases for ChatGPT hosts predating the stable MCP Apps keys.
        "openai/widgetCSP": {
            "connect_domains": connect_domains,
            "resource_domains": [],
        },
        "openai/widgetPrefersBorder": False,
    }


DASHBOARD_RESOURCE_META: dict[str, Any] = dashboard_resource_meta()


def dashboard_html() -> str:
    """Load the dependency-free dashboard embedded in the Python wheel."""
    return files("kilntainers").joinpath("dashboard.html").read_text(encoding="utf-8")

"""Three.js MCP App registration and static auth tests."""

from mcp import Client
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from kilntainers.auth import BearerTokenMiddleware
from kilntainers.backends.test_utils import MockBackend
from kilntainers.config import BackendConfig, ServerConfig
from kilntainers.dashboard import (
    DASHBOARD_MIME_TYPE,
    DASHBOARD_RESOURCE_META,
    DASHBOARD_URI,
    dashboard_resource_meta,
)
from kilntainers.server import create_server


async def test_virtual_computer_tools_resource_and_extension_are_registered() -> None:
    server = create_server(
        MockBackend(BackendConfig()),
        ServerConfig(desktop_environment=False),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}
    resources = await server.list_resources()

    assert set(tools) == {
        "terminal_execute",
        "computer_ui",
        "list_directory",
        "read_file",
        "write_file",
        "edit_file",
        "runtime_status",
        "set_network_access",
        "set_desktop_environment",
    }
    assert tools["computer_ui"].meta == {
        "ui": {
            "visibility": ["model", "app"],
            "resourceUri": DASHBOARD_URI,
        },
        "openai/outputTemplate": DASHBOARD_URI,
        "openai/widgetAccessible": True,
    }
    for name, tool in tools.items():
        if name == "computer_ui":
            continue
        assert tool.meta is None or "openai/outputTemplate" not in tool.meta
        assert not (tool.meta or {}).get("ui", {}).get("resourceUri")
    assert tools["list_directory"].meta == {"ui": {"visibility": ["app"]}}
    for name in ("runtime_status", "set_network_access", "set_desktop_environment"):
        assert tools[name].meta == {"ui": {"visibility": ["app"]}}
    assert len(resources) == 1
    assert str(resources[0].uri) == DASHBOARD_URI
    assert resources[0].mime_type == DASHBOARD_MIME_TYPE
    resource_meta = resources[0].meta
    assert resource_meta is not None
    assert "http://127.0.0.1:8435" in resource_meta["ui"]["csp"]["connectDomains"]
    assert "ws://127.0.0.1:8435" in resource_meta["ui"]["csp"]["connectDomains"]

    async with Client(server) as client:
        extensions = client.server_capabilities.extensions
    assert extensions == {"io.modelcontextprotocol/ui": {}}
    assert DASHBOARD_RESOURCE_META["ui"]["permissions"] == {"clipboardWrite": {}}
    assert DASHBOARD_RESOURCE_META["openai/widgetCSP"]["connect_domains"] == [
        "ws://127.0.0.1:*",
        "http://127.0.0.1:*",
    ]


async def test_virtual_computer_html_is_self_contained() -> None:
    server = create_server(MockBackend(BackendConfig()), ServerConfig())
    contents = await server.read_resource(DASHBOARD_URI)
    html = next(iter(contents)).content

    assert isinstance(html, str)
    assert "Virtual Computer" in html
    assert "Interactive 3D laptop on a desk" in html
    assert "Request Received" not in html
    assert "No operations will be simulated" not in html
    assert '/websockify$/,"/audio"' in html
    assert "AudioContext" in html
    assert "clipboardPasteFrom" in html
    assert 'addEventListener("clipboard"' in html
    assert "navigator.clipboard.readText" in html
    assert "navigator.clipboard.writeText" in html
    assert '"runtime_status"' in html
    assert "window.parent!==window" in html
    assert "<script src=" not in html
    assert "<link rel=" not in html


async def test_lifecycle_management_tools_are_app_only_by_default() -> None:
    server = create_server(
        MockBackend(BackendConfig()),
        ServerConfig(desktop_environment=False),
    )

    tools = {tool.name: tool for tool in await server.list_tools()}
    resources = await server.list_resources()
    async with Client(server) as client:
        extensions = client.server_capabilities.extensions

    for name in ("runtime_status", "set_network_access", "set_desktop_environment"):
        assert tools[name].meta == {"ui": {"visibility": ["app"]}}
    assert len(resources) == 1
    assert extensions == {"io.modelcontextprotocol/ui": {}}


def test_dashboard_resource_meta_adds_exact_loopback_origins() -> None:
    meta = dashboard_resource_meta(
        "http://127.0.0.1:43123/dashboard.html",
        "ws://127.0.0.1:55779/websockify",
    )

    assert meta["ui"]["csp"]["connectDomains"][-2:] == [
        "http://127.0.0.1:43123",
        "ws://127.0.0.1:55779",
    ]


async def test_lifecycle_tools_add_model_visibility_when_enabled() -> None:
    server = create_server(
        MockBackend(BackendConfig()),
        ServerConfig(expose_lifecycle_tools=True),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert {"runtime_status", "set_network_access", "set_desktop_environment"} <= set(
        tools
    )
    expected = {"ui": {"visibility": ["model", "app"]}}
    assert tools["set_network_access"].meta == expected
    assert tools["set_desktop_environment"].meta == expected
    assert tools["runtime_status"].meta == expected


async def test_only_computer_ui_exposes_the_mcp_app_in_desktop_mode() -> None:
    server = create_server(
        MockBackend(BackendConfig()),
        ServerConfig(
            desktop_environment=True,
            expose_lifecycle_tools=True,
        ),
    )
    tools = {tool.name: tool for tool in await server.list_tools()}

    app_tools = {
        name
        for name, tool in tools.items()
        if (tool.meta or {}).get("openai/outputTemplate") == DASHBOARD_URI
        or (tool.meta or {}).get("ui", {}).get("resourceUri") == DASHBOARD_URI
    }

    assert app_tools == {"computer_ui"}


async def _ok(request):
    return JSONResponse({"ok": True})


def test_bearer_middleware_protects_sensitive_routes() -> None:
    app = Starlette(
        routes=[
            Route("/", _ok),
            Route("/healthz", _ok),
            Route("/mcp", _ok, methods=["GET", "POST"]),
        ]
    )
    app.add_middleware(
        BearerTokenMiddleware,  # ty: ignore[invalid-argument-type]
        token="test-secret",
    )

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/mcp").status_code == 401
        assert (
            client.get("/mcp", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        assert (
            client.get(
                "/mcp", headers={"Authorization": "Bearer test-secret"}
            ).status_code
            == 200
        )

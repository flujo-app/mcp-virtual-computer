"""Three.js MCP App registration and static auth tests."""

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from kilntainers.auth import BearerTokenMiddleware
from kilntainers.backends.test_utils import MockBackend
from kilntainers.config import BackendConfig, ServerConfig
from kilntainers.dashboard import DASHBOARD_MIME_TYPE, DASHBOARD_URI
from kilntainers.server import create_server


def test_virtual_computer_tools_resource_and_extension_are_registered() -> None:
    server = create_server(MockBackend(BackendConfig()), ServerConfig())
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}
    resources = server._resource_manager.list_resources()

    assert set(tools) == {
        "terminal_execute",
        "computer_ui",
        "list_directory",
        "read_file",
        "write_file",
        "edit_file",
    }
    assert tools["computer_ui"].meta == {
        "ui": {
            "visibility": ["model", "app"],
            "resourceUri": DASHBOARD_URI,
        },
        "openai/outputTemplate": DASHBOARD_URI,
    }
    for name, tool in tools.items():
        if name == "computer_ui":
            continue
        assert tool.meta is None or "openai/outputTemplate" not in tool.meta
        assert not (tool.meta or {}).get("ui", {}).get("resourceUri")
    assert tools["list_directory"].meta == {"ui": {"visibility": ["app"]}}
    assert len(resources) == 1
    assert str(resources[0].uri) == DASHBOARD_URI
    assert resources[0].mime_type == DASHBOARD_MIME_TYPE

    capabilities = server._mcp_server.create_initialization_options().capabilities
    payload = capabilities.model_dump(by_alias=True, exclude_none=True)
    assert payload["extensions"] == {
        "io.modelcontextprotocol/ui": {"mimeTypes": [DASHBOARD_MIME_TYPE]}
    }


async def test_virtual_computer_html_is_self_contained() -> None:
    server = create_server(MockBackend(BackendConfig()), ServerConfig())
    resource = server._resource_manager.list_resources()[0]
    html = await resource.read()

    assert isinstance(html, str)
    assert "Virtual Computer" in html
    assert "Interactive 3D laptop on a desk" in html
    assert "Request Received" not in html
    assert "No operations will be simulated" not in html
    assert 'pathname="/audio"' in html
    assert "AudioContext" in html
    assert "<script src=" not in html
    assert "<link rel=" not in html


def test_lifecycle_management_tools_are_not_exposed() -> None:
    server = create_server(MockBackend(BackendConfig()), ServerConfig())

    tools = {tool.name for tool in server._tool_manager.list_tools()}
    resources = server._resource_manager.list_resources()
    capabilities = server._mcp_server.create_initialization_options().capabilities
    payload = capabilities.model_dump(by_alias=True, exclude_none=True)

    assert not any(name.startswith("computer_") and name != "computer_ui" for name in tools)
    assert len(resources) == 1
    assert payload["extensions"] == {
        "io.modelcontextprotocol/ui": {"mimeTypes": [DASHBOARD_MIME_TYPE]}
    }


def test_lifecycle_tools_are_exposed_only_when_enabled() -> None:
    server = create_server(
        MockBackend(BackendConfig()),
        ServerConfig(expose_lifecycle_tools=True),
    )
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    assert {"runtime_status", "set_network_access", "set_desktop_environment"} <= set(
        tools
    )
    assert tools["set_network_access"].meta == {"ui": {"visibility": ["app"]}}
    assert tools["set_desktop_environment"].meta == {"ui": {"visibility": ["app"]}}
    assert tools["runtime_status"].meta == {"ui": {"visibility": ["app"]}}


def test_only_computer_ui_exposes_the_mcp_app_in_desktop_mode() -> None:
    server = create_server(
        MockBackend(BackendConfig()),
        ServerConfig(
            desktop_environment=True,
            expose_lifecycle_tools=True,
        ),
    )
    tools = {tool.name: tool for tool in server._tool_manager.list_tools()}

    app_tools = {
        name
        for name, tool in tools.items()
        if (tool.meta or {}).get("openai/outputTemplate") == DASHBOARD_URI
        or (tool.meta or {}).get("ui", {}).get("resourceUri") == DASHBOARD_URI
    }

    assert app_tools == {"computer_ui"}


async def _ok(request):
    return JSONResponse({"ok": True})


def test_bearer_middleware_protects_only_mcp_route() -> None:
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

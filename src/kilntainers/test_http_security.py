"""Real ASGI security checks across the CLI wrapper and MCP SDK transports."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kilntainers.backends.test_utils import MockBackend
from kilntainers.cli import _protected_http_app
from kilntainers.config import BackendConfig, ServerConfig
from kilntainers.server import create_server

_SERVER_TOKEN = "integration-server-token"
_BEARER = {"Authorization": f"Bearer {_SERVER_TOKEN}"}
_MODERN_VERSION = "2026-07-28"
_LEGACY_VERSION = "2025-11-25"


@contextmanager
def protected_client(
    config: ServerConfig,
) -> Iterator[tuple[TestClient, MockBackend]]:
    backend = MockBackend(BackendConfig())
    server = create_server(backend, config)
    app = _protected_http_app(server, config)
    with TestClient(
        app,
        base_url=f"http://127.0.0.1:{config.port}",
        client=("127.0.0.1", 53000),
    ) as client:
        yield client, backend


def modern_rpc(
    client: TestClient,
    method: str = "server/discover",
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    parameters = {
        **(params or {}),
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": _MODERN_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }
    return client.post(
        "/mcp",
        headers={
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _MODERN_VERSION,
            "Mcp-Method": method,
            **(headers or {}),
        },
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": parameters},
    )


def rpc_payload(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    events = [
        json.loads(line.removeprefix("data:").strip())
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]
    return next(event for event in events if event.get("id") == 1)


@pytest.mark.parametrize("path", ["/activity", "/dashboard.html", "/"])
def test_real_companion_routes_require_auth_and_disable_caching(path: str) -> None:
    config = ServerConfig(
        transport="http", auth_token=_SERVER_TOKEN, desktop_environment=False
    )
    with protected_client(config) as (client, backend):
        assert client.get(path).status_code == 401
        assert (
            client.get(path, headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )
        response = client.get(path, headers=_BEARER)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert config.companion_access.token not in response.text
        assert backend.create_count == 0


def test_real_health_probe_is_public_without_provisioning_a_computer() -> None:
    config = ServerConfig(transport="http", auth_token=_SERVER_TOKEN)
    with protected_client(config) as (client, backend):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert client.post("/healthz").status_code == 401
        assert backend.create_count == 0


def test_standalone_header_capability_supports_modern_discovery_and_tools() -> None:
    config = ServerConfig(transport="stdio", desktop_environment=False)
    headers = {
        "X-Computer-Access": config.companion_access.token,
        "Origin": f"http://127.0.0.1:{config.port}",
    }
    with protected_client(config) as (client, backend):
        assert modern_rpc(client).status_code == 401
        response = modern_rpc(client, headers=headers)
        discovery = rpc_payload(response)
        assert discovery["result"]["resultType"] == "complete"
        assert "Mcp-Session-Id" not in response.headers
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        tools = rpc_payload(modern_rpc(client, "tools/list", headers=headers))
        assert tools["result"]["resultType"] == "complete"
        assert "ttlMs" in tools["result"]
        assert "cacheScope" in tools["result"]
        assert "terminal_execute" in {tool["name"] for tool in tools["result"]["tools"]}
        assert backend.create_count == 0


def test_authenticated_http_retains_legacy_handshake_and_tool_listing() -> None:
    config = ServerConfig(
        transport="http", auth_token=_SERVER_TOKEN, desktop_environment=False
    )
    with protected_client(config) as (client, backend):
        headers = {
            **_BEARER,
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": _LEGACY_VERSION,
        }
        initialized = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": _LEGACY_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "security-test", "version": "1.0"},
                },
            },
        )
        payload = rpc_payload(initialized)
        assert payload["result"]["protocolVersion"] == _LEGACY_VERSION
        if session_id := initialized.headers.get("Mcp-Session-Id"):
            headers["Mcp-Session-Id"] = session_id
        notification = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        assert 200 <= notification.status_code < 300, notification.text
        listed = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert "terminal_execute" in {
            tool["name"] for tool in rpc_payload(listed)["result"]["tools"]
        }
        assert backend.create_count == 0


@pytest.mark.parametrize(
    "origin", ["https://attacker.example", "http://127.0.0.1:9999"]
)
def test_nonopaque_hostile_origin_is_rejected_even_with_capability(origin: str) -> None:
    config = ServerConfig(transport="stdio", desktop_environment=False)
    headers = {
        "X-Computer-Access": config.companion_access.token,
        "Origin": origin,
    }
    with protected_client(config) as (client, backend):
        for path in ("/activity", "/dashboard.html", "/"):
            assert client.get(path, headers=headers).status_code == 403
        assert modern_rpc(client, headers=headers).status_code == 403
        assert backend.create_count == 0


def test_mcp_sdk_rejects_opaque_origin_despite_valid_companion_capability() -> None:
    config = ServerConfig(transport="stdio", desktop_environment=False)
    with protected_client(config) as (client, backend):
        response = modern_rpc(
            client,
            headers={
                "X-Computer-Access": config.companion_access.token,
                "Origin": "null",
            },
        )
        assert response.status_code == 403
        assert backend.create_count == 0


@pytest.mark.parametrize("credential", ["bearer", "capability"])
def test_mcp_sdk_rejects_unconfigured_host_even_with_credentials(
    credential: str,
) -> None:
    config = ServerConfig(transport="http", auth_token=_SERVER_TOKEN)
    authorization = (
        _BEARER
        if credential == "bearer"
        else {"X-Computer-Access": config.companion_access.token}
    )
    with protected_client(config) as (client, backend):
        response = modern_rpc(
            client, headers={**authorization, "Host": "attacker.example"}
        )
        assert response.status_code == 421
        assert backend.create_count == 0


def test_anonymous_loopback_http_cannot_bypass_host_or_companion_auth() -> None:
    config = ServerConfig(transport="http", desktop_environment=False)
    with protected_client(config) as (client, backend):
        assert "result" in rpc_payload(modern_rpc(client))
        assert (
            modern_rpc(client, headers={"Host": "attacker.example"}).status_code == 401
        )
        assert client.get("/activity").status_code == 401
        assert client.get("/dashboard.html").status_code == 401
        assert backend.create_count == 0


def test_authorized_requests_still_enforce_mcp_method_header_consistency() -> None:
    config = ServerConfig(transport="http", auth_token=_SERVER_TOKEN)
    with protected_client(config) as (client, backend):
        response = modern_rpc(client, headers={**_BEARER, "Mcp-Method": "tools/list"})
        assert response.status_code == 400
        assert backend.create_count == 0


@pytest.mark.parametrize("origin", [None, "null", "http://127.0.0.1:8435"])
def test_real_desktop_websocket_rejects_missing_capability(origin: str | None) -> None:
    config = ServerConfig(transport="stdio", desktop_environment=False)
    headers = {} if origin is None else {"Origin": origin}
    with protected_client(config) as (client, backend):
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/desktop/websockify", headers=headers):
                pytest.fail("Unauthenticated desktop route was accepted")
        assert denied.value.code == 1008
        assert backend.create_count == 0


def test_real_desktop_rejects_hostile_origin_even_with_capability() -> None:
    config = ServerConfig(transport="stdio", desktop_environment=False)
    with protected_client(config) as (client, backend):
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                config.companion_access.url("/desktop/audio"),
                headers={"Origin": "https://attacker.example"},
            ):
                pytest.fail("Cross-site desktop route was accepted")
        assert denied.value.code == 1008
        assert backend.create_count == 0


def test_opaque_app_frame_passes_auth_without_provisioning_a_desktop() -> None:
    config = ServerConfig(transport="stdio", desktop_environment=False)
    with protected_client(config) as (client, backend):
        with pytest.raises(WebSocketDisconnect) as unavailable:
            with client.websocket_connect(
                config.companion_access.url("/desktop/websockify"),
                headers={"Origin": "null"},
            ):
                pytest.fail("No desktop should be running during this check")
        # Auth succeeded; the real route reports the absent desktop separately.
        assert unavailable.value.code == 1013
        assert backend.create_count == 0


def test_default_http_port_accepts_browser_serialized_origin_and_host() -> None:
    config = ServerConfig(
        transport="http",
        port=80,
        auth_token=_SERVER_TOKEN,
        desktop_environment=False,
    )
    with protected_client(config) as (client, backend):
        response = modern_rpc(
            client,
            headers={**_BEARER, "Origin": "http://127.0.0.1"},
        )
        assert "result" in rpc_payload(response)
        assert backend.create_count == 0


@pytest.mark.parametrize(
    ("configured_origin", "browser_origin"),
    [
        ("https://trusted.example:443", "https://trusted.example"),
        ("http://trusted.example:80", "http://trusted.example"),
    ],
)
def test_configured_default_port_origins_accept_browser_serialization(
    configured_origin: str,
    browser_origin: str,
) -> None:
    config = ServerConfig(
        transport="http",
        auth_token=_SERVER_TOKEN,
        desktop_environment=False,
        allowed_http_origins=(configured_origin,),
    )
    with protected_client(config) as (client, backend):
        response = modern_rpc(
            client,
            headers={**_BEARER, "Origin": browser_origin},
        )
        assert "result" in rpc_payload(response)
        assert backend.create_count == 0

"""Regression coverage for companion disclosure and cross-site desktop access."""

from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kilntainers.auth import BearerTokenMiddleware, CompanionAccess

BEARER = {"Authorization": "Bearer test-server-token"}
TRUSTED_ORIGIN = {"Origin": "http://testserver"}


def client_for(
    *,
    access: CompanionAccess | None = None,
    allow_mcp_capability: bool = False,
    allow_unauthenticated_mcp: bool = False,
    allow_opaque_origin: bool = True,
) -> TestClient:
    async def sensitive_http(request):
        return JSONResponse({"sensitive": "terminal output and file contents"})

    async def sensitive_socket(websocket):
        await websocket.accept()
        await websocket.send_text("desktop stream")
        await websocket.close()

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                sensitive_http,
                methods=["GET", "POST", "HEAD", "OPTIONS"],
            ),
            WebSocketRoute("/desktop/{channel}", sensitive_socket),
        ]
    )
    protected_app = BearerTokenMiddleware(
        app,
        token="test-server-token",
        companion_access=access,
        allowed_origins=["http://testserver", "https://trusted.example"],
        allow_mcp_capability=allow_mcp_capability,
        allow_unauthenticated_mcp=allow_unauthenticated_mcp,
        allow_opaque_origin=allow_opaque_origin,
    )
    return TestClient(protected_app)


@pytest.mark.parametrize(
    "path", ["/activity", "/dashboard.html", "/", "/mcp", "/private-future-route"]
)
def test_sensitive_routes_require_credentials(path: str) -> None:
    with client_for() as client:
        denied = client.get(path)
        assert denied.status_code == 401
        assert "sensitive" not in denied.text
        assert denied.headers["WWW-Authenticate"] == "Bearer"
        assert client.get(path, headers=BEARER).status_code == 200


def test_only_read_only_health_checks_are_public() -> None:
    with client_for() as client:
        assert client.get("/healthz").status_code == 200
        assert client.head("/healthz").status_code == 200
        assert client.post("/healthz").status_code == 401
        assert client.get("/healthz/other").status_code == 401


def test_authenticated_responses_cannot_cache_or_leak_capability_as_referrer() -> None:
    access = CompanionAccess.generate()
    with client_for(access=access) as client:
        response = client.get(access.url("/dashboard.html"))
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("path", ["/", "/activity", "/dashboard.html"])
def test_browser_capability_can_read_companion_routes(path: str) -> None:
    access = CompanionAccess.generate()
    with client_for(access=access) as client:
        assert client.get(access.url(path)).status_code == 200
        assert (
            client.get(path, headers={"X-Computer-Access": access.token}).status_code
            == 200
        )


@pytest.mark.parametrize("path", ["/mcp", "/mcp/tools", "/mcp-other", "/private"])
def test_browser_capability_does_not_grant_mcp_or_unrelated_access(path: str) -> None:
    access = CompanionAccess.generate()
    with client_for(access=access) as client:
        assert client.post(access.url(path)).status_code == 401


def test_standalone_dashboard_mcp_capability_requires_explicit_opt_in() -> None:
    access = CompanionAccess.generate()
    with client_for(access=access, allow_mcp_capability=True) as client:
        assert (
            client.post(
                "/mcp",
                headers={**TRUSTED_ORIGIN, "X-Computer-Access": access.token},
            ).status_code
            == 200
        )
        assert client.post(access.url("/mcp-other")).status_code == 401


@pytest.mark.parametrize(
    "origin", ["https://attacker.example", "null", "not-an-origin"]
)
def test_bearer_does_not_override_untrusted_browser_origin(origin: str) -> None:
    with client_for() as client:
        assert (
            client.get("/activity", headers={**BEARER, "Origin": origin}).status_code
            == 403
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver.attacker.example",
        "http://testserver:9999",
        "http://testserver:0",
        " http://testserver",
        "http://testserver/path",
        "http://testserver@attacker.example",
        "https://attacker.example",
    ],
)
def test_capability_does_not_override_nonopaque_untrusted_origin(origin: str) -> None:
    access = CompanionAccess.generate()
    with client_for(access=access) as client:
        assert (
            client.get(access.url("/activity"), headers={"Origin": origin}).status_code
            == 403
        )


def test_known_origins_and_default_ports_are_allowed() -> None:
    with client_for() as client:
        assert (
            client.get(
                "/activity", headers={**BEARER, "Origin": "http://testserver:80"}
            ).status_code
            == 200
        )
        assert (
            client.get(
                "/activity", headers={**BEARER, "Origin": "https://trusted.example"}
            ).status_code
            == 200
        )


@pytest.mark.parametrize("origin", [None, "null", "http://testserver"])
def test_desktop_requires_capability_including_sandboxed_frames(
    origin: str | None,
) -> None:
    access = CompanionAccess.generate()
    headers = {} if origin is None else {"Origin": origin}
    with client_for(access=access) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/desktop/websockify", headers=headers):
                pytest.fail("Unauthenticated desktop connection was accepted")
        assert denied.value.code == 1008
        with client.websocket_connect(
            access.url("/desktop/websockify"), headers=headers
        ) as websocket:
            assert websocket.receive_text() == "desktop stream"


@pytest.mark.parametrize("origin", [None, "null", "https://attacker.example"])
def test_bearer_alone_cannot_open_untrusted_or_opaque_desktop(
    origin: str | None,
) -> None:
    headers = BEARER if origin is None else {**BEARER, "Origin": origin}
    with client_for() as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect("/desktop/websockify", headers=headers):
                pytest.fail("Cross-site desktop connection was accepted")
        assert denied.value.code == 1008


def test_capability_does_not_open_desktop_from_hostile_origin() -> None:
    access = CompanionAccess.generate()
    with client_for(access=access) as client:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                access.url("/desktop/audio"),
                headers={"Origin": "https://attacker.example"},
            ):
                pytest.fail("Cross-site desktop connection was accepted")
        assert denied.value.code == 1008


def test_opaque_frame_support_can_be_disabled() -> None:
    access = CompanionAccess.generate()
    with client_for(access=access, allow_opaque_origin=False) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                access.url("/desktop/audio"), headers={"Origin": "null"}
            ):
                pytest.fail("Opaque origin was accepted despite explicit policy")


def test_anonymous_local_mcp_does_not_expose_companion_or_trust_rebound_host() -> None:
    with client_for(allow_unauthenticated_mcp=True) as client:
        assert client.post("/mcp").status_code == 200
        assert client.get("/activity").status_code == 401
        assert (
            client.post("/mcp", headers={"Host": "attacker.example"}).status_code == 401
        )
        assert (
            client.post(
                "/mcp", headers={"Origin": "https://attacker.example"}
            ).status_code
            == 403
        )


@pytest.mark.parametrize(
    "headers",
    [
        [
            ("Authorization", "Bearer wrong"),
            ("Authorization", "Bearer test-server-token"),
        ],
        [
            ("Authorization", "Bearer test-server-token"),
            ("Origin", "http://testserver"),
            ("Origin", "https://attacker.example"),
        ],
        [("Authorization", "Bearer \u00e9")],
    ],
)
def test_ambiguous_or_malformed_headers_are_rejected(
    headers: list[tuple[str, str]],
) -> None:
    with client_for() as client:
        # Bytes permit deliberately malformed non-ASCII HTTP header values.
        response = client.get(
            "/activity",
            headers=[(key.encode(), value.encode()) for key, value in headers],
        )
        assert response.status_code in {401, 403}


def test_duplicate_or_oversized_capability_query_is_rejected() -> None:
    access = CompanionAccess.generate()
    url = access.url("/activity")
    with client_for(access=access) as client:
        assert client.get(url + "&computer_access=wrong").status_code == 401
        assert (
            client.get(url, headers={"X-Computer-Access": access.token}).status_code
            == 401
        )
        assert client.get(url + "&x=1" * 65).status_code == 401


def test_capabilities_are_random_and_urls_replace_stale_values() -> None:
    first, second = CompanionAccess.generate(), CompanionAccess.generate()
    assert first.token != second.token
    assert len(first.token) >= 40
    assert first.token not in repr(first)
    url = first.url(
        "ws://testserver/desktop/audio?computer_access=old&quality=1#anchor"
    )
    parts = urlsplit(url)
    assert parts.fragment == "anchor"
    assert parse_qs(parts.query) == {
        "computer_access": [first.token],
        "quality": ["1"],
    }


def test_invalid_origin_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="HTTP"):
        BearerTokenMiddleware(
            Starlette(), allowed_origins=["https://trusted.example/path"]
        )
    with pytest.raises(ValueError, match="fixed"):
        BearerTokenMiddleware(Starlette(), allow_unauthenticated_mcp=True)


def test_bearer_can_open_desktop_from_explicitly_trusted_origin() -> None:
    with client_for() as client:
        with client.websocket_connect(
            "/desktop/audio", headers={**BEARER, **TRUSTED_ORIGIN}
        ) as websocket:
            assert websocket.receive_text() == "desktop stream"

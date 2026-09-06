"""Authentication boundaries for MCP and its browser companion.

The random companion capability is deliberately separate from a configured MCP
bearer token. It lets sandboxed MCP App frames open desktop WebSockets without
putting a long-lived server credential in a URL or a browser cookie.
"""

import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_ACCESS_QUERY = "computer_access"
_ACCESS_HEADER = "x-computer-access"
_COMPANION_PATHS = frozenset(
    {"/", "/activity", "/dashboard.html", "/desktop/websockify", "/desktop/audio"}
)


@dataclass(frozen=True, slots=True)
class CompanionAccess:
    """An unguessable capability valid only for this server process."""

    token: str = field(repr=False)

    @classmethod
    def generate(cls) -> "CompanionAccess":
        """Create a fresh capability; never reuse the remote MCP bearer token."""
        return cls(secrets.token_urlsafe(32))

    def url(self, url: str) -> str:
        """Attach the capability to a companion URL, replacing any old value."""
        parts = urlsplit(url)
        query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != _ACCESS_QUERY
        ]
        query.append((_ACCESS_QUERY, self.token))
        return urlunsplit(parts._replace(query=urlencode(query)))


def _origin(value: str) -> tuple[str, str, int] | None:
    """Parse a serialized web origin without accepting URLs with path/userinfo."""
    if value != value.strip() or any(ord(character) < 32 for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
        ):
            return None
        return (
            parsed.scheme,
            parsed.hostname.lower(),
            parsed.port or (443 if parsed.scheme == "https" else 80),
        )
    except ValueError:
        return None


def _matches(supplied: str, expected: str | None) -> bool:
    """Compare bytes so malformed non-ASCII credentials cannot raise TypeError."""
    return bool(expected) and hmac.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


class BearerTokenMiddleware:
    """Protect MCP and companion routes with separate, explicit credentials.

    Browser origins are compared with a fixed allowlist, never the untrusted
    Host header. A sandboxed frame's opaque null origin, and a WebSocket
    without Origin, require a companion capability. A bearer token alone never
    authorizes those browser contexts.

    allow_mcp_capability is an explicit opt-in for a standalone dashboard
    that calls /mcp directly. Otherwise the capability cannot call MCP tools.
    allow_unauthenticated_mcp preserves an explicitly configured local HTTP
    endpoint; it still validates Origin and Host and does not expose companion
    routes. Health checks alone are public.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str | None = None,
        companion_access: CompanionAccess | None = None,
        allowed_origins: Iterable[str] = (),
        allow_mcp_capability: bool = False,
        allow_unauthenticated_mcp: bool = False,
        allow_opaque_origin: bool = True,
    ) -> None:
        self.app = app
        self.token = token
        self.companion_access = companion_access
        origins = [_origin(value) for value in allowed_origins]
        if any(value is None for value in origins):
            raise ValueError("allowed_origins must contain HTTP(S) origins")
        self.allowed_origins = frozenset(value for value in origins if value)
        self.allow_mcp_capability = allow_mcp_capability
        self.allow_unauthenticated_mcp = allow_unauthenticated_mcp
        self.allow_opaque_origin = allow_opaque_origin
        if allow_unauthenticated_mcp and not self.allowed_origins:
            raise ValueError("Unauthenticated MCP requires fixed allowed_origins")

    def _capability(self, scope: Scope, headers: Headers) -> bool:
        if self.companion_access is None:
            return False
        values = headers.getlist(_ACCESS_HEADER)
        try:
            query = parse_qsl(
                scope.get("query_string", b"").decode("ascii"),
                keep_blank_values=True,
                max_num_fields=64,
            )
        except (UnicodeDecodeError, ValueError):
            return False
        values.extend(value for key, value in query if key == _ACCESS_QUERY)
        return len(values) == 1 and _matches(values[0], self.companion_access.token)

    def _bearer(self, headers: Headers) -> bool:
        values = headers.getlist("authorization")
        if len(values) != 1:
            return False
        scheme, _, supplied = values[0].partition(" ")
        return scheme.lower() == "bearer" and _matches(supplied, self.token)

    def _allowed_origin(
        self, scope: Scope, headers: Headers, *, capability: bool
    ) -> bool:
        values = headers.getlist("origin")
        if not values:
            return scope["type"] != "websocket" or capability
        if len(values) != 1:
            return False
        if values[0] == "null":
            return capability and self.allow_opaque_origin
        origin = _origin(values[0])
        return origin is not None and origin in self.allowed_origins

    def _allowed_host(self, scope: Scope, headers: Headers) -> bool:
        """Bind optional anonymous MCP to configured addresses, not DNS rebinding."""
        hosts = headers.getlist("host")
        if len(hosts) != 1:
            return False
        scheme = "https" if scope.get("scheme") == "https" else "http"
        return _origin(f"{scheme}://{hosts[0]}") in self.allowed_origins

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, *, forbidden: bool = False
    ) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if not forbidden:
            headers["WWW-Authenticate"] = "Bearer"
        response = JSONResponse(
            {"error": "forbidden origin" if forbidden else "unauthorized"},
            status_code=403 if forbidden else 401,
            headers=headers,
        )
        await response(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if (
            scope["type"] == "http"
            and path == "/healthz"
            and scope.get("method") in {"GET", "HEAD"}
        ):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        capability = (
            path in _COMPANION_PATHS or (is_mcp and self.allow_mcp_capability)
        ) and self._capability(scope, headers)
        if not self._allowed_origin(scope, headers, capability=capability):
            await self._reject(scope, receive, send, forbidden=True)
            return

        anonymous_mcp = (
            scope["type"] == "http"
            and is_mcp
            and self.allow_unauthenticated_mcp
            and self._allowed_host(scope, headers)
        )
        if not (self._bearer(headers) or capability or anonymous_mcp):
            await self._reject(scope, receive, send)
            return

        async def private_response(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers["Cache-Control"] = "no-store"
                response_headers["Referrer-Policy"] = "no-referrer"
            await send(message)

        await self.app(scope, receive, private_response)

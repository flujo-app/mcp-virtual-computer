"""Configuration dataclasses."""

import os
from dataclasses import dataclass, field
from typing import Literal

Transport = Literal["stdio", "http"]


def _lifecycle_tools_enabled() -> bool:
    """Read the opt-in lifecycle tool feature flag from the environment."""
    return os.getenv("ENABLE_LIFECYCLE_TOOLS", "false").strip().casefold() == "true"


@dataclass(frozen=True, slots=True, kw_only=True)
class BackendConfig:
    """Base class for all backend configurations.

    Contains fields shared across all backends. Backend-specific
    config classes inherit from this.
    """

    # Passed through for tool description generation
    default_timeout: int = 120


@dataclass(frozen=True, slots=True, kw_only=True)
class ServerConfig:
    """Core server configuration from CLI arguments.

    Consumed by the MCP server layer (Phase 4) and the startup
    orchestration logic. Does not contain backend-specific config.
    """

    # Transport
    transport: Transport = "stdio"
    host: str = "127.0.0.1"  # HTTP bind address
    port: int = 8435  # HTTP listen port

    # Exec defaults
    default_timeout: int = 120  # seconds
    output_limit: int = 2_097_152  # bytes (2 MiB)

    # Tool description
    tool_instruction_override: str | None = None
    extended_tool_instruction: str | None = None

    # Optional MCP surface
    enable_lifecycle_tools: bool = field(default_factory=_lifecycle_tools_enabled)

    # Session management (HTTP only)
    session_timeout: int = 300  # seconds (5 minutes)

    # Remote HTTP protection
    auth_token: str | None = None
    allow_unauthenticated_http: bool = False

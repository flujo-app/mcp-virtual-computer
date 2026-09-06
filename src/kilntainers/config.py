"""Configuration dataclasses for the single-computer Docker server."""

import os
from dataclasses import dataclass, field
from typing import Literal

from kilntainers.auth import CompanionAccess

Transport = Literal["stdio", "http"]


def env_flag(name: str, *, default: bool = False) -> bool:
    """Read a strict, human-friendly boolean environment variable."""
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off; got {value!r}"
    )


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
    file_text_limit: int = 1_048_576  # bytes (1 MiB)
    workspace_directory: str = "/workspace"

    # A server process owns exactly one persistent computer. The CLI replaces
    # this programmatic default with the required COMPUTER_ID environment value.
    computer_id: str = "virtual-computer"
    desktop_environment: bool = True
    network_access: bool = True
    expose_lifecycle_tools: bool = False

    # Tool description
    tool_instruction_override: str | None = None
    extended_tool_instruction: str | None = None

    # A fresh browser capability is scoped to this server process. Exclude it
    # from repr/equality so diagnostics never reveal it.
    companion_access: CompanionAccess = field(
        default_factory=CompanionAccess.generate, repr=False, compare=False
    )
    allowed_http_hosts: tuple[str, ...] = ()
    allowed_http_origins: tuple[str, ...] = ()

    # Remote HTTP protection
    auth_token: str | None = None
    allow_unauthenticated_http: bool = False

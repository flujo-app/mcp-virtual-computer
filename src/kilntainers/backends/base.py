"""Backend abstraction layer — ABCs and shared types."""

import argparse
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from inspect import signature
from types import TracebackType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kilntainers.config import BackendConfig
    from kilntainers.windows_docker import DockerRuntimeProgress

RuntimeProgressReporter = Callable[["DockerRuntimeProgress"], Awaitable[None]]

# --- Shared types ---


@dataclass(frozen=True, slots=True)
class ExecResult:
    """Result of a command execution.

    The return type from every exec call. Immutable, with no optional
    fields — every execution produces all four values.

    Maps directly to the MCP response schema (Functional spec §2.2).
    The MCP layer serializes this to JSON for the tool response.
    """

    stdout: str
    stderr: str
    exit_code: int
    exec_duration_ms: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ComputerInfo:
    """Provider-neutral description of a managed sandbox computer."""

    computer_id: str
    sandbox_id: str
    backend: str
    state: str
    temporary: bool
    image: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, str | bool | None]:
        """Return a JSON-serializable representation for MCP responses."""
        return asdict(self)


@dataclass(frozen=True, slots=True, kw_only=True)
class ExecRequest:
    """Validated parameters for a command execution.

    Constructed by the MCP layer after input validation. The MCP layer
    resolves defaults (effective timeout, configured output limit) so
    the backend always receives concrete values.

    kw_only=True forces callers to use keyword arguments, which is
    clearer for a dataclass with many optional fields.
    """

    # Exactly one of command or args must be provided
    command: str | None = None
    args: list[str] | None = None

    # Optional parameters
    stdin: str | None = None
    working_directory: str | None = None

    # Always provided by MCP layer (defaults resolved before reaching backend)
    timeout: int  # seconds
    output_limit: int  # bytes

    def __post_init__(self) -> None:
        # Validate mutual exclusivity of command/args
        if self.command is not None and self.args is not None:
            raise ValueError("command and args are mutually exclusive")
        if self.command is None and self.args is None:
            raise ValueError("either command or args must be provided")

        # Validate working_directory is absolute
        if self.working_directory is not None and not self.working_directory.startswith(
            "/"
        ):
            raise ValueError("working_directory must be an absolute path")

        # Validate timeout
        if self.timeout < 1:
            raise ValueError("timeout must be at least 1 second")

        # Validate output_limit
        if self.output_limit < 1:
            raise ValueError("output_limit must be positive")


# --- ABCs ---


class Sandbox(ABC):
    """An active, isolated sandbox for executing commands.

    Created by Backend.create_sandbox(). Each Sandbox is independent —
    no shared state between Sandbox instances. Supports async context
    manager for automatic cleanup.
    """

    @property
    @abstractmethod
    def sandbox_id(self) -> str:
        """Unique identifier for this sandbox.

        Used for logging, debugging, and session-to-sandbox mapping in
        HTTP mode. For Docker, this is the container ID (short form).
        """
        ...

    @property
    def computer_id(self) -> str:
        """Stable human-facing ID used to reconnect to this computer.

        Legacy backends do not expose a separate name, so their provider ID is
        also used as the computer ID. Managed backends override this property.
        """
        return getattr(self, "_managed_computer_id", self.sandbox_id)

    @property
    def temporary(self) -> bool:
        """Whether this computer should be removed when its MCP owner exits."""
        return getattr(self, "_managed_temporary", True)

    @property
    def desktop_url(self) -> str | None:
        """Browser-reachable noVNC websocket URL, when a desktop is enabled."""
        return None

    @property
    def desktop_environment(self) -> bool:
        """Whether this sandbox is backed by a real desktop environment."""
        return self.desktop_url is not None

    @property
    def network_access(self) -> bool:
        """Whether this sandbox currently permits outbound network traffic."""
        return True

    @abstractmethod
    async def exec(self, request: ExecRequest) -> ExecResult:
        """Execute a command in the sandbox.

        Returns an ExecResult for all normal outcomes including timeout
        and output-limit conditions. Raises SandboxDiedError if the
        sandbox has died (before or during execution).
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the sandbox and release all resources.

        Idempotent — safe to call on an already-stopped sandbox.
        """
        ...

    @abstractmethod
    async def wait_for_death(self) -> None:
        """Block until the sandbox dies unexpectedly.

        Resolves when the sandbox terminates for reasons other than
        stop() being called (OOM, external kill, daemon crash, etc.).

        Must NOT resolve when stop() is called. Implementations track
        whether stop was requested and suppress the signal in that case.

        The MCP layer runs this as a background task to detect sandbox
        death between exec calls. On normal shutdown, the MCP layer
        cancels this task before calling stop().
        """
        ...

    # --- Context manager support (concrete, not abstract) ---

    async def __aenter__(self) -> "Sandbox":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.stop()


class Backend(ABC):
    """Factory for creating sandboxes.

    Configured at startup from CLI arguments. One instance per server
    process. Creates independent Sandbox objects on demand.
    """

    def __init__(self, config: "BackendConfig") -> None:
        """Initialize the backend with configuration.

        Args:
            config: The backend configuration.
        """
        self._validated: bool = False
        self._config = config

    @classmethod
    def prepare_runtime(cls) -> None:
        """Prepare process-wide state before the server event loop is created.

        Most backends need no preparation. Backends with SDKs that configure
        global runtime state can override this hook so those changes apply only
        when that backend is selected.
        """

    def start_runtime_preparation(self) -> None:
        """Start optional lazy host-runtime preparation without blocking."""

    async def ensure_runtime(
        self,
        progress: RuntimeProgressReporter | None = None,
    ) -> None:
        """Wait for optional host-runtime preparation to finish."""

    def runtime_status(self) -> dict[str, object]:
        """Return optional host-runtime preparation state for an MCP App."""
        return {
            "runtime_state": "ready",
            "runtime_phase": "ready",
            "runtime_message": "Container runtime bootstrap is not required.",
            "runtime_progress": 4.0,
            "runtime_total": 4.0,
            "downloaded_bytes": None,
            "download_total_bytes": None,
            "runtime_error": None,
        }

    @classmethod
    @abstractmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        """Register backend-specific CLI arguments on the given argparse group.

        Args:
            group: An argparse argument group to add arguments to.
        """
        ...

    @classmethod
    @abstractmethod
    def config_from_args(cls, args: argparse.Namespace) -> "BackendConfig":
        """Build backend config from parsed CLI arguments.

        Args:
            args: Parsed command-line arguments from argparse.

        Returns:
            A BackendConfig subclass instance with this backend's configuration.
        """
        ...

    async def validate(self) -> None:
        """Check all prerequisites. Raises BackendError on failure.

        Results are cached — subsequent calls are no-ops after the
        first successful validation.
        """
        if self._validated:
            return
        await self._validate()
        self._validated = True

    @abstractmethod
    async def _validate(self) -> None:
        """Implementation-specific validation. Override this method.

        Check that the backend's prerequisites are met (e.g., Docker
        daemon is reachable, configured image is valid). Raise
        BackendError with an actionable message on failure.
        """
        ...

    async def create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> Sandbox:
        """Create a new sandbox and return it ready to use.

        Auto-validates if validate() has not been called. Returns a
        ready-to-use Sandbox (readiness check already passed).

        Managed backends use ``computer_id`` as a stable provider name and
        ``temporary`` to select cleanup behavior. Legacy backends may ignore
        both values while still participating in the in-process registry.
        """
        await self.validate()
        parameters = signature(self._create_sandbox).parameters
        if "computer_id" not in parameters:
            # Compatibility for third-party backends built against the original
            # no-argument _create_sandbox contract.
            return await self._create_sandbox()
        return await self._create_sandbox(
            computer_id=computer_id,
            temporary=temporary,
        )

    @abstractmethod
    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> Sandbox:
        """Implementation-specific sandbox creation. Override this method.

        Must perform the full startup sequence:
        1. Create the sandbox (e.g., docker run)
        2. Verify readiness (e.g., trivial exec)
        3. Return the ready Sandbox object

        Raise BackendError if startup fails at any step.
        """
        ...

    async def attach_sandbox(self, computer_id: str) -> Sandbox | None:
        """Attach to a provider computer created by an earlier process.

        Backends with provider-side discovery should override this. Returning
        ``None`` means no matching computer exists.
        """
        return None

    async def refresh_sandbox(
        self,
        computer_id: str,
        sandbox: Sandbox,
    ) -> Sandbox:
        """Refresh provider-owned runtime state on an attached sandbox handle.

        The default is intentionally a no-op for legacy and in-memory backends.
        Provider backends should override this when another server process can
        mutate state that is cached on the local ``Sandbox`` instance.
        """
        return sandbox

    async def list_computers(self) -> list[ComputerInfo]:
        """List provider-side managed computers visible to this backend."""
        return []

    async def restart_computer(self, computer_id: str) -> Sandbox | None:
        """Restart and reattach a managed computer, if supported."""
        return None

    async def delete_computer(self, computer_id: str) -> bool:
        """Permanently delete a provider-side computer, if supported."""
        return False

    async def factory_reset_computer(self, computer_id: str) -> Sandbox | None:
        """Recreate a managed computer from its configured base image."""
        return None

    async def set_network_access(
        self, computer_id: str, enabled: bool
    ) -> Sandbox | None:
        """Change outbound network access for a managed computer, if supported."""
        return None

    async def switch_desktop_environment(
        self, computer_id: str, enabled: bool
    ) -> Sandbox | None:
        """Switch a managed computer's UI mode without losing its state."""
        return None

    @abstractmethod
    def tool_instructions(self) -> str | None:
        """Return tool description text for the terminal_execute tool.

        Returns a string describing this backend's sandbox capabilities,
        or None if the backend cannot provide a description (e.g.,
        custom Docker image where the baked-in description doesn't apply).

        The MCP layer uses this in tool description assembly. When None,
        the server requires --tool-instruction-override.
        """
        ...

"""Mock sandbox and backend for testing higher-level code.

These mocks are designed for Phase 4 (MCP server) tests, allowing
testing of the server layer without requiring a real backend like Docker.
"""

import argparse
import asyncio

from kilntainers.backends.base import Backend, ExecRequest, ExecResult, Sandbox
from kilntainers.config import BackendConfig
from kilntainers.errors import BackendError, SandboxDiedError


class MockSandbox(Sandbox):
    """Sandbox that returns preconfigured ExecResults.

    Designed for testing the MCP server layer without requiring a real
    backend like Docker.
    """

    def __init__(
        self,
        *,
        sandbox_id: str = "mock-sandbox-001",
        exec_results: list[ExecResult] | None = None,
    ) -> None:
        """Initialize a mock sandbox.

        Args:
            sandbox_id: The sandbox ID to return.
            exec_results: A queue of results to return from exec(). If None,
                returns a default success result.
        """
        self._sandbox_id = sandbox_id
        self._stopped = False
        self._death_event = asyncio.Event()
        self._stop_requested = False
        self.exec_results: list[ExecResult] = exec_results or []
        self.exec_calls: list[ExecRequest] = []

    @property
    def sandbox_id(self) -> str:
        """Return the sandbox ID."""
        return self._sandbox_id

    async def exec(self, request: ExecRequest) -> ExecResult:
        """Record the call and return a queued result or default."""
        if self._death_event.is_set():
            raise SandboxDiedError(f"mock sandbox {self._sandbox_id} died")
        self.exec_calls.append(request)
        if self.exec_results:
            return self.exec_results.pop(0)
        return ExecResult(
            stdout="",
            stderr="",
            exit_code=0,
            exec_duration_ms=1,
        )

    async def stop(self) -> None:
        """Mark the sandbox as stopped."""
        self._stopped = True
        self._stop_requested = True
        self._death_event.set()

    async def wait_for_death(self) -> None:
        """Block until the sandbox dies."""
        await self._death_event.wait()

    def simulate_death(self) -> None:
        """Test helper: trigger sandbox death."""
        self._death_event.set()

    def is_stopped(self) -> bool:
        """Test helper: check if stopped."""
        return self._stopped


class MockBackend(Backend):
    """Backend that returns MockSandbox instances.

    Designed for testing the MCP server layer without requiring a real
    backend like Docker.
    """

    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        """Mock implementation - does nothing."""

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        """Mock implementation - returns default config."""
        return BackendConfig()

    def __init__(
        self,
        config: BackendConfig,
        *,
        tool_instructions: str | None = "mock instructions",
        sandbox_id: str = "mock-sandbox-001",
        exec_results: list[ExecResult] | None = None,
    ) -> None:
        """Initialize a mock backend.

        Args:
            config: The backend configuration.
            tool_instructions: The tool instructions to return.
            sandbox_id: The sandbox ID to use for created sandboxes.
            exec_results: Results queue for created sandboxes.
        """
        super().__init__(config)
        self._tool_instructions = tool_instructions
        self._sandbox_id = sandbox_id
        self._exec_results = exec_results
        self._validated = False
        # Test helpers for lazy creation testing
        self.create_count = 0
        self.fail_next_create = False

    async def _validate(self) -> None:
        """Mark as validated (no-op for mock)."""
        self._validated = True

    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> Sandbox:
        """Return a MockSandbox.

        Supports test helpers:
        - create_count: incremented on each sandbox creation
        - fail_next_create: if True, raises BackendError and resets flag
        """
        if self.fail_next_create:
            self.fail_next_create = False
            raise BackendError("mock creation failure")
        self.create_count += 1
        return MockSandbox(
            sandbox_id=self._sandbox_id,
            exec_results=self._exec_results,
        )

    def tool_instructions(self) -> str | None:
        """Return the configured tool instructions."""
        return self._tool_instructions

    def is_validated(self) -> bool:
        """Test helper: check if validate was called."""
        return self._validated

"""Tests for backend abstraction layer — ABCs and shared types."""

import argparse
import asyncio
from dataclasses import FrozenInstanceError

import pytest

from kilntainers.backends.base import (
    Backend,
    ExecRequest,
    ExecResult,
    Sandbox,
)
from kilntainers.config import BackendConfig
from kilntainers.errors import SandboxDiedError

# --- ExecRequest Tests ---


class TestExecRequestValidation:
    """Test ExecRequest.__post_init__ validation."""

    def test_both_command_and_args_raises(self) -> None:
        """Providing both command and args should raise ValueError."""
        with pytest.raises(ValueError, match="mutually exclusive"):
            ExecRequest(
                command="ls -la",
                args=["ls", "-la"],
                timeout=30,
                output_limit=1024,
            )

    def test_neither_command_nor_args_raises(self) -> None:
        """Providing neither command nor args should raise ValueError."""
        with pytest.raises(ValueError, match="either command or args"):
            ExecRequest(timeout=30, output_limit=1024)

    def test_relative_working_directory_raises(self) -> None:
        """Relative working_directory should raise ValueError."""
        with pytest.raises(ValueError, match="absolute path"):
            ExecRequest(
                command="ls",
                working_directory="relative/path",
                timeout=30,
                output_limit=1024,
            )

    def test_absolute_working_directory_valid(self) -> None:
        """Absolute working_directory should be valid."""
        req = ExecRequest(
            command="ls",
            working_directory="/absolute/path",
            timeout=30,
            output_limit=1024,
        )
        assert req.working_directory == "/absolute/path"

    def test_timeout_less_than_one_raises(self) -> None:
        """timeout < 1 should raise ValueError."""
        with pytest.raises(ValueError, match="at least 1 second"):
            ExecRequest(command="ls", timeout=0, output_limit=1024)

        with pytest.raises(ValueError, match="at least 1 second"):
            ExecRequest(command="ls", timeout=-1, output_limit=1024)

    def test_timeout_one_or_greater_valid(self) -> None:
        """timeout >= 1 should be valid."""
        req = ExecRequest(command="ls", timeout=1, output_limit=1024)
        assert req.timeout == 1

        req = ExecRequest(command="ls", timeout=300, output_limit=1024)
        assert req.timeout == 300

    def test_output_limit_less_than_one_raises(self) -> None:
        """output_limit < 1 should raise ValueError."""
        with pytest.raises(ValueError, match="positive"):
            ExecRequest(command="ls", timeout=30, output_limit=0)

        with pytest.raises(ValueError, match="positive"):
            ExecRequest(command="ls", timeout=30, output_limit=-1)

    def test_output_limit_positive_valid(self) -> None:
        """output_limit >= 1 should be valid."""
        req = ExecRequest(command="ls", timeout=30, output_limit=1)
        assert req.output_limit == 1

        req = ExecRequest(command="ls", timeout=30, output_limit=10_000_000)
        assert req.output_limit == 10_000_000

    def test_command_only_valid(self) -> None:
        """Request with only command should be valid."""
        req = ExecRequest(
            command="ls -la",
            timeout=30,
            output_limit=1024,
        )
        assert req.command == "ls -la"
        assert req.args is None

    def test_args_only_valid(self) -> None:
        """Request with only args should be valid."""
        req = ExecRequest(
            args=["ls", "-la"],
            timeout=30,
            output_limit=1024,
        )
        assert req.args == ["ls", "-la"]
        assert req.command is None

    def test_with_stdin_valid(self) -> None:
        """Request with stdin should be valid."""
        req = ExecRequest(
            command="cat",
            stdin="hello world",
            timeout=30,
            output_limit=1024,
        )
        assert req.stdin == "hello world"

    def test_frozen_immutable(self) -> None:
        """ExecRequest should be frozen (immutable)."""
        req = ExecRequest(
            command="ls",
            timeout=30,
            output_limit=1024,
        )
        with pytest.raises(FrozenInstanceError):
            req.command = "new command"  # type: ignore[misc]

    def test_kw_only_enforced(self) -> None:
        """Positional arguments should not be allowed."""
        with pytest.raises(TypeError):
            ExecRequest("ls", 30, 1024)  # type: ignore


# --- ExecResult Tests ---


class TestExecResult:
    """Test ExecResult dataclass."""

    def test_construction(self) -> None:
        """All fields should be populated correctly."""
        result = ExecResult(
            stdout="output",
            stderr="errors",
            exit_code=0,
            exec_duration_ms=100,
        )
        assert result.stdout == "output"
        assert result.stderr == "errors"
        assert result.exit_code == 0
        assert result.exec_duration_ms == 100

    def test_frozen_immutable(self) -> None:
        """ExecResult should be frozen (immutable)."""
        result = ExecResult(
            stdout="output",
            stderr="errors",
            exit_code=0,
            exec_duration_ms=100,
        )
        with pytest.raises(FrozenInstanceError):
            result.stdout = "new output"  # type: ignore[misc]


# --- Backend ABC Tests ---


class StubBackend(Backend):
    """Minimal Backend implementation for testing ABC behavior."""

    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        """Stub implementation - does nothing."""

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        """Stub implementation - returns default config."""
        return BackendConfig()

    def __init__(self, config: BackendConfig) -> None:
        super().__init__(config)
        self.validate_called = 0
        self.create_sandbox_called = 0

    async def _validate(self) -> None:
        """Stub validation."""
        self.validate_called += 1

    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> Sandbox:
        """Stub sandbox creation."""
        self.create_sandbox_called += 1
        return StubSandbox()

    def tool_instructions(self) -> str | None:
        return "stub instructions"


class StubSandbox(Sandbox):
    """Minimal Sandbox implementation for testing."""

    def __init__(self, sandbox_id: str = "stub-sandbox-001") -> None:
        self._sandbox_id = sandbox_id
        self._stopped = False
        self._death_event = asyncio.Event()

    @property
    def sandbox_id(self) -> str:
        return self._sandbox_id

    async def exec(self, request: ExecRequest) -> ExecResult:
        if self._death_event.is_set():
            raise SandboxDiedError("stub sandbox died")
        return ExecResult(
            stdout="",
            stderr="",
            exit_code=0,
            exec_duration_ms=1,
        )

    async def stop(self) -> None:
        self._stopped = True
        self._death_event.set()

    async def wait_for_death(self) -> None:
        await self._death_event.wait()


class TestBackendABC:
    """Test Backend ABC concrete methods."""

    @pytest.mark.asyncio
    async def test_validate_calls_validate_once(self) -> None:
        """First validate() call should call _validate()."""
        backend = StubBackend(BackendConfig())
        await backend.validate()
        assert backend.validate_called == 1

    @pytest.mark.asyncio
    async def test_validate_caches_result(self) -> None:
        """Subsequent validate() calls should not call _validate()."""
        backend = StubBackend(BackendConfig())
        await backend.validate()
        await backend.validate()
        await backend.validate()
        assert backend.validate_called == 1

    @pytest.mark.asyncio
    async def test_create_sandbox_auto_validates(self) -> None:
        """create_sandbox() should call validate() if not validated."""
        backend = StubBackend(BackendConfig())
        await backend.create_sandbox()
        assert backend.validate_called == 1
        assert backend.create_sandbox_called == 1

    @pytest.mark.asyncio
    async def test_create_sandbox_does_not_revalidate(self) -> None:
        """create_sandbox() should not re-validate if already validated."""
        backend = StubBackend(BackendConfig())
        await backend.validate()
        await backend.create_sandbox()
        assert backend.validate_called == 1
        assert backend.create_sandbox_called == 1

    def test_tool_instructions_returns_value(self) -> None:
        """tool_instructions() should return subclass value."""
        backend = StubBackend(BackendConfig())
        assert backend.tool_instructions() == "stub instructions"


# --- Sandbox ABC Tests ---


class TestSandboxABC:
    """Test Sandbox ABC context manager."""

    @pytest.mark.asyncio
    async def test_context_manager_calls_stop_on_exit(self) -> None:
        """async with sandbox should call stop() on normal exit."""
        sandbox = StubSandbox()
        async with sandbox:
            pass
        assert sandbox._stopped is True

    @pytest.mark.asyncio
    async def test_context_manager_calls_stop_on_exception(self) -> None:
        """async with sandbox should call stop() even on exception."""
        sandbox = StubSandbox()
        with pytest.raises(ValueError):
            async with sandbox:
                raise ValueError("test error")
        assert sandbox._stopped is True

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self) -> None:
        """Calling stop() multiple times should not error."""
        sandbox = StubSandbox()
        await sandbox.stop()
        await sandbox.stop()
        await sandbox.stop()
        assert sandbox._stopped is True

    @pytest.mark.asyncio
    async def test_exec_returns_result(self) -> None:
        """exec() should return ExecResult."""
        sandbox = StubSandbox()
        req = ExecRequest(command="test", timeout=30, output_limit=1024)
        result = await sandbox.exec(req)
        assert isinstance(result, ExecResult)

    @pytest.mark.asyncio
    async def test_exec_on_dead_sandbox_raises(self) -> None:
        """exec() on dead sandbox should raise SandboxDiedError."""
        sandbox = StubSandbox()
        # Kill the sandbox
        sandbox._death_event.set()
        req = ExecRequest(command="test", timeout=30, output_limit=1024)
        with pytest.raises(SandboxDiedError):
            await sandbox.exec(req)

    @pytest.mark.asyncio
    async def test_sandbox_id_property(self) -> None:
        """sandbox_id property should return the ID."""
        sandbox = StubSandbox(sandbox_id="test-id-123")
        assert sandbox.sandbox_id == "test-id-123"

    @pytest.mark.asyncio
    async def test_wait_for_death_blocks(self) -> None:
        """wait_for_death() should block until death."""
        sandbox = StubSandbox()
        # Create a task that waits for death
        task = asyncio.create_task(sandbox.wait_for_death())
        # Task should not be done yet
        await asyncio.sleep(0.01)
        assert not task.done()
        # Kill the sandbox
        await sandbox.stop()
        # Task should now complete
        await task
        assert task.done()

    @pytest.mark.asyncio
    async def test_context_manager_returns_self(self) -> None:
        """__aenter__ should return self."""
        sandbox = StubSandbox()
        async with sandbox as entered:
            assert sandbox is entered
            assert entered is sandbox

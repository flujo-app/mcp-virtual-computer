"""Unit tests for E2B backend.

Tests mock the E2B SDK to simulate sandbox behavior without
requiring a real E2B account or network access.
"""

import asyncio

import pytest
from e2b.exceptions import TimeoutException
from e2b.sandbox.commands.command_handle import CommandExitException

from kilntainers.backends.base import ExecRequest
from kilntainers.backends.e2b import (
    E2BBackend,
    E2BBackendConfig,
    E2BSandbox,
)
from kilntainers.errors import BackendError, SandboxDiedError

# --- Mock E2B SDK utilities ---


class MockCommandResult:
    """Mock E2B CommandResult."""

    def __init__(self, stdout: str = "", stderr: str = "", exit_code: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class MockCommandHandle:
    """Mock E2B background command handle."""

    def __init__(self, pid: int = 12345, result: MockCommandResult | None = None):
        self.pid = pid
        self._result = result or MockCommandResult()

    async def wait(self) -> MockCommandResult:
        return self._result


class MockCommands:
    """Mock E2B commands module."""

    def __init__(self):
        self.run_responses: list[MockCommandResult | MockCommandHandle] = []
        self.sent_stdin: list[tuple[int, str]] = []
        self.last_cmd: str | None = None

    def set_response(self, response: MockCommandResult | MockCommandHandle):
        self.run_responses.append(response)

    async def run(self, cmd: str, background: bool = False, **kwargs):
        self.last_cmd = cmd
        if self.run_responses:
            response = self.run_responses.pop(0)
            if background and isinstance(response, MockCommandResult):
                return MockCommandHandle(result=response)
            if not background and isinstance(response, MockCommandHandle):
                return response._result
            return response
        return MockCommandResult()

    async def send_stdin(self, pid: int, data: str):
        self.sent_stdin.append((pid, data))


class MockAsyncSandbox:
    """Mock e2b.AsyncSandbox."""

    def __init__(self, sandbox_id: str = "mock-sandbox-abc123"):
        self.sandbox_id = sandbox_id
        self.commands = MockCommands()
        self._killed = False

    async def kill(self) -> bool:
        self._killed = True
        return True

    @classmethod
    async def create(cls, **kwargs):
        return cls()

    @classmethod
    def list(cls, **kwargs):
        """list() returns a paginator synchronously, not awaitable."""
        return []


@pytest.fixture
def mock_e2b(monkeypatch):
    """Mock e2b.AsyncSandbox with configurable responses."""
    mock_sandbox_class = MockAsyncSandbox

    # Patch AsyncSandbox in the e2b module
    import kilntainers.backends.e2b as e2b_module

    monkeypatch.setattr(e2b_module, "AsyncSandbox", mock_sandbox_class)
    return mock_sandbox_class


@pytest.fixture
def default_config():
    """Return a default E2BBackendConfig."""
    return E2BBackendConfig()


# --- E2BBackendConfig tests ---


class TestE2BBackendConfig:
    """Tests for E2BBackendConfig dataclass."""

    def test_default_config(self):
        """Default config has expected values."""
        config = E2BBackendConfig()

        assert config.api_key is None
        assert config.template == "base"
        assert config.shell == "/bin/bash"
        assert config.network_enabled is False
        assert config.sandbox_timeout == 3600
        assert config.metadata is None
        assert config.envs is None
        assert config.default_timeout == 120

    def test_custom_config(self):
        """Custom config values."""
        config = E2BBackendConfig(
            api_key="test-key",
            template="custom-template",
            shell="/bin/sh",
            network_enabled=True,
            sandbox_timeout=7200,
            metadata={"key": "value"},
            envs={"ENV": "val"},
            default_timeout=300,
        )

        assert config.api_key == "test-key"
        assert config.template == "custom-template"
        assert config.shell == "/bin/sh"
        assert config.network_enabled is True
        assert config.sandbox_timeout == 7200
        assert config.metadata == {"key": "value"}
        assert config.envs == {"ENV": "val"}
        assert config.default_timeout == 300


# --- E2BBackend CLI tests ---


class MockArgumentParser:
    """Mock argparse.ArgumentParser for testing."""

    def __init__(self):
        self.arguments = {}

    def add_argument_group(self, name: str):
        return self

    def add_argument(self, *args, **kwargs):
        self.arguments[args[0]] = kwargs


class MockNamespace:
    """Mock argparse.Namespace for testing."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class TestE2BBackendCLI:
    """Tests for E2BBackend CLI methods."""

    def test_add_cli_arguments(self):
        """CLI arguments are registered correctly."""
        parser = MockArgumentParser()
        group = parser.add_argument_group("e2b backend options")

        E2BBackend.add_cli_arguments(group)

        assert "--e2b-api-key" in parser.arguments
        assert "--e2b-template" in parser.arguments
        assert "--e2b-sandbox-timeout" in parser.arguments
        assert "--e2b-metadata" in parser.arguments
        assert "--e2b-env" in parser.arguments

    def test_config_from_args(self):
        """Config is built from args correctly."""
        args = MockNamespace(
            e2b_api_key="test-key",
            e2b_template="custom-template",
            e2b_sandbox_timeout=7200,
            e2b_metadata=["key1=value1", "key2=value2"],
            e2b_env=["ENV1=val1"],
            shell="/bin/sh",
            network=True,
            timeout=300,
        )

        config = E2BBackend.config_from_args(args)  # type: ignore[arg-type]

        assert config.api_key == "test-key"  # type: ignore[attr-defined]
        assert config.template == "custom-template"  # type: ignore[attr-defined]
        assert config.shell == "/bin/sh"  # type: ignore[attr-defined]
        assert config.network_enabled is True  # type: ignore[attr-defined]
        assert config.sandbox_timeout == 7200  # type: ignore[attr-defined]
        assert config.metadata == {"key1": "value1", "key2": "value2"}  # type: ignore[attr-defined]
        assert config.envs == {"ENV1": "val1"}  # type: ignore[attr-defined]
        assert config.default_timeout == 300  # type: ignore[attr-defined]

    def test_config_from_args_defaults(self):
        """Config uses defaults when not specified."""
        args = MockNamespace(
            e2b_api_key=None,
            e2b_template="base",
            e2b_sandbox_timeout=3600,
            e2b_metadata=None,
            e2b_env=None,
            shell="/bin/bash",
            network=False,
            timeout=120,
        )

        config = E2BBackend.config_from_args(args)  # type: ignore[arg-type]

        assert config.api_key is None  # type: ignore[attr-defined]
        assert config.template == "base"  # type: ignore[attr-defined]
        assert config.metadata is None  # type: ignore[attr-defined]
        assert config.envs is None  # type: ignore[attr-defined]


# --- E2BBackend validation tests ---


class TestE2BBackendValidation:
    """Tests for E2BBackend._validate method."""

    @pytest.mark.asyncio
    async def test_validate_success(self, mock_e2b, default_config):
        """Validation passes when E2B auth succeeds."""
        backend = E2BBackend(default_config)
        await backend.validate()

        # Second call should be cached (no-op)
        await backend.validate()

    @pytest.mark.asyncio
    async def test_validate_auth_failure(self, monkeypatch, default_config):
        """Validation fails when auth fails."""

        class AuthFailSandbox:
            @classmethod
            def list(cls, **kwargs):
                """list() is synchronous and raises on auth failure."""
                raise Exception("401 Unauthorized - invalid API key")

        import kilntainers.backends.e2b as e2b_module

        monkeypatch.setattr(e2b_module, "AsyncSandbox", AuthFailSandbox)
        backend = E2BBackend(default_config)

        with pytest.raises(BackendError) as exc_info:
            await backend.validate()

        assert "validation failed" in str(exc_info.value).lower()
        assert "401" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_validate_connection_failure(self, monkeypatch, default_config):
        """Validation fails when API is unreachable."""

        class ConnectionFailSandbox:
            @classmethod
            def list(cls, **kwargs):
                """list() is synchronous and raises on connection failure."""
                raise Exception("Connection refused")

        import kilntainers.backends.e2b as e2b_module

        monkeypatch.setattr(e2b_module, "AsyncSandbox", ConnectionFailSandbox)
        backend = E2BBackend(default_config)

        with pytest.raises(BackendError) as exc_info:
            await backend.validate()

        assert "validation failed" in str(exc_info.value).lower()
        assert "Connection refused" in str(exc_info.value)


# --- E2BBackend sandbox creation tests ---


class TestE2BBackendSandboxCreation:
    """Tests for E2BBackend._create_sandbox method."""

    @pytest.mark.asyncio
    async def test_create_sandbox_success(self, mock_e2b, default_config):
        """Full sandbox creation sequence succeeds."""
        backend = E2BBackend(default_config)

        # Set up mock for readiness check
        original_create = mock_e2b.create

        async def mock_create(**kwargs):
            sb = mock_e2b("sb-test-123")
            sb.commands.set_response(MockCommandResult(stdout="kilntainers-ready\n"))
            return sb

        mock_e2b.create = mock_create

        sandbox = await backend.create_sandbox()

        assert isinstance(sandbox, E2BSandbox)
        assert sandbox.sandbox_id == "sb-test-123"

        # Restore
        mock_e2b.create = original_create

    @pytest.mark.asyncio
    async def test_create_sandbox_template_not_found(self, monkeypatch, default_config):
        """Creation fails when template not found."""

        class TemplateNotFoundSandbox:
            @classmethod
            async def create(cls, **kwargs):
                raise Exception("template not found: 'bad-template'")

            @classmethod
            def list(cls, **kwargs):
                """list() is synchronous."""
                return []

        import kilntainers.backends.e2b as e2b_module

        monkeypatch.setattr(e2b_module, "AsyncSandbox", TemplateNotFoundSandbox)
        backend = E2BBackend(default_config)

        with pytest.raises(BackendError) as exc_info:
            await backend.create_sandbox()

        assert "failed to create e2b sandbox" in str(exc_info.value).lower()
        assert "template not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_sandbox_readiness_failure(self, monkeypatch, default_config):
        """Readiness check fails, sandbox is cleaned up."""

        class FailingReadinessSandbox:
            sandbox_id = "sb-test-123"

            def __init__(self):
                self.commands = MockCommands()
                self.commands.set_response(MockCommandResult(stdout="wrong output\n"))
                self._killed = False

            async def kill(self):
                self._killed = True
                return True

            @classmethod
            async def create(cls, **kwargs):
                return cls()

            @classmethod
            def list(cls, **kwargs):
                """list() is synchronous."""
                return []

        import kilntainers.backends.e2b as e2b_module

        monkeypatch.setattr(e2b_module, "AsyncSandbox", FailingReadinessSandbox)
        backend = E2BBackend(default_config)

        with pytest.raises(BackendError) as exc_info:
            await backend.create_sandbox()

        assert "readiness check failed" in str(exc_info.value)


# --- E2BBackend tool instructions tests ---


class TestE2BBackendToolInstructions:
    """Tests for E2BBackend.tool_instructions method."""

    def test_tool_instructions_default_template(self, default_config):
        """Default template returns tool description."""
        backend = E2BBackend(default_config)
        instructions = backend.tool_instructions()

        assert instructions is not None
        assert "E2B" in instructions
        assert "bash" in instructions
        assert "120" in instructions  # default timeout

    def test_tool_instructions_custom_template(self):
        """Custom template returns None."""
        config = E2BBackendConfig(template="custom-template")
        backend = E2BBackend(config)
        instructions = backend.tool_instructions()

        assert instructions is None


# --- E2BSandbox command construction tests ---


class TestE2BSandboxCommandConstruction:
    """Tests for E2BSandbox._build_command and _build_run_kwargs."""

    @pytest.fixture
    def sandbox(self):
        """Create a test sandbox."""
        mock_sb = MockAsyncSandbox("sb-test-123")
        return E2BSandbox(e2b_sandbox=mock_sb, shell="/bin/bash")  # type: ignore[arg-type]

    def test_command_mode(self, sandbox):
        """Command mode wraps in shell."""
        request = ExecRequest(command="ls -la", timeout=30, output_limit=2_097_152)
        cmd = sandbox._build_command(request)

        assert cmd == "/bin/bash -c 'ls -la'"

    def test_args_mode(self, sandbox):
        """Args mode joins with quoting (only for special chars)."""
        request = ExecRequest(
            args=["python3", "script.py"], timeout=30, output_limit=2_097_152
        )
        cmd = sandbox._build_command(request)

        # shlex.quote only quotes strings with special characters
        assert cmd == "python3 script.py"

    def test_args_mode_with_spaces(self, sandbox):
        """Args mode quotes args with spaces."""
        request = ExecRequest(
            args=["echo", "hello world"], timeout=30, output_limit=2_097_152
        )
        cmd = sandbox._build_command(request)

        # shlex.quote quotes strings with spaces
        assert cmd == "echo 'hello world'"

    def test_build_run_kwargs_default(self, sandbox):
        """Default kwargs only include timeout."""
        request = ExecRequest(command="echo hello", timeout=60, output_limit=2_097_152)
        kwargs = sandbox._build_run_kwargs(request)

        assert kwargs == {"timeout": 60}

    def test_build_run_kwargs_with_workdir(self, sandbox):
        """Working directory is added to kwargs."""
        request = ExecRequest(
            command="pwd",
            working_directory="/app",
            timeout=60,
            output_limit=2_097_152,
        )
        kwargs = sandbox._build_run_kwargs(request)

        assert kwargs == {"timeout": 60, "cwd": "/app"}


# --- E2BSandbox exec tests ---


class TestE2BSandboxExec:
    """Tests for E2BSandbox.exec method."""

    @pytest.fixture
    def sandbox(self):
        """Create a test sandbox."""
        mock_sb = MockAsyncSandbox("sb-test-123")
        return E2BSandbox(e2b_sandbox=mock_sb, shell="/bin/bash")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_exec_success(self, sandbox):
        """Successful command execution."""
        mock_sb = sandbox._e2b_sandbox
        mock_sb.commands.set_response(
            MockCommandResult(stdout="hello\n", stderr="", exit_code=0)
        )

        request = ExecRequest(command="echo hello", timeout=30, output_limit=2_097_152)
        result = await sandbox.exec(request)

        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.exec_duration_ms >= 0

    @pytest.mark.asyncio
    async def test_exec_failure(self, sandbox):
        """Command fails with non-zero exit."""
        mock_sb = sandbox._e2b_sandbox
        mock_sb.commands.set_response(
            MockCommandResult(stdout="", stderr="error: not found\n", exit_code=1)
        )

        request = ExecRequest(command="false", timeout=30, output_limit=2_097_152)
        result = await sandbox.exec(request)

        assert result.exit_code == 1
        assert "error: not found" in result.stderr

    @pytest.mark.asyncio
    async def test_exec_output_limit(self, sandbox):
        """Output limit exceeded."""
        mock_sb = sandbox._e2b_sandbox
        mock_sb.commands.set_response(
            MockCommandResult(stdout="x" * 10000, stderr="y" * 10000, exit_code=0)
        )

        request = ExecRequest(command="yes", timeout=30, output_limit=1000)
        result = await sandbox.exec(request)

        assert result.exit_code == 1
        assert "output limit exceeded" in result.stderr
        assert result.stdout == ""

    @pytest.mark.asyncio
    async def test_exec_stdin(self, sandbox):
        """Stdin is sent via SDK's native stdin mechanism."""
        mock_sb = sandbox._e2b_sandbox
        mock_sb.commands.set_response(
            MockCommandResult(stdout="file content\n", stderr="", exit_code=0)
        )

        request = ExecRequest(
            command="cat", stdin="file content", timeout=30, output_limit=2_097_152
        )
        result = await sandbox.exec(request)

        assert result.stdout == "file content\n"
        # Verify stdin was sent through SDK, not shell-escaped into command
        assert len(mock_sb.commands.sent_stdin) == 1
        assert mock_sb.commands.sent_stdin[0] == (12345, "file content")
        # Verify command uses head -c for EOF signaling
        assert mock_sb.commands.last_cmd is not None
        stdin_bytes = len("file content".encode("utf-8"))
        assert f"head -c {stdin_bytes}" in mock_sb.commands.last_cmd

    @pytest.mark.asyncio
    async def test_exec_after_stop(self, sandbox):
        """Exec after stop raises SandboxDiedError."""
        sandbox._stopped = True

        request = ExecRequest(command="echo", timeout=30, output_limit=2_097_152)

        with pytest.raises(SandboxDiedError) as exc_info:
            await sandbox.exec(request)

        assert "stopped" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exec_serialization(self, sandbox):
        """Concurrent exec calls are serialized."""
        mock_sb = sandbox._e2b_sandbox
        exec_count = [0]

        async def slow_run(cmd, **kwargs):
            exec_count[0] += 1
            await asyncio.sleep(0.1)
            return MockCommandResult(stdout="done\n", stderr="")

        mock_sb.commands.run = slow_run

        request = ExecRequest(command="echo test", timeout=30, output_limit=2_097_152)

        # Start two exec calls concurrently
        task1 = asyncio.create_task(sandbox.exec(request))
        task2 = asyncio.create_task(sandbox.exec(request))

        # Both should complete
        await task1
        await task2

        assert exec_count[0] == 2

    @pytest.mark.asyncio
    async def test_exec_timeout_exception(self, sandbox):
        """E2B TimeoutException is converted to exit code 124."""
        mock_sb = sandbox._e2b_sandbox

        async def raise_timeout(cmd, **kwargs):
            raise TimeoutException("context deadline exceeded")

        mock_sb.commands.run = raise_timeout

        request = ExecRequest(command="sleep 60", timeout=2, output_limit=2_097_152)
        result = await sandbox.exec(request)

        assert result.exit_code == 124
        assert "timed out" in result.stderr

    @pytest.mark.asyncio
    async def test_exec_command_exit_exception(self, sandbox):
        """E2B CommandExitException is converted to ExecResult."""
        mock_sb = sandbox._e2b_sandbox

        async def raise_exit(cmd, **kwargs):
            raise CommandExitException(
                stdout="output",
                stderr="error message",
                exit_code=42,
                error="",
            )

        mock_sb.commands.run = raise_exit

        request = ExecRequest(command="bad-command", timeout=30, output_limit=2_097_152)
        result = await sandbox.exec(request)

        assert result.exit_code == 42
        assert result.stdout == "output"
        assert "error message" in result.stderr

    @pytest.mark.asyncio
    async def test_exec_command_exit_exception_output_limit(self, sandbox):
        """E2B CommandExitException respects output limit."""
        mock_sb = sandbox._e2b_sandbox

        async def raise_exit(cmd, **kwargs):
            raise CommandExitException(
                stdout="x" * 10000,
                stderr="y" * 10000,
                exit_code=0,
                error="",
            )

        mock_sb.commands.run = raise_exit

        request = ExecRequest(command="big-output", timeout=30, output_limit=1000)
        result = await sandbox.exec(request)

        assert result.exit_code == 1
        assert "output limit exceeded" in result.stderr
        assert result.stdout == ""


# --- E2BSandbox stop tests ---


class TestE2BSandboxStop:
    """Tests for E2BSandbox.stop method."""

    @pytest.fixture
    def sandbox(self):
        """Create a test sandbox."""
        mock_sb = MockAsyncSandbox("sb-test-123")
        return E2BSandbox(e2b_sandbox=mock_sb, shell="/bin/bash")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_stop(self, sandbox):
        """Stop calls kill."""
        mock_sb = sandbox._e2b_sandbox

        await sandbox.stop()

        assert sandbox._stopped
        assert sandbox._stop_requested
        assert mock_sb._killed

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, sandbox):
        """Stop is idempotent."""
        mock_sb = sandbox._e2b_sandbox

        await sandbox.stop()
        await sandbox.stop()
        await sandbox.stop()

        # kill should only be called once
        assert mock_sb._killed


# --- E2BSandbox death detection tests ---


class TestE2BSandboxDeathDetection:
    """Tests for E2BSandbox.wait_for_death method."""

    @pytest.fixture
    def sandbox(self):
        """Create a test sandbox."""
        mock_sb = MockAsyncSandbox("sb-test-123")
        return E2BSandbox(e2b_sandbox=mock_sb, shell="/bin/bash")  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_wait_for_death_blocks_until_cancelled(self, sandbox):
        """wait_for_death blocks forever until cancelled."""
        task = asyncio.create_task(sandbox.wait_for_death())

        # Wait a bit - task should still be running
        await asyncio.sleep(0.1)
        assert not task.done()

        # Cancel the task
        task.cancel()
        await task  # Should complete without error


# --- E2BSandbox sandbox_id tests ---


class TestE2BSandboxSandboxId:
    """Tests for E2BSandbox.sandbox_id property."""

    def test_sandbox_id(self):
        """sandbox_id returns E2B's sandbox_id."""
        mock_sb = MockAsyncSandbox("custom-sandbox-456")
        sandbox = E2BSandbox(e2b_sandbox=mock_sb, shell="/bin/bash")  # type: ignore[arg-type]

        assert sandbox.sandbox_id == "custom-sandbox-456"

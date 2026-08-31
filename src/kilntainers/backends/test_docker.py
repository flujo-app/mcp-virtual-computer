"""Unit tests for Docker backend.

Tests mock subprocess calls to simulate Docker CLI responses without
requiring a real Docker daemon.
"""

import asyncio

import pytest

from kilntainers.backends.base import ExecRequest
from kilntainers.backends.docker import (
    DockerBackend,
    DockerBackendConfig,
    DockerSandbox,
    _DockerSandboxState,
)
from kilntainers.errors import BackendError, SandboxDiedError

# --- Mock subprocess utilities ---


class MockStdin:
    """Mock stdin for subprocess."""

    def __init__(self):
        self.closed = False

    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class MockStreamReader:
    """Mock asyncio.StreamReader for subprocess output."""

    def __init__(self, data: bytes, delay: float = 0, block_forever: bool = False):
        self._data = data
        self._pos = 0
        self._delay = delay
        self._block_forever = block_forever

    async def read(self, n: int = -1) -> bytes:
        if self._block_forever:
            # Block forever until cancelled
            await asyncio.Future()
            return b""  # Never reached
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        if n == -1:
            result = self._data[self._pos :]
            self._pos = len(self._data)
            return result
        result = self._data[self._pos : self._pos + n]
        self._pos += len(result)
        return result


class MockProcess:
    """Mock asyncio.subprocess.Process."""

    def __init__(
        self,
        returncode: int,
        stdout: bytes = b"",
        stderr: bytes = b"",
        stdin_data: bytes | None = None,
        stdout_delay: float = 0,
        stderr_delay: float = 0,
        block_stdout: bool = False,
        block_stderr: bool = False,
    ):
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._stdin_data = stdin_data
        self._stdout_delay = stdout_delay
        self._stderr_delay = stderr_delay
        self.pid = 12345
        self.stdin = MockStdin() if stdin_data else None
        self.stdout = (
            MockStreamReader(stdout, stdout_delay, block_stdout)
            if (stdout or block_stdout)
            else None
        )
        self.stderr = (
            MockStreamReader(stderr, stderr_delay, block_stderr)
            if (stderr or block_stderr)
            else None
        )
        self._killed = False

    async def wait(self):
        return self.returncode

    async def communicate(self, input_data=None):
        return self._stdout, self._stderr

    def kill(self):
        self._killed = True


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Mock asyncio.create_subprocess_exec with configurable responses."""

    responses: list[MockProcess] = []

    async def create_mock(*args, **kwargs):
        if responses:
            return responses.pop(0)
        return MockProcess(0, b"", b"")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

    # Return a function to add responses
    def add_response(*, returncode=0, stdout=b"", stderr=b"", stdin_data=None):
        responses.insert(0, MockProcess(returncode, stdout, stderr, stdin_data))

    return add_response


@pytest.fixture
def default_config():
    """Return a default DockerBackendConfig."""
    return DockerBackendConfig()


# --- DockerBackend tests ---


class TestDockerBackendValidation:
    """Tests for DockerBackend._validate method."""

    @pytest.mark.asyncio
    async def test_validate_success(self, mock_subprocess, default_config):
        """Validation passes when docker info succeeds."""
        mock_subprocess(returncode=0, stdout=b"Docker info output")

        backend = DockerBackend(default_config)
        await backend.validate()

        # Second call should be cached (no-op)
        await backend.validate()

    @pytest.mark.asyncio
    async def test_validate_failure(self, mock_subprocess, default_config):
        """Validation fails when docker info fails."""
        mock_subprocess(returncode=1, stderr=b"Cannot connect to daemon")

        backend = DockerBackend(default_config)

        with pytest.raises(BackendError) as exc_info:
            await backend.validate()

        assert "Cannot connect to" in str(exc_info.value)
        assert "docker" in str(exc_info.value)


class TestDockerBackendImageManagement:
    """Tests for DockerBackend._ensure_image method."""

    @pytest.mark.asyncio
    async def test_image_exists_locally(self, mock_subprocess, default_config):
        """Image is available locally, no pull needed."""
        mock_subprocess(returncode=0, stdout=b'{"Id": "abc123"}')

        backend = DockerBackend(default_config)
        await backend._ensure_image()

        # Should have called image inspect only
        # (mock responses are LIFO, so we can't verify order easily here)

    @pytest.mark.asyncio
    async def test_image_not_local_pull_success(self, monkeypatch, default_config):
        """Image not local, pull succeeds."""

        async def create_mock(*args, **kwargs):
            if "inspect" in args:
                return MockProcess(1, stderr=b"Error: No such image")
            elif "pull" in args:
                return MockProcess(0)
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        backend = DockerBackend(default_config)
        await backend._ensure_image()  # Should not raise

    @pytest.mark.asyncio
    async def test_image_pull_failure(self, monkeypatch, default_config):
        """Image not local, pull fails."""

        async def create_mock(*args, **kwargs):
            if "inspect" in args:
                return MockProcess(1, stderr=b"Error: No such image")
            elif "pull" in args:
                return MockProcess(1, stderr=b"Error: pull access denied")
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        backend = DockerBackend(default_config)

        with pytest.raises(BackendError) as exc_info:
            await backend._ensure_image()

        assert "Failed to pull image" in str(exc_info.value)


class TestDockerBackendSandboxCreation:
    """Tests for DockerBackend._create_sandbox method."""

    @pytest.mark.asyncio
    async def test_create_sandbox_success(self, monkeypatch, default_config):
        """Full sandbox creation sequence succeeds."""

        call_count = [0]
        calls_log = []

        async def create_mock(*args, **kwargs):
            call_count[0] += 1
            calls_log.append(args)

            if "inspect" in args:
                return MockProcess(0, stdout=b'{"Id": "abc123"}')
            elif "run" in args:
                return MockProcess(0, stdout=b"a" * 64)
            elif "exec" in args:
                return MockProcess(0, stdout=b"kilntainers-ready\n")
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        backend = DockerBackend(default_config)
        sandbox = await backend.create_sandbox()

        assert isinstance(sandbox, DockerSandbox)
        assert sandbox.sandbox_id == "a" * 12, (
            f"Expected 'aaaaaaaaaaaa', got '{sandbox.sandbox_id}'. Calls: {calls_log}"
        )

    @pytest.mark.asyncio
    async def test_create_sandbox_readiness_failure(self, monkeypatch, default_config):
        """Readiness check fails, container is cleaned up."""

        call_count = [0]
        stop_called = [False]

        async def create_mock(*args, **kwargs):
            call_count[0] += 1

            if call_count[0] == 1:  # image inspect
                return MockProcess(0, stdout=b'{"Id": "abc123"}')
            elif call_count[0] == 2:  # docker run
                return MockProcess(0, stdout=b"a" * 64)
            elif call_count[0] == 3:  # readiness check - wrong output
                return MockProcess(0, stdout=b"wrong output\n")
            elif "stop" in args:
                stop_called[0] = True
                return MockProcess(0)
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        backend = DockerBackend(default_config)

        with pytest.raises(BackendError) as exc_info:
            await backend.create_sandbox()

        assert "readiness check failed" in str(exc_info.value)
        assert stop_called[0], "Container should be stopped after readiness failure"


class TestDockerBackendToolInstructions:
    """Tests for DockerBackend.tool_instructions method."""

    def test_tool_instructions_default_image(self, default_config):
        """Default image returns tool description."""
        backend = DockerBackend(default_config)
        instructions = backend.tool_instructions()

        assert instructions is not None
        assert "Debian" in instructions
        assert "bash" in instructions
        assert "120" in instructions  # default timeout

    def test_tool_instructions_custom_image(self):
        """Custom image returns None."""
        config = DockerBackendConfig(image="alpine:latest")
        backend = DockerBackend(config)
        instructions = backend.tool_instructions()

        assert instructions is None


class TestDockerBackendEnginePrefix:
    """Tests for DockerBackend._engine_prefix property."""

    def test_engine_prefix_no_host(self, default_config):
        """No host returns engine only."""
        backend = DockerBackend(default_config)
        assert backend._engine_prefix == ["docker"]

    def test_engine_prefix_with_host(self):
        """Host adds -H flag to prefix."""
        config = DockerBackendConfig(host="tcp://remote:2375")
        backend = DockerBackend(config)
        assert backend._engine_prefix == ["docker", "-H", "tcp://remote:2375"]

    def test_engine_prefix_with_custom_engine_and_host(self):
        """Custom engine and host both appear in prefix."""
        config = DockerBackendConfig(engine="podman", host="unix:///custom.sock")
        backend = DockerBackend(config)
        assert backend._engine_prefix == ["podman", "-H", "unix:///custom.sock"]

    @pytest.mark.asyncio
    async def test_run_docker_includes_host(self, monkeypatch):
        """_run_docker passes -H to subprocess when host is configured."""
        captured_args: list[tuple] = []

        async def create_mock(*args, **kwargs):
            captured_args.append(args)
            return MockProcess(0, stdout=b"ok\n")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        config = DockerBackendConfig(host="tcp://remote:2375")
        backend = DockerBackend(config)
        await backend._run_docker("info")

        assert len(captured_args) == 1
        cmd = captured_args[0]
        assert cmd[0] == "docker"
        assert cmd[1] == "-H"
        assert cmd[2] == "tcp://remote:2375"
        assert cmd[3] == "info"

    @pytest.mark.asyncio
    async def test_run_docker_no_host(self, monkeypatch, default_config):
        """_run_docker omits -H when no host is configured."""
        captured_args: list[tuple] = []

        async def create_mock(*args, **kwargs):
            captured_args.append(args)
            return MockProcess(0, stdout=b"ok\n")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        backend = DockerBackend(default_config)
        await backend._run_docker("info")

        assert len(captured_args) == 1
        cmd = captured_args[0]
        assert cmd[0] == "docker"
        assert cmd[1] == "info"
        assert "-H" not in cmd


class TestDockerSandboxEnginePrefix:
    """Tests for DockerSandbox._engine_prefix property."""

    def test_engine_prefix_no_host(self):
        """No host returns engine only."""
        state = _DockerSandboxState(
            engine="docker", host=None, shell="/bin/bash", container_id="a" * 64
        )
        sandbox = DockerSandbox(state)
        assert sandbox._engine_prefix == ["docker"]

    def test_engine_prefix_with_host(self):
        """Host adds -H flag to prefix."""
        state = _DockerSandboxState(
            engine="docker",
            host="tcp://remote:2375",
            shell="/bin/bash",
            container_id="a" * 64,
        )
        sandbox = DockerSandbox(state)
        assert sandbox._engine_prefix == ["docker", "-H", "tcp://remote:2375"]

    def test_build_exec_command_includes_host(self):
        """_build_exec_command includes -H when host is configured."""
        state = _DockerSandboxState(
            engine="docker",
            host="tcp://remote:2375",
            shell="/bin/bash",
            container_id="a" * 64,
        )
        sandbox = DockerSandbox(state)
        request = ExecRequest(command="ls", timeout=30, output_limit=2_097_152)
        cmd = sandbox._build_exec_command(request)

        assert cmd[0] == "docker"
        assert cmd[1] == "-H"
        assert cmd[2] == "tcp://remote:2375"
        assert "exec" in cmd

    def test_build_exec_command_no_host(self):
        """_build_exec_command omits -H when no host is configured."""
        state = _DockerSandboxState(
            engine="docker", host=None, shell="/bin/bash", container_id="a" * 64
        )
        sandbox = DockerSandbox(state)
        request = ExecRequest(command="ls", timeout=30, output_limit=2_097_152)
        cmd = sandbox._build_exec_command(request)

        assert cmd[0] == "docker"
        assert cmd[1] == "exec"
        assert "-H" not in cmd


class TestDockerBackendRunCommand:
    """Tests for DockerBackend._build_run_command method."""

    def test_build_run_command_defaults(self, default_config):
        """Default config produces expected docker run command."""
        backend = DockerBackend(default_config)
        cmd = backend._build_run_command()

        assert cmd[0] == "run"
        assert "-d" in cmd
        assert "--rm" in cmd
        assert "--label" in cmd
        assert "kilntainers=true" in cmd
        assert "--network" in cmd
        assert "none" in cmd
        assert default_config.image in cmd
        assert "tail" in cmd
        assert "-f" in cmd
        assert "/dev/null" in cmd

    def test_build_run_command_network_enabled(self):
        """Network enabled omits --network none."""
        config = DockerBackendConfig(network_enabled=True)
        backend = DockerBackend(config)
        cmd = backend._build_run_command()

        assert "--network" not in cmd
        assert "none" not in cmd

    def test_build_run_command_with_resources(self):
        """CPU and memory limits are added."""
        config = DockerBackendConfig(cpu="1.5", memory="512m")
        backend = DockerBackend(config)
        cmd = backend._build_run_command()

        assert "--cpus" in cmd
        assert "1.5" in cmd
        assert "--memory" in cmd
        assert "512m" in cmd

    def test_build_run_command_with_custom_flags(self):
        """Custom flags are appended."""
        config = DockerBackendConfig(
            docker_run_flags=["--read-only", "--pids-limit=256"]
        )
        backend = DockerBackend(config)
        cmd = backend._build_run_command()

        assert "--read-only" in cmd
        assert "--pids-limit=256" in cmd


# --- DockerSandbox tests ---


class TestDockerSandboxExecCommandConstruction:
    """Tests for DockerSandbox._build_exec_command method."""

    @pytest.fixture
    def sandbox(self, default_config):
        """Create a test sandbox."""
        state = _DockerSandboxState(
            engine="docker", host=None, shell="/bin/bash", container_id="a" * 64
        )
        return DockerSandbox(state)

    def test_command_mode(self, sandbox):
        """Command mode wraps in shell."""
        request = ExecRequest(command="ls -la", timeout=30, output_limit=2_097_152)
        cmd = sandbox._build_exec_command(request)

        assert "docker" in cmd
        assert "exec" in cmd
        assert "a" * 64 in cmd
        assert "/bin/bash" in cmd
        assert "-c" in cmd
        assert "ls -la" in cmd
        assert "-i" not in cmd

    def test_args_mode(self, sandbox):
        """Args mode passes directly."""
        request = ExecRequest(
            args=["python3", "script.py"], timeout=30, output_limit=2_097_152
        )
        cmd = sandbox._build_exec_command(request)

        assert "docker" in cmd
        assert "exec" in cmd
        assert "a" * 64 in cmd
        assert "python3" in cmd
        assert "script.py" in cmd
        assert "/bin/bash" not in cmd
        assert "-c" not in cmd
        assert "-i" not in cmd

    def test_with_stdin(self, sandbox):
        """Stdin adds -i flag."""
        request = ExecRequest(
            command="cat", stdin="hello", timeout=30, output_limit=2_097_152
        )
        cmd = sandbox._build_exec_command(request)

        assert "-i" in cmd

    def test_with_working_directory(self, sandbox):
        """Working directory adds -w flag."""
        request = ExecRequest(
            command="pwd", working_directory="/tmp", timeout=30, output_limit=2_097_152
        )
        cmd = sandbox._build_exec_command(request)

        assert "-w" in cmd
        assert "/tmp" in cmd

    def test_all_options(self, sandbox):
        """All options combined."""
        request = ExecRequest(
            command="make",
            stdin="",
            working_directory="/app",
            timeout=30,
            output_limit=2_097_152,
        )
        cmd = sandbox._build_exec_command(request)

        assert "-i" in cmd
        assert "-w" in cmd
        assert "/app" in cmd
        assert "/bin/bash" in cmd
        assert "-c" in cmd
        assert "make" in cmd


class TestDockerSandboxExec:
    """Tests for DockerSandbox.exec method."""

    @pytest.fixture
    def sandbox(self, default_config):
        """Create a test sandbox."""
        state = _DockerSandboxState(
            engine="docker", host=None, shell="/bin/bash", container_id="a" * 64
        )
        return DockerSandbox(state)

    @pytest.mark.asyncio
    async def test_exec_success(self, monkeypatch, sandbox):
        """Successful command execution."""

        async def create_mock(*args, **kwargs):
            return MockProcess(0, stdout=b"hello\n", stderr=b"")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        request = ExecRequest(command="echo hello", timeout=30, output_limit=2_097_152)
        result = await sandbox.exec(request)

        assert result.stdout == "hello\n"
        assert result.stderr == ""
        assert result.exit_code == 0
        assert result.exec_duration_ms >= 0

    @pytest.mark.asyncio
    async def test_exec_failure(self, monkeypatch, sandbox):
        """Command fails with non-zero exit."""

        async def create_mock(*args, **kwargs):
            if "inspect" in args:
                # Container is still running
                return MockProcess(0, stdout=b"true")
            # Command execution fails
            return MockProcess(1, stdout=b"", stderr=b"error: not found\n")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        request = ExecRequest(command="false", timeout=30, output_limit=2_097_152)
        result = await sandbox.exec(request)

        assert result.exit_code == 1
        assert "error: not found" in result.stderr

    @pytest.mark.asyncio
    async def test_exec_timeout(self, monkeypatch, sandbox):
        """Command times out."""

        async def create_mock(*args, **kwargs):
            # Create a process with blocking stream readers
            proc = MockProcess(0, block_stdout=True, block_stderr=True)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        request = ExecRequest(command="sleep 60", timeout=1, output_limit=2_097_152)
        result = await sandbox.exec(request)

        assert result.exit_code == 124
        assert "timed out" in result.stderr
        assert result.stdout == ""

    @pytest.mark.asyncio
    async def test_exec_output_limit(self, monkeypatch, sandbox):
        """Output limit exceeded."""

        async def create_mock(*args, **kwargs):
            # Create a process that generates lots of output
            proc = MockProcess(0, stdout=b"x" * 10000, stderr=b"y" * 10000)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        request = ExecRequest(command="yes", timeout=30, output_limit=1000)
        result = await sandbox.exec(request)

        assert result.exit_code == 1
        assert "output limit exceeded" in result.stderr
        assert result.stdout == ""

    @pytest.mark.asyncio
    async def test_exec_after_stop(self, sandbox):
        """Exec after stop raises SandboxDiedError."""
        sandbox._stopped = True

        request = ExecRequest(command="echo", timeout=30, output_limit=2_097_152)

        with pytest.raises(SandboxDiedError) as exc_info:
            await sandbox.exec(request)

        assert "stopped" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_exec_serialization(self, monkeypatch, sandbox):
        """Concurrent exec calls are serialized."""

        exec_count = [0]
        in_exec = asyncio.Event()

        async def mock_wait(self):
            exec_count[0] += 1
            in_exec.set()
            # Wait a bit to verify serialization
            await asyncio.sleep(0.1)
            return 0

        async def create_mock(*args, **kwargs):
            proc = MockProcess(0, stdout=b"done\n", stderr=b"")
            proc.wait = mock_wait.__get__(proc, MockProcess)  # type: ignore[method-assign]
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        request = ExecRequest(command="echo test", timeout=30, output_limit=2_097_152)

        # Start two exec calls concurrently
        task1 = asyncio.create_task(sandbox.exec(request))
        task2 = asyncio.create_task(sandbox.exec(request))

        # Wait for first to start
        await in_exec.wait()

        # Both should complete
        await task1
        await task2

        # Due to lock, they should have run serially
        assert exec_count[0] == 2


class TestDockerSandboxStop:
    """Tests for DockerSandbox.stop method."""

    @pytest.fixture
    def sandbox(self, default_config):
        """Create a test sandbox."""
        state = _DockerSandboxState(
            engine="docker", host=None, shell="/bin/bash", container_id="a" * 64
        )
        return DockerSandbox(state)

    @pytest.mark.asyncio
    async def test_stop(self, monkeypatch, sandbox):
        """Stop calls docker stop."""
        stop_called = [False]

        async def create_mock(*args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if "stop" in cmd:
                stop_called[0] = True
                return MockProcess(0)
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        await sandbox.stop()

        assert sandbox._stopped
        assert sandbox._stop_requested
        assert stop_called[0]

    @pytest.mark.asyncio
    async def test_stop_idempotent(self, monkeypatch, sandbox):
        """Stop is idempotent."""
        stop_count = [0]

        async def create_mock(*args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if "stop" in cmd:
                stop_count[0] += 1
                return MockProcess(0)
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        await sandbox.stop()
        await sandbox.stop()
        await sandbox.stop()

        assert stop_count[0] == 1  # Only called once


class TestDockerSandboxDeathDetection:
    """Tests for DockerSandbox.wait_for_death method."""

    @pytest.fixture
    def sandbox(self, default_config):
        """Create a test sandbox."""
        state = _DockerSandboxState(
            engine="docker", host=None, shell="/bin/bash", container_id="a" * 64
        )
        return DockerSandbox(state)

    @pytest.mark.asyncio
    async def test_wait_for_death_unexpected_exit(self, monkeypatch, sandbox):
        """Container exits unexpectedly."""

        async def create_mock(*args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if "wait" in cmd:
                # Container exits immediately
                return MockProcess(0)
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        # Should return when container exits unexpectedly
        await sandbox.wait_for_death()

    @pytest.mark.asyncio
    async def test_wait_for_death_with_stop_requested(self, monkeypatch, sandbox):
        """Container exits after stop is requested."""

        async def create_mock(*args, **kwargs):
            cmd = args[1] if len(args) > 1 else ""
            if "wait" in cmd:
                # Container exits
                return MockProcess(0)
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        sandbox._stop_requested = True

        # Should block forever (we'll cancel it)
        task = asyncio.create_task(sandbox.wait_for_death())
        await asyncio.sleep(0.1)

        # Task should still be running
        assert not task.done()

        # Cancel the task
        task.cancel()
        await task

    @pytest.mark.asyncio
    async def test_wait_for_death_cancellation(self, monkeypatch, sandbox):
        """Task is cancelled before container exits."""

        async def create_mock(*args, **kwargs):
            if "wait" in args:
                # Create a process that completes immediately
                return MockProcess(0)
            return MockProcess(0)

        monkeypatch.setattr(asyncio, "create_subprocess_exec", create_mock)

        # When container exits immediately, wait_for_death should return
        await sandbox.wait_for_death()


class TestDockerSandboxSandboxId:
    """Tests for DockerSandbox.sandbox_id property."""

    def test_sandbox_id(self, default_config):
        """sandbox_id returns first 12 chars of container_id."""
        state = _DockerSandboxState(
            engine="docker",
            host=None,
            shell="/bin/bash",
            container_id="abcd1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
        )
        sandbox = DockerSandbox(state)

        assert sandbox.sandbox_id == "abcd12345678"

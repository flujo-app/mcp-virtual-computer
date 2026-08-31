"""Shared integration tests for Linux container backends (Docker, Podman, Modal).

These tests verify the core behavioral contract of all Linux container backends.
They are parameterized by backend type.
"""

import pytest
import pytest_asyncio

from kilntainers.backends.base import ExecRequest
from kilntainers.backends.e2b import E2BBackend, E2BBackendConfig
from kilntainers.backends.modal import ModalBackend, ModalBackendConfig
from kilntainers.backends.test_docker_integration import get_docker_backend
from kilntainers.backends.test_e2b_e2e import (
    skip_if_e2b_temporarily_unavailable,
    skip_if_no_e2b,
)
from kilntainers.backends.test_modal_integration import _modal_auth_available
from kilntainers.errors import BackendError


@pytest_asyncio.fixture(params=["docker", "podman", "modal", "e2b"], loop_scope="class")
async def backend(request):
    """Fixture to provide a validated backend instance for each supported type."""
    pytest.mark.integration()
    backend_type = request.param

    if backend_type in ["docker", "podman"]:
        return await get_docker_backend(backend_type)

    elif backend_type == "modal":
        if not _modal_auth_available():
            pytest.skip("Modal credentials not configured")

        config = ModalBackendConfig()
        backend = ModalBackend(config)
        try:
            await backend.validate()
        except BackendError as e:
            if "authentication failed" in str(e).lower():
                pytest.skip("Modal authentication failed")
            if "Cannot connect to Modal" in str(e):
                pytest.skip("Cannot connect to Modal API")
            raise
        return backend
    elif backend_type == "e2b":
        skip_if_no_e2b()

        config = E2BBackendConfig()
        backend = E2BBackend(config)
        try:
            await backend.validate()
        except BackendError as e:
            skip_if_e2b_temporarily_unavailable(e)
            pytest.skip("E2B API validation failed")
        return backend

    pytest.fail(f"Unknown backend type: {backend_type}")


@pytest_asyncio.fixture(loop_scope="class")
async def sandbox(backend):
    """Create a real sandbox for testing.

    The sandbox is automatically stopped after the test.
    """
    try:
        sb = await backend.create_sandbox()
    except BackendError as e:
        skip_if_e2b_temporarily_unavailable(e)
        raise
    yield sb
    # Cleanup: stop the sandbox
    await sb.stop()


# --- Shared Lifecycle Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedLifecycle:
    """Tests for sandbox lifecycle operations shared across all backends."""

    async def test_create_sandbox(self, backend):
        """Sandbox creation succeeds."""
        try:
            sb = await backend.create_sandbox()
        except BackendError as e:
            skip_if_e2b_temporarily_unavailable(e)
            raise
        assert sb.sandbox_id is not None
        assert len(sb.sandbox_id) > 0
        await sb.stop()

    async def test_readiness_check(self, sandbox):
        """Sandbox passes readiness check (implicit in creation)."""
        # If this test runs, readiness check passed
        assert sandbox.sandbox_id is not None

    async def test_stop(self, sandbox):
        """Stop terminates the sandbox."""
        await sandbox.stop()
        assert sandbox._stopped

    async def test_stop_idempotent(self, sandbox):
        """Stop can be called multiple times safely."""
        await sandbox.stop()
        await sandbox.stop()
        await sandbox.stop()
        assert sandbox._stopped


# --- Shared Basic Exec Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedBasicExec:
    """Tests for basic command execution shared across all backends."""

    async def test_echo_success(self, sandbox):
        """Simple echo command works."""
        request = ExecRequest(command="echo hello world", timeout=5, output_limit=1024)
        result = await sandbox.exec(request)
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert result.stderr == ""

    async def test_false_fails(self, sandbox):
        """Command that fails returns non-zero exit."""
        request = ExecRequest(command="false", timeout=5, output_limit=1024)
        result = await sandbox.exec(request)
        assert result.exit_code != 0

    async def test_nonexistent_file(self, sandbox):
        """Nonexistent file produces error output."""
        request = ExecRequest(command="ls /nonexistent", timeout=5, output_limit=1024)
        result = await sandbox.exec(request)
        assert result.exit_code != 0
        assert "No such file" in result.stderr


# --- Shared Command vs Args Mode Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedCommandVsArgsMode:
    """Tests for command mode vs args mode shared across all backends."""

    async def test_command_mode_shell_features(self, sandbox):
        """Command mode supports shell features like pipes and redirects."""
        request = ExecRequest(
            command="echo hello | tr a-z A-Z", timeout=5, output_limit=1024
        )
        result = await sandbox.exec(request)
        assert result.exit_code == 0
        assert "HELLO" in result.stdout

    async def test_args_mode_no_shell(self, sandbox):
        """Args mode does not use shell interpretation."""
        # Args mode runs echo directly, treating "|", "tr", etc. as arguments
        request = ExecRequest(
            args=["echo", "hello | tr a-z A-Z"], timeout=5, output_limit=1024
        )
        result = await sandbox.exec(request)
        assert result.exit_code == 0
        assert "hello | tr a-z A-Z" in result.stdout  # Literal output


# --- Shared Working Directory Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedWorkingDirectory:
    """Tests for working_directory parameter shared across all backends."""

    async def test_working_directory(self, sandbox):
        """Working directory changes execution context."""
        request = ExecRequest(
            command="pwd", working_directory="/tmp", timeout=5, output_limit=1024
        )
        result = await sandbox.exec(request)
        assert result.exit_code == 0
        assert "/tmp" in result.stdout

    async def test_working_directory_with_command(self, sandbox):
        """Working directory works with complex commands."""
        request = ExecRequest(
            command="pwd && ls", working_directory="/etc", timeout=5, output_limit=25000
        )
        result = await sandbox.exec(request)
        assert result.exit_code == 0
        assert "/etc" in result.stdout


# --- Shared Stdin Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedStdin:
    """Tests for stdin parameter shared across all backends."""

    async def test_stdin_piping(self, sandbox):
        """Stdin data is piped to command."""
        request = ExecRequest(
            command="cat", stdin="hello from stdin", timeout=5, output_limit=1024
        )
        result = await sandbox.exec(request)
        assert result.exit_code == 0
        assert "hello from stdin" in result.stdout

    async def test_stdin_special_characters(self, sandbox):
        """Stdin handles special characters correctly."""
        special_input = "Hello\nWorld\t!"
        request = ExecRequest(
            command="cat", stdin=special_input, timeout=5, output_limit=1024
        )
        result = await sandbox.exec(request)
        assert result.exit_code == 0
        # Verify newline and tab are preserved in round-trip
        assert "Hello\nWorld" in result.stdout
        assert "\t!" in result.stdout


# --- Shared Filesystem E2E Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedFilesystemE2E:
    """End-to-end tests: filesystem state persists across execs in same sandbox."""

    async def test_stdin_to_file_then_cat(self, sandbox):
        """Write stdin to a file in one exec; read it back in the next."""
        content = "e2e filesystem content\nline two"
        write_request = ExecRequest(
            command="cat > /tmp/e2e_content",
            stdin=content,
            timeout=5,
            output_limit=1024,
        )
        write_result = await sandbox.exec(write_request)
        assert write_result.exit_code == 0

        read_request = ExecRequest(
            command="cat /tmp/e2e_content", timeout=5, output_limit=1024
        )
        read_result = await sandbox.exec(read_request)
        assert read_result.exit_code == 0
        assert read_result.stdout == content

    async def test_mkdir_then_ls(self, sandbox):
        """Create a directory in one exec; list it in the next."""
        dir_path = "/tmp/e2e_testdir"
        mkdir_request = ExecRequest(
            command=f"mkdir {dir_path}", timeout=5, output_limit=1024
        )
        mkdir_result = await sandbox.exec(mkdir_request)
        assert mkdir_result.exit_code == 0

        ls_request = ExecRequest(
            command=f"ls -d {dir_path}", timeout=5, output_limit=1024
        )
        ls_result = await sandbox.exec(ls_request)
        assert ls_result.exit_code == 0
        assert "e2e_testdir" in ls_result.stdout


# --- Shared Output Limit Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedOutputLimit:
    """Tests for output_limit enforcement shared across all backends."""

    async def test_output_limit_exceeded(self, sandbox):
        """Command generating excessive output is terminated."""
        # Emit a ~2k byte string, which should exceed the 1k output limit.
        request = ExecRequest(
            command="head -c 2048 /dev/zero | tr '\\0' 'a'",
            timeout=5,
            output_limit=1024,
        )
        result = await sandbox.exec(request)
        assert result.exit_code != 0
        assert "output limit exceeded" in result.stderr


# --- Shared Stateless Execution Tests ---


@pytest.mark.integration
@pytest.mark.asyncio(loop_scope="class")
class TestSharedStatelessExecution:
    """Tests for state isolation between exec calls shared across all backends."""

    async def test_exports_dont_persist(self, sandbox):
        """Shell variable exports don't persist across calls."""
        # Set a variable
        request1 = ExecRequest(
            command="export MY_VAR=test && echo $MY_VAR",
            timeout=5,
            output_limit=1024,
        )
        result1 = await sandbox.exec(request1)
        assert "test" in result1.stdout

        # Variable should not persist
        request2 = ExecRequest(command="echo $MY_VAR", timeout=5, output_limit=1024)
        result2 = await sandbox.exec(request2)
        assert "test" not in result2.stdout

    async def test_cd_doesnt_persist(self, sandbox):
        """Directory changes don't persist across calls."""
        # Change directory
        request1 = ExecRequest(command="cd /tmp && pwd", timeout=5, output_limit=1024)
        result1 = await sandbox.exec(request1)
        assert "/tmp" in result1.stdout

        # Should be back in original directory
        request2 = ExecRequest(command="pwd", timeout=5, output_limit=1024)
        result2 = await sandbox.exec(request2)
        assert "/tmp" not in result2.stdout

"""Integration tests for GoBusyBox backend with real wasmtime.

These tests require:
1. wasmtime Python package installed
2. busybox.wasm bundled in kilntainers.backends.wasm_data

Tests create real sandboxes and execute actual WASM commands.
Skipped if wasmtime is not installed.
"""

import os
from typing import TYPE_CHECKING

import pytest

from kilntainers.backends.base import ExecRequest
from kilntainers.backends.wasm import GoBusyBoxBackend, WasmBackendConfig
from kilntainers.errors import BackendError

# Check if wasmtime is available
if TYPE_CHECKING:
    import wasmtime  # type: ignore[import-not-found]

    WASMTIME_AVAILABLE = True
else:
    try:
        import wasmtime

        WASMTIME_AVAILABLE = True
    except ImportError:
        wasmtime = None
        WASMTIME_AVAILABLE = False

# Check if busybox.wasm is bundled
try:
    import importlib.resources

    _busybox_ref = (
        importlib.resources.files("kilntainers.backends.wasm_data") / "busybox.wasm"
    )
    # Try to access the file to verify it exists
    _busybox_path = str(_busybox_ref)
    BUSYBOX_AVAILABLE = os.path.isfile(_busybox_path)
except Exception:
    BUSYBOX_AVAILABLE = False


@pytest.mark.skipif(not WASMTIME_AVAILABLE, reason="wasmtime package not installed")
@pytest.mark.skipif(not BUSYBOX_AVAILABLE, reason="busybox.wasm not bundled")
class TestGoBusyBoxIntegration:
    """Integration tests for GoBusyBox with real WASM execution.

    GoBusyBox uses args mode only - no shell. Commands are called directly
    as busybox applets like ["ls", "-la"] or ["grep", "pattern", "file"].
    """

    @pytest.fixture
    async def backend(self):
        """Create a real GoBusyBoxBackend instance."""
        config = WasmBackendConfig(
            wasm_path=_busybox_path,
            shell=None,  # Args mode only
            network_enabled=False,
            max_memory_mib=256,
            fuel=None,
            default_timeout=30,
        )
        backend = GoBusyBoxBackend(config)
        await backend.validate()
        yield backend
        # Cleanup is handled by backend lifecycle

    @pytest.fixture
    async def sandbox(self, backend):
        """Create a real sandbox for testing."""
        sb = await backend._create_sandbox()
        yield sb
        await sb.stop()

    async def test_write_file_from_python_read_with_busybox_cat(self, sandbox):
        """Test writing a file from Python and reading with busybox cat."""
        # Write a test file directly to the sandbox's sandbox directory
        test_file = os.path.join(sandbox._sandbox_dir, "test.txt")
        test_content = "Hello from busybox!"

        with open(test_file, "w") as f:
            f.write(test_content)

        # Read the file using busybox cat (args mode)
        result = await sandbox.exec(
            ExecRequest(
                args=["cat", "test.txt"],
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code == 0
        assert test_content in result.stdout
        assert result.stderr == ""

    async def test_ls_command_lists_directory(self, sandbox):
        """Test that ls command works."""
        # Create some test files from Python
        with open(os.path.join(sandbox._sandbox_dir, "file1.txt"), "w") as f:
            f.write("content1")
        with open(os.path.join(sandbox._sandbox_dir, "file2.txt"), "w") as f:
            f.write("content2")

        # List directory
        result = await sandbox.exec(
            ExecRequest(
                args=["ls", "-la"],
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code == 0
        assert "file1.txt" in result.stdout
        assert "file2.txt" in result.stdout

    async def test_cat_reads_multiple_files(self, sandbox):
        """Test that cat can read multiple files."""
        # Create test files from Python
        with open(os.path.join(sandbox._sandbox_dir, "a.txt"), "w") as f:
            f.write("AAA")
        with open(os.path.join(sandbox._sandbox_dir, "b.txt"), "w") as f:
            f.write("BBB")

        # Read both files
        result = await sandbox.exec(
            ExecRequest(
                args=["cat", "a.txt", "b.txt"],
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code == 0
        assert "AAA" in result.stdout
        assert "BBB" in result.stdout

    async def test_grep_searches_files(self, sandbox):
        """Test that grep command works."""
        # Create test files from Python
        with open(os.path.join(sandbox._sandbox_dir, "test.txt"), "w") as f:
            f.write("hello world\ngoodbye world")

        # Search for "hello"
        result = await sandbox.exec(
            ExecRequest(
                args=["grep", "hello", "test.txt"],
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert "goodbye world" not in result.stdout

    async def test_grep_recursive_search(self, sandbox):
        """Test that grep -r works for recursive search."""
        # Create directory structure from Python
        os.makedirs(os.path.join(sandbox._sandbox_dir, "subdir"), exist_ok=True)
        with open(os.path.join(sandbox._sandbox_dir, "subdir", "file.txt"), "w") as f:
            f.write("wasm is cool")
        with open(os.path.join(sandbox._sandbox_dir, "root.txt"), "w") as f:
            f.write("wasm rules")

        # Recursive search for "wasm"
        result = await sandbox.exec(
            ExecRequest(
                args=["grep", "-r", "wasm", "."],
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code == 0
        assert "wasm is cool" in result.stdout
        assert "wasm rules" in result.stdout

    async def test_stdin_passed_to_command(self, sandbox):
        """Test that stdin is properly passed to the command."""
        test_content = "Data via stdin"

        result = await sandbox.exec(
            ExecRequest(
                args=["cat"],
                timeout=10,
                output_limit=4096,
                stdin=test_content,
            )
        )

        assert result.exit_code == 0
        assert test_content in result.stdout

    async def test_echo_command(self, sandbox):
        """Test that echo command works."""
        result = await sandbox.exec(
            ExecRequest(
                args=["echo", "hello", "world"],
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code == 0
        assert "hello world" in result.stdout

    async def test_head_tail_commands(self, sandbox):
        """Test head and tail commands."""
        # Create a file with multiple lines
        with open(os.path.join(sandbox._sandbox_dir, "lines.txt"), "w") as f:
            for i in range(1, 11):
                f.write(f"line {i}\n")

        # Test head
        head_result = await sandbox.exec(
            ExecRequest(
                args=["head", "-n", "3", "lines.txt"],
                timeout=10,
                output_limit=4096,
            )
        )
        assert head_result.exit_code == 0
        assert "line 1" in head_result.stdout
        assert "line 3" in head_result.stdout

        # Test tail
        tail_result = await sandbox.exec(
            ExecRequest(
                args=["tail", "-n", "3", "lines.txt"],
                timeout=10,
                output_limit=4096,
            )
        )
        assert tail_result.exit_code == 0
        assert "line 8" in tail_result.stdout
        assert "line 10" in tail_result.stdout

    async def test_wc_command(self, sandbox):
        """Test wc (word count) command."""
        with open(os.path.join(sandbox._sandbox_dir, "wc_test.txt"), "w") as f:
            f.write("one two three\nfour five six\n")

        result = await sandbox.exec(
            ExecRequest(
                args=["wc", "-l", "wc_test.txt"],
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code == 0
        assert "2" in result.stdout  # 2 lines

    async def test_mkdir_rmdir_commands(self, sandbox):
        """Test mkdir and rmdir commands."""
        # Create directory
        mkdir_result = await sandbox.exec(
            ExecRequest(
                args=["mkdir", "testdir"],
                timeout=10,
                output_limit=4096,
            )
        )
        assert mkdir_result.exit_code == 0

        # Verify directory exists (via ls)
        ls_result = await sandbox.exec(
            ExecRequest(
                args=["ls"],
                timeout=10,
                output_limit=4096,
            )
        )
        assert "testdir" in ls_result.stdout

        # Remove directory
        rmdir_result = await sandbox.exec(
            ExecRequest(
                args=["rmdir", "testdir"],
                timeout=10,
                output_limit=4096,
            )
        )
        assert rmdir_result.exit_code == 0

    async def test_exit_code_nonzero(self, sandbox):
        """Test that non-zero exit codes are properly reported."""
        result = await sandbox.exec(
            ExecRequest(
                args=["grep", "nonexistentfile.txt"],  # Should fail
                timeout=10,
                output_limit=4096,
            )
        )

        assert result.exit_code != 0

    async def test_filesystem_persists_across_calls(self, sandbox):
        """Test that the filesystem persists across exec calls."""
        # Create a file via Python
        with open(os.path.join(sandbox._sandbox_dir, "persist.txt"), "w") as f:
            f.write("persistent data")

        # Read it back in first exec call
        result1 = await sandbox.exec(
            ExecRequest(
                args=["cat", "persist.txt"],
                timeout=10,
                output_limit=4096,
            )
        )
        assert result1.exit_code == 0
        assert "persistent data" in result1.stdout

        # Read it again in second exec call
        result2 = await sandbox.exec(
            ExecRequest(
                args=["cat", "persist.txt"],
                timeout=10,
                output_limit=4096,
            )
        )
        assert result2.exit_code == 0
        assert "persistent data" in result2.stdout


@pytest.mark.skipif(not WASMTIME_AVAILABLE, reason="wasmtime package not installed")
@pytest.mark.skipif(not BUSYBOX_AVAILABLE, reason="busybox.wasm not bundled")
class TestGoBusyBoxBackendValidation:
    """Tests for GoBusyBox backend validation."""

    async def test_backend_validation_succeeds(self):
        """Test that GoBusyBox backend validates successfully."""
        config = WasmBackendConfig(
            wasm_path=_busybox_path,
            shell=None,  # Args mode only
            network_enabled=False,
            max_memory_mib=256,
            fuel=None,
            default_timeout=30,
        )
        backend = GoBusyBoxBackend(config)

        # Should not raise
        await backend.validate()

        # Verify engine and module were created
        assert backend._engine is not None
        assert backend._module is not None
        assert backend._linker is not None

    async def test_backend_validation_fails_with_missing_wasm(self):
        """Test that validation fails with missing .wasm file."""
        config = WasmBackendConfig(
            wasm_path="/nonexistent/path/to/busybox.wasm",
            shell=None,  # Args mode only
            network_enabled=False,
            max_memory_mib=256,
            fuel=None,
            default_timeout=30,
        )
        backend = GoBusyBoxBackend(config)

        with pytest.raises(BackendError, match="WASM file not found"):
            await backend.validate()

    async def test_tool_instructions_not_none(self):
        """Test that GoBusyBox provides tool instructions."""
        config = WasmBackendConfig(
            wasm_path=_busybox_path,
            shell=None,  # Args mode only
            network_enabled=False,
            max_memory_mib=256,
            fuel=None,
            default_timeout=30,
        )
        backend = GoBusyBoxBackend(config)

        instructions = backend.tool_instructions()
        assert instructions is not None
        assert "busybox" in instructions.lower()
        assert "args" in instructions.lower()

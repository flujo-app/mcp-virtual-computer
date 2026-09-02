"""Integration tests for the CLI.

These tests verify the CLI works correctly without requiring a full
MCP client connection. They test startup, help output, and error handling.
"""

import subprocess
import sys

import pytest

from kilntainers.cli import build_parser


class TestCLIStartup:
    """Tests for CLI startup behavior."""

    @pytest.mark.asyncio
    async def test_cli_startup_does_not_crash_immediately(self):
        """The CLI doesn't crash immediately when started (before MCP connection)."""
        # Run the module with --help to verify basic CLI functionality.
        result = subprocess.run(
            [sys.executable, "-m", "kilntainers", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # --help should exit with code 0 and show help
        assert result.returncode == 0
        assert "mcp-virtual-computer" in result.stdout.lower()

    def test_cli_help_output_complete(self):
        """--help output includes all expected sections."""
        parser = build_parser()
        help_text = parser.format_help()

        # Check for argument group headers
        assert "core options" in help_text.lower()
        assert "tool description" in help_text.lower()
        assert "docker backend options" in help_text.lower()
        assert "fly backend options" in help_text.lower()

        # Check for key arguments
        assert "--backend" in help_text
        assert "--transport" in help_text
        assert "--timeout" in help_text
        assert "--engine" in help_text
        assert "--image" in help_text
        assert "--shell" in help_text
        assert "--network" in help_text
        assert "--no-network" in help_text
        assert "--docker-run-flag" in help_text

        # Check for HTTP-only args (present in help even though they error in stdio mode)
        assert "--host" in help_text
        assert "--port" in help_text
        assert "--session-timeout" in help_text

        # Check for tool description args
        assert "--tool-instruction-override" in help_text
        assert "--extended-tool-instruction" in help_text

    def test_cli_help_shows_defaults(self):
        """--help output shows default values."""
        parser = build_parser()
        help_text = parser.format_help()

        # Check that some defaults are visible
        assert "mcp-virtual-computer-" in help_text
        assert "desktop:bookworm" in help_text
        assert "/bin/bash" in help_text  # default shell
        assert "120" in help_text  # default timeout


class TestCLIValidation:
    """Tests for CLI validation behavior."""

    def test_cli_rejects_removed_backend_argument(self):
        """The CLI rejects a backend option that no longer exists."""
        result = subprocess.run(
            [sys.executable, "-m", "kilntainers", "--backend", "invalid"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Should exit with non-zero code
        assert result.returncode != 0

    def test_cli_rejects_invalid_transport(self):
        """CLI rejects invalid transport choice."""
        result = subprocess.run(
            [sys.executable, "-m", "kilntainers", "--transport", "websocket"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Should exit with non-zero code
        assert result.returncode != 0

    def test_cli_rejects_stdio_with_host(self):
        """CLI rejects --host in stdio mode."""
        result = subprocess.run(
            [sys.executable, "-m", "kilntainers", "--host", "0.0.0.0"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Should exit with code 1 (validation error)
        assert result.returncode == 1
        # Error message should mention the issue
        assert "--host" in result.stderr
        assert "stdio" in result.stderr.lower()

    def test_cli_accepts_stdio_dashboard_port(self):
        """CLI accepts an explicit port for the stdio dashboard companion."""
        args = build_parser().parse_args(["--port", "9090"])

        assert args.port == 9090

    def test_cli_rejects_both_tool_description_params(self):
        """CLI rejects both --tool-instruction-override and --extended-tool-instruction."""
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kilntainers",
                "--tool-instruction-override",
                "custom",
                "--extended-tool-instruction",
                "extra",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Should exit with code 1 (validation error)
        assert result.returncode == 1
        # Error message should mention the issue
        assert "Cannot use both" in result.stderr


class TestCLIErrorOutput:
    """Tests for CLI error message formatting."""

    def test_cli_errors_use_project_prefix(self):
        """CLI error messages use the project command prefix."""
        result = subprocess.run(
            [sys.executable, "-m", "kilntainers", "--host", "0.0.0.0"],
            capture_output=True,
            text=True,
            timeout=10,
        )

        # Should have error prefix
        assert "mcp-virtual-computer: error:" in result.stderr


class TestCLIAcceptsValidArgs:
    """Tests that CLI accepts valid argument combinations."""

    def test_cli_accepts_default_args(self):
        """CLI accepts running with no arguments (for stdio mode)."""
        parser = build_parser()
        args = parser.parse_args([])

        # Should parse successfully
        assert args.transport == "stdio"

    def test_cli_accepts_custom_timeout(self):
        """CLI accepts custom timeout."""
        parser = build_parser()
        args = parser.parse_args(["--timeout", "300"])

        assert args.timeout == 300

    def test_cli_accepts_custom_image(self):
        """CLI accepts custom image."""
        parser = build_parser()
        args = parser.parse_args(["--image", "alpine:latest"])

        assert args.image == "alpine:latest"

    def test_cli_accepts_http_mode_with_all_args(self):
        """CLI accepts HTTP mode with host, port, and session-timeout."""
        parser = build_parser()
        args = parser.parse_args(
            ["--transport", "http", "--host", "0.0.0.0", "--port", "9090"]
        )

        assert args.transport == "http"
        assert args.host == "0.0.0.0"
        assert args.port == 9090

    def test_cli_accepts_docker_run_flags(self):
        """CLI accepts repeatable --docker-run-flag."""
        parser = build_parser()
        args = parser.parse_args(
            ["--docker-run-flag", "readonly", "--docker-run-flag", "pids-limit=256"]
        )

        assert args.docker_run_flags == ["readonly", "pids-limit=256"]

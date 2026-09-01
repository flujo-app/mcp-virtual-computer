"""Tests for CLI argument parsing, config construction, and validation."""

from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from kilntainers.backends.docker import DockerBackendConfig
from kilntainers.cli import (
    _UNSET,
    _async_main,
    _startup_error,
    build_configs,
    build_parser,
    main,
    validate_config,
)
from kilntainers.config import ServerConfig
from kilntainers.errors import BackendError


@pytest.fixture(autouse=True)
def configured_computer_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI startup requires its single computer identity from the environment."""
    monkeypatch.setenv("COMPUTER_ID", "test-computer")


# ================
# Parser Tests
# ================


def test_parser_defaults():
    """Test that parser produces correct defaults with no arguments."""
    parser = build_parser()
    args = parser.parse_args([])

    assert args.transport == "stdio"
    assert args.host is _UNSET
    assert args.port is _UNSET
    assert args.timeout == 120
    assert args.output_limit == 2_097_152
    assert args.session_timeout is _UNSET
    assert args.tool_instruction_override is None
    assert args.extended_tool_instruction is None
    assert args.engine == "docker"
    assert args.image == "mcp-virtual-computer-desktop:bookworm"
    assert args.shell == "/bin/bash"
    assert args.network is True
    assert args.cpu is None
    assert args.memory is None
    assert args.docker_run_flags is None


def test_parser_core_args():
    """Test parser accepts core arguments."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "9090",
            "--timeout",
            "300",
            "--output-limit",
            "1048576",
            "--session-timeout",
            "600",
        ]
    )

    assert args.transport == "http"
    assert args.host == "0.0.0.0"
    assert args.port == 9090
    assert args.timeout == 300
    assert args.output_limit == 1_048_576
    assert args.session_timeout == 600


def test_parser_tool_description_args():
    """Test parser accepts tool description arguments."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--tool-instruction-override",
            "custom description",
            "--extended-tool-instruction",
            "extra info",
        ]
    )

    assert args.tool_instruction_override == "custom description"
    assert args.extended_tool_instruction == "extra info"


def test_parser_docker_args():
    """Test parser accepts Docker backend arguments."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--engine",
            "podman",
            "--image",
            "alpine:latest",
            "--shell",
            "/bin/sh",
            "--network",
            "--cpu",
            "1.5",
            "--memory",
            "512m",
        ]
    )

    assert args.engine == "podman"
    assert args.image == "alpine:latest"
    assert args.shell == "/bin/sh"
    assert args.network is True
    assert args.cpu == "1.5"
    assert args.memory == "512m"


def test_parser_repeatable_docker_run_flag():
    """Test parser accepts repeatable --docker-run-flag."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--docker-run-flag",
            "readonly",
            "--docker-run-flag",
            "pids-limit=256",
        ]
    )

    assert args.docker_run_flags == ["readonly", "pids-limit=256"]


def test_parser_invalid_choices():
    """Test parser rejects invalid choices for backend and transport."""
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--backend", "unknown"])

    with pytest.raises(SystemExit):
        parser.parse_args(["--transport", "websocket"])


def test_parser_invalid_types():
    """Test parser rejects invalid types for numeric arguments."""
    parser = build_parser()

    # Port is an int
    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "abc"])

    # Negative timeout parses (caught by validation)
    args = parser.parse_args(["--timeout", "-5"])
    assert args.timeout == -5


# ================
# Config Construction Tests
# ================


def test_build_configs_default_args(monkeypatch):
    """Test build_configs with default arguments."""
    monkeypatch.delenv("DESKTOP_ENVIRONMENT", raising=False)
    monkeypatch.delenv("EXPOSE_LIFECYCLE_TOOLS", raising=False)
    parser = build_parser()
    args = parser.parse_args([])

    server_config, docker_config = build_configs(args)

    assert isinstance(server_config, ServerConfig)
    assert isinstance(docker_config, DockerBackendConfig)

    # Server config defaults
    assert server_config.transport == "stdio"
    assert server_config.host == "127.0.0.1"
    assert server_config.port == 8435
    assert server_config.default_timeout == 120
    assert server_config.output_limit == 2_097_152
    assert server_config.session_timeout == 300
    assert server_config.tool_instruction_override is None
    assert server_config.extended_tool_instruction is None
    assert server_config.computer_id == "test-computer"
    assert server_config.desktop_environment is False
    assert server_config.expose_lifecycle_tools is False

    # Docker config defaults
    assert docker_config.engine == "docker"
    assert docker_config.image == "mcp-virtual-computer-desktop:bookworm"
    assert docker_config.shell == "/bin/bash"
    assert docker_config.network_enabled is True
    assert docker_config.cpu is None
    assert docker_config.memory is None
    assert docker_config.docker_run_flags == []
    assert docker_config.default_timeout == 120


def test_build_configs_custom_args():
    """Test build_configs with custom arguments."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "9090",
            "--timeout",
            "300",
            "--output-limit",
            "1048576",
            "--session-timeout",
            "600",
            "--engine",
            "podman",
            "--image",
            "alpine:latest",
            "--shell",
            "/bin/sh",
            "--network",
            "--cpu",
            "1.5",
            "--memory",
            "512m",
            "--docker-run-flag",
            "readonly",
        ]
    )

    server_config, docker_config = build_configs(args)
    docker_config = cast(DockerBackendConfig, docker_config)

    # Server config
    assert server_config.transport == "http"
    assert server_config.host == "0.0.0.0"
    assert server_config.port == 9090
    assert server_config.default_timeout == 300
    assert server_config.output_limit == 1_048_576
    assert server_config.session_timeout == 600

    # Docker config
    assert docker_config.engine == "podman"
    assert docker_config.image == "alpine:latest"
    assert docker_config.shell == "/bin/sh"
    assert docker_config.network_enabled is True
    assert docker_config.cpu == "1.5"
    assert docker_config.memory == "512m"
    assert docker_config.docker_run_flags == ["readonly"]
    assert docker_config.default_timeout == 300


def test_build_configs_timeout_in_both_configs():
    """Test that --timeout appears in both server and docker configs."""
    parser = build_parser()
    args = parser.parse_args(["--timeout", "300"])

    server_config, docker_config = build_configs(args)
    docker_config = cast(DockerBackendConfig, docker_config)

    assert server_config.default_timeout == 300
    assert docker_config.default_timeout == 300


@pytest.mark.parametrize("value", ["true", "TRUE", " True ", "1", "yes", "on"])
def test_build_configs_enables_desktop_from_environment(monkeypatch, value: str):
    monkeypatch.setenv("DESKTOP_ENVIRONMENT", value)
    parser = build_parser()

    server_config, _backend_config = build_configs(parser.parse_args([]))

    assert server_config.desktop_environment is True


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_build_configs_disables_desktop_from_environment(monkeypatch, value: str):
    monkeypatch.setenv("DESKTOP_ENVIRONMENT", value)
    parser = build_parser()

    server_config, _backend_config = build_configs(parser.parse_args([]))

    assert server_config.desktop_environment is False


def test_build_configs_rejects_invalid_desktop_environment(monkeypatch) -> None:
    monkeypatch.setenv("DESKTOP_ENVIRONMENT", "sometimes")
    with pytest.raises(ValueError, match="DESKTOP_ENVIRONMENT"):
        build_configs(build_parser().parse_args([]))


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_build_configs_exposes_lifecycle_tools(monkeypatch, value: str) -> None:
    monkeypatch.setenv("EXPOSE_LIFECYCLE_TOOLS", value)

    server_config, _backend_config = build_configs(build_parser().parse_args([]))

    assert server_config.expose_lifecycle_tools is True


def test_build_configs_rejects_invalid_lifecycle_tools_flag(monkeypatch) -> None:
    monkeypatch.setenv("EXPOSE_LIFECYCLE_TOOLS", "sometimes")
    with pytest.raises(ValueError, match="EXPOSE_LIFECYCLE_TOOLS"):
        build_configs(build_parser().parse_args([]))


@pytest.mark.parametrize("value", ["false", "0", "no", "off"])
def test_build_configs_disables_network_from_environment(
    monkeypatch, value: str
) -> None:
    monkeypatch.setenv("NETWORK_ACCESS", value)
    parser = build_parser()

    server_config, backend_config = build_configs(parser.parse_args([]))

    assert server_config.network_access is False
    assert cast(DockerBackendConfig, backend_config).network_enabled is False


def test_build_configs_network_environment_overrides_cli(monkeypatch) -> None:
    monkeypatch.setenv("NETWORK_ACCESS", "true")

    server_config, backend_config = build_configs(
        build_parser().parse_args(["--no-network"])
    )

    assert server_config.network_access is True
    assert cast(DockerBackendConfig, backend_config).network_enabled is True


def test_build_configs_docker_run_flags_not_provided():
    """Test that docker_run_flags is empty list when not provided."""
    parser = build_parser()
    args = parser.parse_args([])

    _server_config, docker_config = build_configs(args)
    docker_config = cast(DockerBackendConfig, docker_config)

    assert docker_config.docker_run_flags == []


def test_build_configs_network_flag():
    """Test that --network flag sets network_enabled to True."""
    parser = build_parser()
    args = parser.parse_args(["--network"])

    _server_config, docker_config = build_configs(args)
    docker_config = cast(DockerBackendConfig, docker_config)

    assert docker_config.network_enabled is True


def test_build_configs_no_network_flag():
    """Test that --no-network explicitly disables network access."""
    parser = build_parser()
    args = parser.parse_args(["--no-network"])

    _server_config, docker_config = build_configs(args)
    docker_config = cast(DockerBackendConfig, docker_config)

    assert docker_config.network_enabled is False


# ================
# Validation Tests
# ================


def test_validate_config_stdio_mode_with_host():
    """Test that --host in stdio mode causes startup error."""
    parser = build_parser()
    args = parser.parse_args(["--host", "0.0.0.0"])
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_stdio_mode_with_port():
    """Test that --port in stdio mode causes startup error."""
    parser = build_parser()
    args = parser.parse_args(["--port", "9090"])
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_stdio_mode_with_session_timeout():
    """Test that --session-timeout in stdio mode causes startup error."""
    parser = build_parser()
    args = parser.parse_args(["--session-timeout", "600"])
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_http_mode_no_error():
    """Test that HTTP args are valid in HTTP mode."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--port",
            "9090",
            "--allow-unauthenticated-http",
        ]
    )
    server_config, _docker_config = build_configs(args)

    # Should not raise
    validate_config(server_config)


def test_validate_config_public_http_requires_auth_or_explicit_override():
    parser = build_parser()
    args = parser.parse_args(["--transport", "http", "--host", "0.0.0.0"])
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_public_http_accepts_bearer_token():
    parser = build_parser()
    args = parser.parse_args(
        [
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
            "--auth-token",
            "test-secret",
        ]
    )
    server_config, _docker_config = build_configs(args)

    validate_config(server_config)
    assert server_config.auth_token == "test-secret"


def test_validate_config_both_tool_description_params():
    """Test that both tool description params cause startup error."""
    parser = build_parser()
    args = parser.parse_args(
        [
            "--tool-instruction-override",
            "custom",
            "--extended-tool-instruction",
            "extra",
        ]
    )
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_override_only():
    """Test that only override is allowed."""
    parser = build_parser()
    args = parser.parse_args(["--tool-instruction-override", "custom"])
    server_config, _docker_config = build_configs(args)

    # Should not raise
    validate_config(server_config)


def test_validate_config_extended_only():
    """Test that only extended is allowed."""
    parser = build_parser()
    args = parser.parse_args(["--extended-tool-instruction", "extra"])
    server_config, _docker_config = build_configs(args)

    # Should not raise
    validate_config(server_config)


def test_validate_config_neither_tool_description():
    """Test that neither tool description param is allowed."""
    parser = build_parser()
    args = parser.parse_args([])
    server_config, _docker_config = build_configs(args)

    # Should not raise
    validate_config(server_config)


def test_validate_config_timeout_zero():
    """Test that timeout=0 causes startup error."""
    parser = build_parser()
    args = parser.parse_args(["--timeout", "0"])
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_timeout_negative():
    """Test that negative timeout causes startup error."""
    parser = build_parser()
    args = parser.parse_args(["--timeout", "-1"])
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_timeout_minimum_valid():
    """Test that timeout=1 is valid (minimum)."""
    parser = build_parser()
    args = parser.parse_args(["--timeout", "1"])
    server_config, _docker_config = build_configs(args)

    # Should not raise
    validate_config(server_config)


def test_validate_config_output_limit_zero():
    """Test that output_limit=0 causes startup error."""
    parser = build_parser()
    args = parser.parse_args(["--output-limit", "0"])
    server_config, _docker_config = build_configs(args)

    with pytest.raises(SystemExit) as exc_info:
        validate_config(server_config)

    assert exc_info.value.code == 1


def test_validate_config_output_limit_minimum_valid():
    """Test that output_limit=1 is valid (minimum)."""
    parser = build_parser()
    args = parser.parse_args(["--output-limit", "1"])
    server_config, _docker_config = build_configs(args)

    # Should not raise
    validate_config(server_config)


# ================
# Startup Error Tests
# ================


def test_startup_error_exits_with_code_1(capsys):
    """Test that _startup_error writes to stderr and exits with code 1."""
    with pytest.raises(SystemExit) as exc_info:
        _startup_error("test error message")

    assert exc_info.value.code == 1

    captured = capsys.readouterr()
    assert "mcp-virtual-computer: error: test error message" in captured.err
    assert captured.out == ""


# ================
# Startup Flow Tests
# ================


@pytest.mark.asyncio
async def test_async_main_server_creation_fails():
    """Test that server creation failure causes startup error."""
    server_config = ServerConfig()
    docker_config = DockerBackendConfig()

    mock_backend = MagicMock()

    with (
        patch("kilntainers.cli.get_backend_class") as mock_get_backend,
        patch("kilntainers.cli.create_server") as mock_create_server,
    ):
        mock_get_backend.return_value = lambda _: mock_backend
        mock_create_server.side_effect = BackendError("No tool instructions")

        with pytest.raises(SystemExit) as exc_info:
            await _async_main(server_config, docker_config, "docker")

    assert exc_info.value.code == 1


@pytest.mark.asyncio
async def test_async_main_successful_startup():
    """Test successful startup flow."""
    server_config = ServerConfig()
    docker_config = DockerBackendConfig()

    mock_backend = MagicMock()

    mock_mcp = MagicMock()
    mock_mcp.run = MagicMock()

    with (
        patch("kilntainers.cli.get_backend_class") as mock_get_backend,
        patch("kilntainers.cli.create_server") as mock_create_server,
    ):
        mock_get_backend.return_value = lambda _: mock_backend
        mock_create_server.return_value = mock_mcp

        await _async_main(server_config, docker_config, "docker")

        # Verify server was created
        mock_create_server.assert_called_once_with(mock_backend, server_config)

        # Verify mcp.run was called with correct transport (keyword arg)
        mock_mcp.run.assert_called_once_with(transport="stdio")


@pytest.mark.asyncio
async def test_async_main_transport_mapping():
    """Test that CLI transport maps to correct FastMCP transport string."""
    server_config = ServerConfig(transport="http")
    docker_config = DockerBackendConfig()

    mock_backend = MagicMock()

    mock_mcp = MagicMock()
    mock_mcp.run = MagicMock()

    with (
        patch("kilntainers.cli.get_backend_class") as mock_get_backend,
        patch("kilntainers.cli.create_server") as mock_create_server,
    ):
        mock_get_backend.return_value = lambda _: mock_backend
        mock_create_server.return_value = mock_mcp

        await _async_main(server_config, docker_config, "docker")

        # Verify transport mapping (keyword arg)
        mock_mcp.run.assert_called_once_with(transport="streamable-http")


# ================
# Main Entry Point Tests
# ================


def test_main_keyboard_interrupt():
    """Test that main() handles KeyboardInterrupt gracefully."""
    mock_backend = MagicMock()

    mock_mcp = MagicMock()
    mock_mcp.run = MagicMock(side_effect=KeyboardInterrupt())

    with (
        patch("kilntainers.cli.build_parser") as mock_parser,
        patch("kilntainers.cli.build_configs") as mock_build_configs,
        patch("kilntainers.cli.validate_config"),
        patch("kilntainers.cli.get_backend_class") as mock_get_backend,
        patch("kilntainers.cli.create_server") as mock_create_server,
    ):
        mock_args = MagicMock()
        mock_parser.return_value.parse_args.return_value = mock_args
        mock_build_configs.return_value = (ServerConfig(), DockerBackendConfig())
        mock_backend_class = MagicMock(return_value=mock_backend)
        mock_get_backend.return_value = mock_backend_class
        mock_create_server.return_value = mock_mcp

        # Should not raise
        main()

        mock_backend_class.prepare_runtime.assert_called_once_with()


# ================
# Integration Test: Help Output
# ================


def test_help_output_structure():
    """Test that --help output is organized by argument groups."""
    parser = build_parser()
    help_text = parser.format_help()

    # Check for argument group headers
    assert "core options" in help_text.lower()
    assert "tool description" in help_text.lower()
    assert "docker backend options" in help_text.lower()

    # Check for key arguments
    assert "--backend" not in help_text
    assert "--transport" in help_text
    assert "--timeout" in help_text
    assert "--engine" in help_text
    assert "--image" in help_text
    assert "--docker-run-flag" in help_text


def test_only_docker_backend_is_exposed():
    from kilntainers.backends import get_available_backend_names, get_backend_class

    assert get_available_backend_names() == ["docker"]
    with pytest.raises(KeyError, match="Available backends: docker"):
        get_backend_class("modal")

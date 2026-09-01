"""Tests for configuration dataclasses."""

from dataclasses import FrozenInstanceError

import pytest

from kilntainers.backends.docker import DockerBackendConfig
from kilntainers.config import ServerConfig

# --- ServerConfig Tests ---


class TestServerConfig:
    """Test ServerConfig dataclass."""

    def test_defaults(self, monkeypatch) -> None:
        """Default construction should produce expected values."""
        monkeypatch.delenv("DESKTOP_ENVIRONMENT", raising=False)
        config = ServerConfig()
        assert config.transport == "stdio"
        assert config.host == "127.0.0.1"
        assert config.port == 8435
        assert config.default_timeout == 120
        assert config.output_limit == 2_097_152
        assert config.tool_instruction_override is None
        assert config.extended_tool_instruction is None
        assert config.computer_id == "virtual-computer"
        assert config.desktop_environment is False
        assert config.network_access is True
        assert config.workspace_directory == "/workspace"
        assert config.session_timeout == 300

    def test_custom_values(self) -> None:
        """Custom values should be stored correctly."""
        config = ServerConfig(
            transport="http",
            host="0.0.0.0",
            port=9000,
            default_timeout=300,
            output_limit=1_048_576,
            tool_instruction_override="custom",
            extended_tool_instruction="extended",
            computer_id="studio-computer",
            desktop_environment=True,
            network_access=False,
            session_timeout=600,
        )
        assert config.transport == "http"
        assert config.host == "0.0.0.0"
        assert config.port == 9000
        assert config.default_timeout == 300
        assert config.output_limit == 1_048_576
        assert config.tool_instruction_override == "custom"
        assert config.extended_tool_instruction == "extended"
        assert config.computer_id == "studio-computer"
        assert config.desktop_environment is True
        assert config.network_access is False
        assert config.session_timeout == 600

    def test_frozen_immutable(self) -> None:
        """ServerConfig should be frozen (immutable)."""
        config = ServerConfig()
        with pytest.raises(FrozenInstanceError):
            config.transport = "http"  # type: ignore[misc]

    def test_kw_only_enforced(self) -> None:
        """Positional arguments should not be allowed."""
        with pytest.raises(TypeError):
            ServerConfig("stdio", "127.0.0.1")  # type: ignore


# --- DockerBackendConfig Tests ---


class TestDockerBackendConfig:
    """Test DockerBackendConfig dataclass."""

    def test_defaults(self) -> None:
        """Default construction should produce expected values."""
        config = DockerBackendConfig()
        assert config.engine == "docker"
        assert config.image == "debian:bookworm-slim"
        assert config.shell == "/bin/bash"
        assert config.network_enabled is True
        assert config.cpu is None
        assert config.memory is None
        assert config.docker_run_flags == []
        assert config.default_timeout == 120

    def test_custom_values(self) -> None:
        """Custom values should be stored correctly."""
        config = DockerBackendConfig(
            engine="podman",
            image="alpine:latest",
            shell="/bin/sh",
            network_enabled=True,
            cpu="1.5",
            memory="512m",
            docker_run_flags=["--read-only", "--pids-limit=256"],
            default_timeout=300,
        )
        assert config.engine == "podman"
        assert config.image == "alpine:latest"
        assert config.shell == "/bin/sh"
        assert config.network_enabled is True
        assert config.cpu == "1.5"
        assert config.memory == "512m"
        assert config.docker_run_flags == ["--read-only", "--pids-limit=256"]
        assert config.default_timeout == 300

    def test_frozen_immutable(self) -> None:
        """DockerBackendConfig should be frozen (immutable)."""
        config = DockerBackendConfig()
        with pytest.raises(FrozenInstanceError):
            config.image = "alpine:latest"  # type: ignore[misc]

    def test_kw_only_enforced(self) -> None:
        """Positional arguments should not be allowed."""
        with pytest.raises(TypeError):
            DockerBackendConfig("docker", "debian:bookworm-slim")  # type: ignore

    def test_docker_run_flags_default_is_empty_list(self) -> None:
        """docker_run_flags default should be an empty list (not None)."""
        config = DockerBackendConfig()
        assert config.docker_run_flags == []
        assert isinstance(config.docker_run_flags, list)

    def test_docker_run_flags_is_mutable_list(self) -> None:
        """The list inside the frozen dataclass should still be mutable."""
        # This is a known dataclass behavior with frozen=True and mutable defaults
        # The config reference itself is frozen, but the list contents can change
        config = DockerBackendConfig(docker_run_flags=["--flag1"])
        # Can modify the list
        config.docker_run_flags.append("--flag2")
        assert config.docker_run_flags == ["--flag1", "--flag2"]

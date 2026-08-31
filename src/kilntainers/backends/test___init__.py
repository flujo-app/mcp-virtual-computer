"""Tests for backend registry."""

import pytest

from kilntainers.backends import (
    get_available_backend_names,
    get_backend_class,
)
from kilntainers.backends.docker import DockerBackend


class TestBackendRegistry:
    """Test backend registry and lookup function."""

    def test_registry_contains_docker(self) -> None:
        """Registry should contain Docker backend."""
        available = get_available_backend_names()
        assert "docker" in available

    def test_get_backend_class_docker(self) -> None:
        """get_backend_class should return DockerBackend for 'docker'."""
        backend_class = get_backend_class("docker")
        assert backend_class == DockerBackend

    def test_get_backend_class_unknown_raises(self) -> None:
        """get_backend_class should raise KeyError for unknown backend."""
        with pytest.raises(KeyError, match=r"Unknown backend.*'unknown'"):
            get_backend_class("unknown")

    def test_get_backend_class_error_message_includes_available(self) -> None:
        """Error message should include available backends."""
        with pytest.raises(KeyError) as exc_info:
            get_backend_class("unknown")
        error_msg = str(exc_info.value)
        assert "Available backends:" in error_msg
        # At least docker should be available
        assert "docker" in error_msg

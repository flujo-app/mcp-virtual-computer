"""Tests for exception hierarchy."""

import pytest

from kilntainers.errors import BackendError, KilntainersError, SandboxDiedError


class TestKilntainersError:
    def test_is_exception_subclass(self) -> None:
        """KilntainersError should be an Exception subclass."""
        assert issubclass(KilntainersError, Exception)

    def test_message_preserved(self) -> None:
        """Exception message should be preserved."""
        msg = "test error message"
        e = KilntainersError(msg)
        assert str(e) == msg


class TestBackendError:
    def test_inherits_from_kilntainers_error(self) -> None:
        """BackendError should be a KilntainersError subclass."""
        assert issubclass(BackendError, KilntainersError)

    def test_not_sandbox_died_error(self) -> None:
        """BackendError should not be a SandboxDiedError subclass."""
        assert not issubclass(BackendError, SandboxDiedError)

    def test_message_preserved(self) -> None:
        """Exception message should be preserved."""
        msg = "backend failed"
        e = BackendError(msg)
        assert str(e) == msg


class TestSandboxDiedError:
    def test_inherits_from_kilntainers_error(self) -> None:
        """SandboxDiedError should be a KilntainersError subclass."""
        assert issubclass(SandboxDiedError, KilntainersError)

    def test_not_backend_error(self) -> None:
        """SandboxDiedError should not be a BackendError subclass."""
        assert not issubclass(SandboxDiedError, BackendError)

    def test_message_preserved(self) -> None:
        """Exception message should be preserved."""
        msg = "sandbox died"
        e = SandboxDiedError(msg)
        assert str(e) == msg


class TestExceptionHierarchy:
    def test_catch_base_catches_all(self) -> None:
        """Catching KilntainersError should catch both backend errors."""
        errors = [
            BackendError("backend error"),
            SandboxDiedError("sandbox died"),
        ]

        for e in errors:
            with pytest.raises(KilntainersError):
                raise e

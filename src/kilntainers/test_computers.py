"""Tests for named computer registry and lifecycle semantics."""

import re

import pytest

from kilntainers.backends.test_utils import MockBackend, MockSandbox
from kilntainers.computers import (
    ComputerRegistry,
    random_computer_id,
    validate_computer_id,
)
from kilntainers.config import BackendConfig
from kilntainers.errors import BackendError
from kilntainers.server import SessionContext


def test_random_computer_id_is_provider_safe() -> None:
    values = {random_computer_id() for _ in range(100)}
    assert len(values) == 100
    assert all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", value) for value in values
    )


@pytest.mark.parametrize(
    "value",
    ["", "UPPER", "with space", "-leading", "trailing-", "a" * 64, "under_score"],
)
def test_validate_computer_id_rejects_invalid_slug(value: str) -> None:
    with pytest.raises(BackendError, match="computer_id"):
        validate_computer_id(value)


@pytest.mark.asyncio
async def test_temporary_computer_is_stopped_after_last_owner_releases() -> None:
    backend = MockBackend(BackendConfig())
    registry = ComputerRegistry(backend)
    computer_id, sandbox = await registry.acquire(
        "short-lived",
        temporary=True,
        add_owner=True,
    )
    assert computer_id == "short-lived"
    assert sandbox.computer_id == "short-lived"
    assert sandbox.temporary is True

    await registry.release(computer_id)

    assert isinstance(sandbox, MockSandbox)
    assert sandbox.is_stopped()
    assert await registry.list() == []


@pytest.mark.asyncio
async def test_permanent_computer_survives_session_release() -> None:
    backend = MockBackend(BackendConfig())
    registry = ComputerRegistry(backend)
    computer_id, sandbox = await registry.acquire(
        "long-lived",
        temporary=False,
        add_owner=True,
    )
    await registry.release(computer_id)

    assert isinstance(sandbox, MockSandbox)
    assert not sandbox.is_stopped()
    assert sandbox.temporary is False
    inventory = await registry.list()
    assert inventory[0].computer_id == "long-lived"
    assert inventory[0].temporary is False


@pytest.mark.asyncio
async def test_session_reuses_only_configured_persistent_computer() -> None:
    backend = MockBackend(BackendConfig())
    session = SessionContext(
        backend=backend,
        transport="http",
        computer_id="named-one",
    )

    default_one = await session.get_or_create_sandbox()
    default_two = await session.get_or_create_sandbox()
    assert default_one is default_two
    assert default_one.computer_id == "named-one"
    assert default_one.temporary is False
    assert backend.create_count == 1
    with pytest.raises(BackendError, match="one computer selected by COMPUTER_ID"):
        await session.get_or_create_sandbox("other-computer")
    with pytest.raises(BackendError, match="Temporary computers are disabled"):
        await session.get_or_create_sandbox("named-one", temporary=True)
    await session.cleanup()


@pytest.mark.asyncio
async def test_registry_refreshes_provider_owned_state() -> None:
    class RefreshingBackend(MockBackend):
        def __init__(self) -> None:
            super().__init__(BackendConfig())
            self.replacement = MockSandbox(sandbox_id="refreshed")
            self.refresh_count = 0

        async def refresh_sandbox(self, computer_id, sandbox):
            assert computer_id == "shared"
            self.refresh_count += 1
            return self.replacement

    backend = RefreshingBackend()
    registry = ComputerRegistry(backend)
    _, original = await registry.acquire(
        "shared",
        temporary=False,
        add_owner=True,
    )

    refreshed = await registry.refresh("shared")

    assert refreshed is backend.replacement
    assert refreshed is not original
    assert await registry.get_owned("shared") is refreshed
    assert backend.refresh_count == 1


@pytest.mark.asyncio
async def test_existing_lifecycle_mode_cannot_be_silently_changed() -> None:
    backend = MockBackend(BackendConfig())
    registry = ComputerRegistry(backend)
    await registry.acquire("fixed", temporary=False, add_owner=False)

    with pytest.raises(BackendError, match="lifecycle mode cannot be changed"):
        await registry.acquire("fixed", temporary=True, add_owner=False)


@pytest.mark.asyncio
async def test_generic_factory_reset_replaces_writable_sandbox() -> None:
    backend = MockBackend(BackendConfig())
    registry = ComputerRegistry(backend)
    _, original = await registry.acquire(
        "reset-me",
        temporary=False,
        add_owner=False,
    )

    replacement = await registry.factory_reset("reset-me")

    assert replacement is not original
    assert isinstance(original, MockSandbox)
    assert original.is_stopped()
    assert replacement.computer_id == "reset-me"
    assert replacement.temporary is False

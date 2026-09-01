"""Named computer registry shared by MCP sessions and dashboard tools."""

import asyncio
import os
import re
import secrets
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, BinaryIO

from kilntainers.backends.base import Backend, ComputerInfo, Sandbox
from kilntainers.errors import BackendError

_COMPUTER_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_ADJECTIVES = (
    "amber",
    "brisk",
    "calm",
    "clever",
    "coral",
    "crisp",
    "gentle",
    "lucky",
    "quiet",
    "rapid",
    "silver",
    "steady",
)
_NOUNS = (
    "badger",
    "comet",
    "falcon",
    "gecko",
    "heron",
    "lynx",
    "otter",
    "panda",
    "raven",
    "tiger",
    "whale",
    "wolf",
)


class _ProcessFileLock:
    """Small cross-platform advisory lock shared by local MCP processes."""

    def __init__(self, computer_id: str, timeout: float = 120.0) -> None:
        self.computer_id = computer_id
        self.path = Path(tempfile.gettempdir()) / (
            f"mcp-virtual-computer-{computer_id}.lock"
        )
        self.timeout = timeout
        self.handle: BinaryIO | None = None

    def acquire(self) -> None:
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - exercised by Linux CI
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.handle = handle
                return
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise BackendError(
                        f"Timed out waiting to update computer '{self.computer_id}'."
                    )
                time.sleep(0.05)

    def release(self) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised by Linux CI
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@asynccontextmanager
async def _computer_process_lock(computer_id: str) -> AsyncIterator[None]:
    lock = _ProcessFileLock(computer_id)
    await asyncio.to_thread(lock.acquire)
    try:
        yield
    finally:
        await asyncio.to_thread(lock.release)


def random_computer_id() -> str:
    """Generate a readable, collision-resistant provider-safe slug."""
    return (
        f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_NOUNS)}-{secrets.token_hex(2)}"
    )


def validate_computer_id(computer_id: str) -> str:
    """Validate and return a stable computer ID.

    IDs intentionally use a conservative lowercase Docker-safe slug format.
    """
    value = computer_id.strip()
    if not _COMPUTER_ID_RE.fullmatch(value):
        raise BackendError(
            "computer_id must be 1-63 lowercase letters, numbers, or hyphens; "
            "it must start and end with a letter or number."
        )
    return value


@dataclass(slots=True)
class _ComputerRecord:
    sandbox: Sandbox
    temporary: bool
    owners: int = 0


class ComputerRegistry:
    """Coordinate named sandboxes across MCP sessions in one server process.

    The Docker backend also discovers computers provider-side, so permanent
    records survive a server restart and can be reattached by ID.
    """

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._records: dict[str, _ComputerRecord] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _tag(sandbox: Sandbox, computer_id: str, temporary: bool) -> Sandbox:
        """Apply registry metadata used by legacy Sandbox base properties."""
        setattr(sandbox, "_managed_computer_id", computer_id)
        setattr(sandbox, "_managed_temporary", temporary)
        return sandbox

    async def acquire(
        self,
        computer_id: str | None,
        *,
        temporary: bool,
        add_owner: bool,
    ) -> tuple[str, Sandbox]:
        """Attach to or create a computer and optionally add an owner ref."""
        requested_id = (
            random_computer_id()
            if computer_id is None
            else validate_computer_id(computer_id)
        )

        async with self._lock:
            record = self._records.get(requested_id)
            if record is not None:
                if record.temporary != temporary:
                    raise BackendError(
                        f"Computer '{requested_id}' already exists with "
                        f"temporary={str(record.temporary).lower()}; lifecycle "
                        "mode cannot be changed without a factory reset or delete."
                    )
                if add_owner:
                    record.owners += 1
                return requested_id, record.sandbox

            # Separate stdio/HTTP MCP processes can share one persistent Docker
            # computer. Serialize provider discovery and creation so a cold
            # start cannot race into duplicate creates or conflicting startup
            # mutations.
            async with _computer_process_lock(requested_id):
                sandbox = await self.backend.attach_sandbox(requested_id)
                if sandbox is not None:
                    actual_temporary = sandbox.temporary
                    if actual_temporary != temporary:
                        raise BackendError(
                            f"Computer '{requested_id}' already exists with "
                            f"temporary={str(actual_temporary).lower()}; requested "
                            f"temporary={str(temporary).lower()}."
                        )
                else:
                    sandbox = await self.backend.create_sandbox(
                        computer_id=requested_id,
                        temporary=temporary,
                    )
                    # Legacy backends keep their original Sandbox interface. These
                    # attributes let the base properties expose registry semantics
                    # without wrapping the instance or breaking backend-specific APIs.
                    self._tag(sandbox, requested_id, temporary)
                    actual_temporary = temporary

            self._records[requested_id] = _ComputerRecord(
                sandbox=sandbox,
                temporary=actual_temporary,
                owners=1 if add_owner else 0,
            )
            return requested_id, sandbox

    async def get_owned(self, computer_id: str) -> Sandbox | None:
        """Return a locally attached sandbox without changing owner refs."""
        async with self._lock:
            record = self._records.get(computer_id)
            return record.sandbox if record is not None else None

    async def refresh(self, computer_id: str) -> Sandbox | None:
        """Refresh cached state from the provider without changing ownership."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            if record is None:
                return None
            replacement = await self.backend.refresh_sandbox(
                computer_id,
                record.sandbox,
            )
            self._tag(replacement, computer_id, record.temporary)
            record.sandbox = replacement
            return replacement

    def peek(self, computer_id: str) -> Sandbox | None:
        """Return a local sandbox for synchronous status properties."""
        record = self._records.get(computer_id)
        return record.sandbox if record is not None else None

    async def release(self, computer_id: str) -> None:
        """Release one owner and remove an unowned temporary computer."""
        sandbox: Sandbox | None = None
        async with self._lock:
            record = self._records.get(computer_id)
            if record is None:
                return
            record.owners = max(0, record.owners - 1)
            if record.temporary and record.owners == 0:
                sandbox = record.sandbox
                del self._records[computer_id]

        if sandbox is not None:
            await sandbox.stop()

    async def list(self) -> list[ComputerInfo]:
        """Return a de-duplicated provider and in-process inventory."""
        provider_items = await self.backend.list_computers()
        by_id = {item.computer_id: item for item in provider_items}

        async with self._lock:
            for computer_id, record in self._records.items():
                by_id.setdefault(
                    computer_id,
                    ComputerInfo(
                        computer_id=computer_id,
                        sandbox_id=record.sandbox.sandbox_id,
                        backend=self.backend.__class__.__name__,
                        state="running",
                        temporary=record.temporary,
                    ),
                )
        return sorted(by_id.values(), key=lambda item: item.computer_id)

    async def restart(self, computer_id: str) -> Sandbox:
        """Restart a computer while preserving its filesystem."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            replacement = await self.backend.restart_computer(computer_id)
            if replacement is None:
                if record is None:
                    raise BackendError(f"Computer '{computer_id}' was not found.")
                await record.sandbox.stop()
                replacement = await self.backend.create_sandbox(
                    computer_id=computer_id,
                    temporary=record.temporary,
                )
                self._tag(replacement, computer_id, record.temporary)
            if record is None:
                record = _ComputerRecord(
                    replacement,
                    replacement.temporary,
                    owners=0,
                )
                self._records[computer_id] = record
            else:
                record.sandbox = replacement
            return replacement

    async def factory_reset(self, computer_id: str) -> Sandbox:
        """Delete a computer's writable state and recreate its base image."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            replacement = await self.backend.factory_reset_computer(computer_id)
            if replacement is None:
                if record is None:
                    raise BackendError(f"Computer '{computer_id}' was not found.")
                await record.sandbox.stop()
                replacement = await self.backend.create_sandbox(
                    computer_id=computer_id,
                    temporary=record.temporary,
                )
                self._tag(replacement, computer_id, record.temporary)
            if record is None:
                self._records[computer_id] = _ComputerRecord(
                    replacement,
                    replacement.temporary,
                    owners=0,
                )
            else:
                record.sandbox = replacement
            return replacement

    async def set_network_access(self, computer_id: str, enabled: bool) -> Sandbox:
        """Change real network access and refresh the attached sandbox handle."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            if record is None:
                raise BackendError(f"Computer '{computer_id}' was not found.")
            async with _computer_process_lock(computer_id):
                replacement = await self.backend.set_network_access(
                    computer_id,
                    enabled,
                )
            if replacement is None:
                raise BackendError("This backend cannot change network access at runtime.")
            self._tag(replacement, computer_id, record.temporary)
            record.sandbox = replacement
            return replacement

    async def switch_desktop_environment(
        self,
        computer_id: str,
        enabled: bool,
    ) -> Sandbox:
        """Switch virtual/Xfce mode while preserving the whole computer."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.get(computer_id)
            if record is None:
                raise BackendError(f"Computer '{computer_id}' was not found.")
            async with _computer_process_lock(computer_id):
                updated = await self.backend.switch_desktop_environment(
                    computer_id,
                    enabled,
                )
            if updated is None:
                raise BackendError("This backend cannot switch desktop mode at runtime.")
            self._tag(updated, computer_id, record.temporary)
            record.sandbox = updated
            return updated

    async def delete(self, computer_id: str) -> None:
        """Permanently delete a managed computer."""
        computer_id = validate_computer_id(computer_id)
        async with self._lock:
            record = self._records.pop(computer_id, None)
            deleted = await self.backend.delete_computer(computer_id)
            if deleted:
                return
            if record is None:
                raise BackendError(f"Computer '{computer_id}' was not found.")
            await record.sandbox.stop()

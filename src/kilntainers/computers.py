"""Named computer registry shared by MCP sessions and dashboard tools."""

import asyncio
import re
import secrets
from dataclasses import dataclass

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


def random_computer_id() -> str:
    """Generate a readable, collision-resistant provider-safe slug."""
    return (
        f"{secrets.choice(_ADJECTIVES)}-{secrets.choice(_NOUNS)}-{secrets.token_hex(2)}"
    )


def validate_computer_id(computer_id: str) -> str:
    """Validate and return a stable computer ID.

    IDs intentionally follow the common Docker/Fly lowercase slug subset, so
    the same value works across both providers and can safely become a resource
    name without provider-specific rewriting.
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

    Docker and Fly backends additionally discover computers provider-side, so
    permanent records survive a server restart and can be reattached by ID.
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

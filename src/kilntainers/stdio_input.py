"""Cancellable process stdin for the SDK's blocking file-reader transport.

The SDK accepts the public sys.stdin stream. A bounded daemon pump owns a
duplicate of the original pipe; transport reads poll a queue and can observe
shutdown even when the client keeps its write end open. The pump can remain
blocked only until process exit, after application cleanup has completed.
"""

import io
import os
import queue
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from mcp.os.win32.utilities import rebind_std_handle_to_fd


class _InputBuffer(io.RawIOBase):
    def __init__(
        self, messages: queue.Queue[bytes | OSError | None], stopped: threading.Event
    ):
        super().__init__()
        self.messages = messages
        self.stopped = stopped
        self.pending = b""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while not self.pending:
            if self.stopped.is_set():
                return 0
            try:
                data = self.messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if data is None:
                return 0
            if isinstance(data, OSError):
                raise data
            self.pending = data
        size = min(len(buffer), len(self.pending))
        buffer[:size] = self.pending[:size]
        self.pending = self.pending[size:]
        return size


@contextmanager
def interruptible_stdin(enabled: bool) -> Iterator[Callable[[], None]]:
    """Divert child stdin from MCP and permit SIGTERM without client EOF."""
    stopped = threading.Event()
    original = sys.stdin
    if not enabled:
        yield stopped.set
        return
    try:
        fd = original.fileno()
    except (AttributeError, OSError, io.UnsupportedOperation):
        yield stopped.set
        return
    if fd != 0:
        yield stopped.set
        return

    restore_fd = os.dup(fd)
    read_fd = os.dup(fd)
    messages: queue.Queue[bytes | OSError | None] = queue.Queue(maxsize=2)

    def send(data: bytes | OSError | None) -> bool:
        while not stopped.is_set():
            try:
                messages.put(data, timeout=0.05)
                return True
            except queue.Full:
                pass
        return False

    def pump() -> None:
        try:
            with os.fdopen(read_fd, "rb", buffering=0) as wire:
                while not stopped.is_set():
                    data = wire.read(65536)
                    if not send(data or None) or not data:
                        break
        except OSError as error:
            send(error)

    thread = threading.Thread(target=pump, name="mcp-stdin-pump", daemon=True)
    raw = _InputBuffer(messages, stopped)
    stream = io.TextIOWrapper(
        io.BufferedReader(raw), encoding="utf-8", errors="replace"
    )
    started = False
    try:
        null_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(null_fd, fd)
            if sys.platform == "win32":
                rebind_std_handle_to_fd(fd)
        finally:
            os.close(null_fd)
        sys.stdin = stream
        thread.start()
        started = True
        yield stopped.set
    finally:
        stopped.set()
        sys.stdin = original
        os.dup2(restore_fd, fd)
        if sys.platform == "win32":
            rebind_std_handle_to_fd(fd)
        os.close(restore_fd)
        if not started:
            os.close(read_fd)
        stream.close()

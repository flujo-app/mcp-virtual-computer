"""Unit tests for text-only file operations."""

import base64
from collections.abc import Iterable

import pytest

from kilntainers.backends.base import ExecRequest, ExecResult, Sandbox
from kilntainers.file_tools import (
    FileToolError,
    edit_text_file,
    list_directory,
    read_text_file,
    resolve_path,
    write_text_file,
)


class FileSandbox(Sandbox):
    def __init__(self, results: Iterable[ExecResult]) -> None:
        self.results = list(results)
        self.requests: list[ExecRequest] = []

    @property
    def sandbox_id(self) -> str:
        return "file-test"

    async def exec(self, request: ExecRequest) -> ExecResult:
        self.requests.append(request)
        return self.results.pop(0)

    async def stop(self) -> None:
        return None

    async def wait_for_death(self) -> None:
        raise NotImplementedError


def result(stdout: str = "", stderr: str = "", exit_code: int = 0) -> ExecResult:
    return ExecResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        exec_duration_ms=1,
    )


def encoded(value: bytes) -> str:
    return f"{len(value)}\n{base64.b64encode(value).decode('ascii')}"


def test_resolve_relative_and_absolute_paths() -> None:
    assert resolve_path("src/../README.md", "/workspace") == "/workspace/README.md"
    assert resolve_path("/tmp/../etc/hosts", "/workspace") == "/etc/hosts"


@pytest.mark.parametrize("path", ["", "  ", "bad\x00path"])
def test_resolve_rejects_invalid_path(path: str) -> None:
    with pytest.raises(FileToolError):
        resolve_path(path, "/workspace")


async def test_read_file_decodes_utf8_and_returns_digest() -> None:
    raw = "hello, café\n".encode()
    sandbox = FileSandbox([result(stdout=encoded(raw))])

    file = await read_text_file(
        sandbox,
        "notes.txt",
        workspace_directory="/workspace",
        timeout=10,
        output_limit=1000,
        text_limit=1000,
    )

    assert file.path == "/workspace/notes.txt"
    assert file.content == "hello, café\n"
    assert file.size_bytes == len(raw)
    assert len(file.sha256) == 64
    assert sandbox.requests[0].working_directory == "/workspace"


async def test_read_file_rejects_non_utf8() -> None:
    sandbox = FileSandbox([result(stdout=encoded(b"\xff\xfe"))])
    with pytest.raises(FileToolError, match="not valid UTF-8"):
        await read_text_file(
            sandbox,
            "/binary.dat",
            workspace_directory="/workspace",
            timeout=10,
            output_limit=1000,
            text_limit=1000,
        )


async def test_list_directory_decodes_and_sorts_real_entries() -> None:
    raw = b"z.txt\0f\0" b"3\0" b"docs\0d\0" b"4096\0" b"a.txt\0f\0" b"1\0"
    sandbox = FileSandbox([result(stdout=base64.b64encode(raw).decode("ascii"))])

    listing = await list_directory(
        sandbox,
        ".",
        workspace_directory="/workspace",
        timeout=10,
        output_limit=1000,
    )

    assert listing.path == "/workspace"
    assert [(entry.name, entry.kind) for entry in listing.entries] == [
        ("docs", "directory"),
        ("a.txt", "file"),
        ("z.txt", "file"),
    ]


async def test_write_file_uses_stdin_and_atomic_shell() -> None:
    sandbox = FileSandbox([result()])
    file = await write_text_file(
        sandbox,
        "docs/readme.txt",
        "saved text",
        workspace_directory="/workspace",
        timeout=10,
        output_limit=1000,
        text_limit=1000,
    )

    request = sandbox.requests[0]
    assert request.stdin == "saved text"
    assert request.args is not None
    assert "/workspace/docs/readme.txt" in request.args
    assert "mktemp" in request.args[2]
    assert file.content == "saved text"


async def test_edit_file_requires_unambiguous_match() -> None:
    raw = b"same same"
    sandbox = FileSandbox([result(stdout=encoded(raw))])
    with pytest.raises(FileToolError, match="occurs 2 times"):
        await edit_text_file(
            sandbox,
            "a.txt",
            "same",
            "new",
            replace_all=False,
            workspace_directory="/workspace",
            timeout=10,
            output_limit=1000,
            text_limit=1000,
        )
    assert len(sandbox.requests) == 1


async def test_edit_file_writes_with_expected_hash() -> None:
    raw = b"alpha beta"
    sandbox = FileSandbox([result(stdout=encoded(raw)), result()])
    file, replacements, original_content = await edit_text_file(
        sandbox,
        "a.txt",
        "beta",
        "gamma",
        replace_all=False,
        workspace_directory="/workspace",
        timeout=10,
        output_limit=1000,
        text_limit=1000,
    )

    assert replacements == 1
    assert original_content == "alpha beta"
    assert file.content == "alpha gamma"
    write_request = sandbox.requests[1]
    assert write_request.args is not None
    assert file.sha256 not in write_request.args
    assert len(write_request.args[-1]) == 64

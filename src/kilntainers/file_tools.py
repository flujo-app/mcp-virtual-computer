"""Text file operations executed inside the persistent computer."""

import base64
import binascii
import hashlib
import posixpath
from dataclasses import dataclass

from kilntainers.backends.base import ExecRequest, Sandbox
from kilntainers.errors import BackendError, SandboxDiedError


@dataclass(frozen=True, slots=True)
class TextFile:
    path: str
    content: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    name: str
    path: str
    kind: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    path: str
    entries: tuple[DirectoryEntry, ...]


class FileToolError(BackendError):
    """A user-correctable file operation failure."""


def resolve_path(path: str, workspace_directory: str) -> str:
    """Resolve a Linux absolute or workspace-relative file path."""
    if not path or not path.strip():
        raise FileToolError("path must not be empty")
    if "\x00" in path:
        raise FileToolError("path must not contain a NUL byte")
    value = path.strip()
    if value.startswith("/"):
        return posixpath.normpath(value)
    return posixpath.normpath(posixpath.join(workspace_directory, value))


async def _exec(
    sandbox: Sandbox,
    *,
    args: list[str],
    stdin: str | None,
    workspace_directory: str,
    timeout: int,
    output_limit: int,
) -> tuple[str, str, int]:
    try:
        result = await sandbox.exec(
            ExecRequest(
                args=args,
                stdin=stdin,
                working_directory=workspace_directory,
                timeout=timeout,
                output_limit=output_limit,
            )
        )
    except SandboxDiedError:
        raise
    return result.stdout, result.stderr, result.exit_code


async def read_text_file(
    sandbox: Sandbox,
    path: str,
    *,
    workspace_directory: str,
    timeout: int,
    output_limit: int,
    text_limit: int,
) -> TextFile:
    """Read one UTF-8 file without lossy decoding in the Docker backend."""
    resolved = resolve_path(path, workspace_directory)
    script = r'''
set -eu
p=$1
limit=$2
if [ ! -e "$p" ]; then
  printf 'File does not exist: %s\n' "$p" >&2
  exit 66
fi
if [ ! -f "$p" ]; then
  printf 'Path is not a regular file: %s\n' "$p" >&2
  exit 65
fi
size=$(wc -c < "$p")
if [ "$size" -gt "$limit" ]; then
  printf 'File is %s bytes; text tool limit is %s bytes\n' "$size" "$limit" >&2
  exit 67
fi
printf '%s\n' "$size"
base64 -w0 -- "$p"
'''.strip()
    stdout, stderr, exit_code = await _exec(
        sandbox,
        args=["/bin/sh", "-c", script, "mcp-read-file", resolved, str(text_limit)],
        stdin=None,
        workspace_directory=workspace_directory,
        timeout=timeout,
        output_limit=max(output_limit, int(text_limit * 1.4) + 1024),
    )
    if exit_code != 0:
        raise FileToolError(stderr.strip() or f"Unable to read {resolved}")
    try:
        size_line, encoded = stdout.split("\n", 1)
        raw = base64.b64decode(encoded, validate=True)
        declared_size = int(size_line)
    except (ValueError, binascii.Error) as error:
        raise FileToolError(f"Docker returned invalid file data for {resolved}") from error
    if len(raw) != declared_size:
        raise FileToolError(f"File changed while it was being read: {resolved}")
    try:
        content = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise FileToolError(
            f"File is not valid UTF-8 text at byte {error.start}: {resolved}"
        ) from error
    return TextFile(
        path=resolved,
        content=content,
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


async def list_directory(
    sandbox: Sandbox,
    path: str,
    *,
    workspace_directory: str,
    timeout: int,
    output_limit: int,
) -> DirectoryListing:
    """List one real Docker directory without parsing line-oriented filenames."""
    resolved = resolve_path(path, workspace_directory)
    script = r'''
set -eu
p=$1
if [ ! -e "$p" ]; then
  printf 'Directory does not exist: %s\n' "$p" >&2
  exit 66
fi
if [ ! -d "$p" ]; then
  printf 'Path is not a directory: %s\n' "$p" >&2
  exit 65
fi
find "$p" -mindepth 1 -maxdepth 1 -printf '%f\0%y\0%s\0' | base64 -w0
'''.strip()
    stdout, stderr, exit_code = await _exec(
        sandbox,
        args=["/bin/sh", "-c", script, "mcp-list-directory", resolved],
        stdin=None,
        workspace_directory=workspace_directory,
        timeout=timeout,
        output_limit=output_limit,
    )
    if exit_code != 0:
        raise FileToolError(stderr.strip() or f"Unable to list {resolved}")
    try:
        raw = base64.b64decode(stdout, validate=True)
        fields = raw.split(b"\0")
        if fields[-1:] == [b""]:
            fields.pop()
        if len(fields) % 3:
            raise ValueError("incomplete directory entry")
        entries = []
        for index in range(0, len(fields), 3):
            name = fields[index].decode("utf-8", errors="strict")
            kind_code = fields[index + 1].decode("ascii")
            size_bytes = int(fields[index + 2])
            kind = "directory" if kind_code == "d" else "file" if kind_code == "f" else "other"
            entries.append(
                DirectoryEntry(
                    name=name,
                    path=posixpath.join(resolved, name),
                    kind=kind,
                    size_bytes=size_bytes,
                )
            )
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise FileToolError(f"Docker returned invalid directory data for {resolved}") from error
    entries.sort(key=lambda entry: (entry.kind != "directory", entry.name.casefold()))
    return DirectoryListing(path=resolved, entries=tuple(entries))


async def write_text_file(
    sandbox: Sandbox,
    path: str,
    content: str,
    *,
    workspace_directory: str,
    timeout: int,
    output_limit: int,
    text_limit: int,
    create_parent_directories: bool = True,
    expected_sha256: str | None = None,
) -> TextFile:
    """Atomically write UTF-8 text, optionally rejecting a stale edit."""
    resolved = resolve_path(path, workspace_directory)
    raw = content.encode("utf-8")
    if len(raw) > text_limit:
        raise FileToolError(
            f"content is {len(raw)} bytes; text tool limit is {text_limit} bytes"
        )
    script = r'''
set -eu
p=$1
create_parents=$2
expected=$3
parent=$(dirname -- "$p")
if [ "$create_parents" = true ]; then
  mkdir -p -- "$parent"
fi
if [ ! -d "$parent" ]; then
  printf 'Parent directory does not exist: %s\n' "$parent" >&2
  exit 68
fi
if [ "$expected" != - ]; then
  if [ ! -f "$p" ]; then
    printf 'File changed before edit could be saved: %s\n' "$p" >&2
    exit 73
  fi
  actual=$(sha256sum -- "$p" | cut -d ' ' -f 1)
  if [ "$actual" != "$expected" ]; then
    printf 'File changed before edit could be saved: %s\n' "$p" >&2
    exit 73
  fi
fi
tmp=$(mktemp "$parent/.mcp-write.XXXXXX")
trap 'rm -f -- "$tmp"' EXIT HUP INT TERM
cat > "$tmp"
if [ -e "$p" ]; then
  chmod --reference="$p" "$tmp"
fi
mv -f -- "$tmp" "$p"
trap - EXIT HUP INT TERM
'''.strip()
    _, stderr, exit_code = await _exec(
        sandbox,
        args=[
            "/bin/sh",
            "-c",
            script,
            "mcp-write-file",
            resolved,
            "true" if create_parent_directories else "false",
            expected_sha256 or "-",
        ],
        stdin=content,
        workspace_directory=workspace_directory,
        timeout=timeout,
        output_limit=output_limit,
    )
    if exit_code != 0:
        raise FileToolError(stderr.strip() or f"Unable to write {resolved}")
    return TextFile(
        path=resolved,
        content=content,
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


async def edit_text_file(
    sandbox: Sandbox,
    path: str,
    old_text: str,
    new_text: str,
    *,
    replace_all: bool,
    workspace_directory: str,
    timeout: int,
    output_limit: int,
    text_limit: int,
) -> tuple[TextFile, int, str]:
    """Apply an exact textual replacement with optimistic concurrency."""
    if not old_text:
        raise FileToolError("old_text must not be empty")
    current = await read_text_file(
        sandbox,
        path,
        workspace_directory=workspace_directory,
        timeout=timeout,
        output_limit=output_limit,
        text_limit=text_limit,
    )
    occurrences = current.content.count(old_text)
    if occurrences == 0:
        raise FileToolError(f"old_text was not found in {current.path}")
    if occurrences > 1 and not replace_all:
        raise FileToolError(
            f"old_text occurs {occurrences} times in {current.path}; provide more "
            "surrounding text or set replace_all=true"
        )
    replacements = occurrences if replace_all else 1
    updated = current.content.replace(old_text, new_text, -1 if replace_all else 1)
    saved = await write_text_file(
        sandbox,
        current.path,
        updated,
        workspace_directory=workspace_directory,
        timeout=timeout,
        output_limit=output_limit,
        text_limit=text_limit,
        create_parent_directories=False,
        expected_sha256=current.sha256,
    )
    return saved, replacements, current.content

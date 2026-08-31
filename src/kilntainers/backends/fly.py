"""Fly Machines backend for remotely hosted sandbox computers."""

import argparse
import asyncio
import base64
import json
import os
import shlex
import time
from dataclasses import dataclass
from typing import cast

from kilntainers.backends.base import (
    Backend,
    ComputerInfo,
    ExecRequest,
    ExecResult,
    Sandbox,
)
from kilntainers.computers import random_computer_id
from kilntainers.config import BackendConfig
from kilntainers.errors import BackendError, SandboxDiedError

DEFAULT_FLY_IMAGE = "debian:bookworm-slim"
COMPUTER_ID_METADATA = "kilntainers-computer-id"
TEMPORARY_METADATA = "kilntainers-temporary"
IMAGE_METADATA = "kilntainers-image"


@dataclass(frozen=True, slots=True, kw_only=True)
class FlyBackendConfig(BackendConfig):
    """Configuration for Fly Machines orchestration through flyctl."""

    fly_cli: str = "fly"
    app: str | None = None
    token: str | None = None
    image: str = DEFAULT_FLY_IMAGE
    region: str | None = None
    shell: str = "/bin/bash"
    cpu_kind: str = "shared"
    cpus: int = 1
    memory_mb: int = 512
    rootfs_size_gb: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class _FlySandboxState:
    machine_id: str
    computer_id: str
    temporary: bool
    image: str


class FlyBackend(Backend):
    """Provision and manage one Fly Machine per sandbox computer."""

    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        group.add_argument(
            "--fly-cli",
            default="fly",
            help="flyctl/fly executable (default: fly)",
        )
        group.add_argument(
            "--fly-app",
            default=os.getenv("FLY_APP_NAME"),
            help="Fly App that owns sandbox Machines (default: FLY_APP_NAME)",
        )
        group.add_argument(
            "--fly-token",
            default=os.getenv("FLY_API_TOKEN") or os.getenv("FLY_TOKEN"),
            help="Fly API token (default: FLY_API_TOKEN or FLY_TOKEN)",
        )
        group.add_argument(
            "--fly-image",
            default=DEFAULT_FLY_IMAGE,
            help=f"Fly Machine image (default: {DEFAULT_FLY_IMAGE})",
        )
        group.add_argument(
            "--fly-region",
            default=os.getenv("FLY_REGION"),
            help="Region for new Machines (default: FLY_REGION or Fly placement)",
        )
        group.add_argument(
            "--fly-cpu-kind",
            choices=["shared", "performance"],
            default="shared",
            help="Fly Machine CPU kind (default: shared)",
        )
        group.add_argument(
            "--fly-cpus",
            type=int,
            default=1,
            help="Fly Machine vCPU count (default: 1)",
        )
        group.add_argument(
            "--fly-memory",
            type=int,
            default=512,
            dest="fly_memory",
            help="Fly Machine memory in MB (default: 512)",
        )
        group.add_argument(
            "--fly-rootfs-size",
            type=int,
            default=None,
            help="Optional Fly Machine root filesystem size in GB",
        )

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        return FlyBackendConfig(
            fly_cli=args.fly_cli,
            app=args.fly_app,
            token=args.fly_token,
            image=args.fly_image,
            region=args.fly_region,
            shell=args.shell,
            cpu_kind=args.fly_cpu_kind,
            cpus=args.fly_cpus,
            memory_mb=args.fly_memory,
            rootfs_size_gb=args.fly_rootfs_size,
            default_timeout=args.timeout,
        )

    def __init__(self, config: FlyBackendConfig) -> None:
        super().__init__(config)
        self._config: FlyBackendConfig = config

    def _fly_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self._config.token:
            env["FLY_API_TOKEN"] = self._config.token
        return env

    async def _run_fly(
        self,
        *args: str,
        check: bool = True,
        timeout: float = 60,
    ) -> tuple[int, bytes, bytes]:
        """Run flyctl without putting the API token in the process arguments."""
        cmd = [self._config.fly_cli, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._fly_env(),
            )
        except FileNotFoundError:
            raise BackendError(
                f"Fly CLI '{self._config.fly_cli}' was not found. Install flyctl "
                "or pass --fly-cli."
            )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BackendError(
                f"Fly command timed out after {timeout:g}s: {' '.join(cmd)}"
            )
        assert proc.returncode is not None
        if check and proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise BackendError(
                f"Fly command failed (exit {proc.returncode}): {' '.join(cmd)}"
                + (f"\n{detail}" if detail else "")
            )
        return proc.returncode, stdout, stderr

    async def _validate(self) -> None:
        if not self._config.app:
            raise BackendError(
                "Fly backend requires --fly-app or the FLY_APP_NAME environment variable."
            )
        if not self._config.token:
            raise BackendError(
                "Fly backend requires --fly-token, FLY_API_TOKEN, or FLY_TOKEN. "
                "Use an app-scoped deploy token where possible."
            )
        if self._config.cpus < 1:
            raise BackendError("--fly-cpus must be at least 1.")
        if self._config.memory_mb < 256:
            raise BackendError("--fly-memory must be at least 256 MB.")
        await self._run_fly("version", timeout=10)
        await self._list_machine_rows()

    async def _list_machine_rows(self) -> list[dict[str, object]]:
        if not self._config.app:
            return []
        _, stdout, _ = await self._run_fly(
            "machine",
            "list",
            "--app",
            self._config.app,
            "--json",
            timeout=20,
        )
        try:
            payload = json.loads(stdout.decode("utf-8") or "[]")
        except json.JSONDecodeError:
            raise BackendError("Fly CLI returned invalid machine inventory JSON.")
        if isinstance(payload, dict):
            payload = payload.get("machines", payload.get("Machines", []))
        if not isinstance(payload, list):
            raise BackendError(
                "Fly CLI returned an unexpected machine inventory shape."
            )
        return [row for row in payload if isinstance(row, dict)]

    @staticmethod
    def _metadata(row: dict[str, object]) -> dict[str, object]:
        config = row.get("config", row.get("Config", {}))
        if not isinstance(config, dict):
            return {}
        config = cast("dict[str, object]", config)
        metadata = config.get("metadata", config.get("Metadata", {}))
        return cast("dict[str, object]", metadata) if isinstance(metadata, dict) else {}

    @staticmethod
    def _value(row: dict[str, object], *names: str) -> object | None:
        for name in names:
            if name in row:
                return row[name]
        return None

    def _computer_row(
        self, rows: list[dict[str, object]], computer_id: str
    ) -> dict[str, object] | None:
        for row in rows:
            metadata = self._metadata(row)
            if metadata.get(COMPUTER_ID_METADATA) == computer_id:
                return row
        return None

    def _row_to_sandbox(self, row: dict[str, object]) -> "FlySandbox":
        metadata = self._metadata(row)
        computer_id = str(metadata.get(COMPUTER_ID_METADATA, ""))
        temporary = str(metadata.get(TEMPORARY_METADATA, "true")).lower() == "true"
        image = str(metadata.get(IMAGE_METADATA, self._config.image))
        machine_id = str(self._value(row, "id", "ID", "machine_id") or "")
        return FlySandbox(
            self,
            _FlySandboxState(
                machine_id=machine_id,
                computer_id=computer_id,
                temporary=temporary,
                image=image,
            ),
        )

    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> "FlySandbox":
        if not self._config.app:  # validated by create_sandbox
            raise BackendError("Fly App is not configured.")
        computer_id = computer_id or random_computer_id()
        if self._computer_row(await self._list_machine_rows(), computer_id) is not None:
            raise BackendError(f"Computer '{computer_id}' already exists in Fly.")

        machine_config = json.dumps(
            {"init": {"exec": ["tail", "-f", "/dev/null"]}},
            separators=(",", ":"),
        )
        args = [
            "machine",
            "run",
            "--app",
            self._config.app,
            "--name",
            computer_id,
            "--detach",
            "--machine-config",
            machine_config,
            "--metadata",
            "kilntainers=true",
            "--metadata",
            f"{COMPUTER_ID_METADATA}={computer_id}",
            "--metadata",
            f"{TEMPORARY_METADATA}={str(temporary).lower()}",
            "--metadata",
            f"{IMAGE_METADATA}={self._config.image}",
            "--vm-cpu-kind",
            self._config.cpu_kind,
            "--vm-cpus",
            str(self._config.cpus),
            "--vm-memory",
            str(self._config.memory_mb),
            "--rootfs-persist",
            "never" if temporary else "always",
            "--restart",
            "no" if temporary else "always",
        ]
        if temporary:
            args.append("--rm")
        if self._config.region:
            args.extend(["--region", self._config.region])
        if self._config.rootfs_size_gb is not None:
            args.extend(["--rootfs-size", str(self._config.rootfs_size_gb)])
        args.append(self._config.image)
        await self._run_fly(*args, timeout=120)

        deadline = asyncio.get_running_loop().time() + 45
        row: dict[str, object] | None = None
        while asyncio.get_running_loop().time() < deadline:
            row = self._computer_row(await self._list_machine_rows(), computer_id)
            if row is not None:
                state = str(self._value(row, "state", "State") or "").lower()
                if state in {"started", "running"}:
                    break
            await asyncio.sleep(0.5)
        if row is None:
            raise BackendError(
                f"Fly created computer '{computer_id}' but it did not appear within 45s."
            )
        sandbox = self._row_to_sandbox(row)
        try:
            await sandbox._verify_readiness()
        except Exception:
            # A failed readiness check must not leak a billable Machine.
            await self.delete_computer(computer_id)
            raise
        return sandbox

    async def attach_sandbox(self, computer_id: str) -> "FlySandbox | None":
        await self.validate()
        row = self._computer_row(await self._list_machine_rows(), computer_id)
        if row is None:
            return None
        state = str(self._value(row, "state", "State") or "").lower()
        machine_id = str(self._value(row, "id", "ID", "machine_id") or "")
        if state not in {"started", "running"}:
            assert self._config.app is not None
            await self._run_fly(
                "machine", "start", machine_id, "--app", self._config.app, timeout=45
            )
            row = self._computer_row(await self._list_machine_rows(), computer_id)
            if row is None:  # pragma: no cover - provider race
                raise BackendError(
                    f"Computer '{computer_id}' disappeared while starting."
                )
        sandbox = self._row_to_sandbox(row)
        await sandbox._verify_readiness()
        return sandbox

    async def list_computers(self) -> list[ComputerInfo]:
        await self.validate()
        computers: list[ComputerInfo] = []
        for row in await self._list_machine_rows():
            metadata = self._metadata(row)
            computer_id = metadata.get(COMPUTER_ID_METADATA)
            if not isinstance(computer_id, str) or not computer_id:
                continue
            computers.append(
                ComputerInfo(
                    computer_id=computer_id,
                    sandbox_id=str(self._value(row, "id", "ID", "machine_id") or ""),
                    backend="fly",
                    state=str(self._value(row, "state", "State") or "unknown"),
                    temporary=(
                        str(metadata.get(TEMPORARY_METADATA, "true")).lower() == "true"
                    ),
                    image=str(metadata.get(IMAGE_METADATA, self._config.image)),
                    created_at=(
                        str(self._value(row, "created_at", "createdAt", "CreatedAt"))
                        if self._value(row, "created_at", "createdAt", "CreatedAt")
                        else None
                    ),
                )
            )
        return computers

    async def restart_computer(self, computer_id: str) -> "FlySandbox | None":
        row = self._computer_row(await self._list_machine_rows(), computer_id)
        if row is None:
            return None
        assert self._config.app is not None
        machine_id = str(self._value(row, "id", "ID", "machine_id") or "")
        await self._run_fly(
            "machine", "restart", machine_id, "--app", self._config.app, timeout=60
        )
        refreshed = self._computer_row(await self._list_machine_rows(), computer_id)
        if refreshed is None:  # pragma: no cover - provider race
            raise BackendError(f"Computer '{computer_id}' disappeared after restart.")
        return self._row_to_sandbox(refreshed)

    async def delete_computer(self, computer_id: str) -> bool:
        row = self._computer_row(await self._list_machine_rows(), computer_id)
        if row is None:
            return False
        assert self._config.app is not None
        machine_id = str(self._value(row, "id", "ID", "machine_id") or "")
        await self._run_fly(
            "machine",
            "destroy",
            "--force",
            "--app",
            self._config.app,
            machine_id,
            timeout=60,
        )
        return True

    async def factory_reset_computer(self, computer_id: str) -> "FlySandbox | None":
        row = self._computer_row(await self._list_machine_rows(), computer_id)
        if row is None:
            return None
        metadata = self._metadata(row)
        temporary = str(metadata.get(TEMPORARY_METADATA, "true")).lower() == "true"
        await self.delete_computer(computer_id)
        return await self._create_sandbox(
            computer_id=computer_id,
            temporary=temporary,
        )

    def tool_instructions(self) -> str | None:
        if self._config.image != DEFAULT_FLY_IMAGE:
            return None
        return (
            "Execute shell commands in an isolated Debian Fly Machine. Pass a "
            "computer_id to reconnect to a named Machine, or omit it to create a "
            "readable random ID. Temporary Machines are destroyed with their MCP "
            "session; permanent Machines persist on Fly and can be reattached later."
        )


class FlySandbox(Sandbox):
    """Command and lifecycle handle for one Fly Machine."""

    def __init__(self, backend: FlyBackend, state: _FlySandboxState) -> None:
        self._backend = backend
        self._machine_id = state.machine_id
        self._computer_id = state.computer_id
        self._temporary = state.temporary
        self._image = state.image
        self._stopped = False
        self._stop_requested = False
        self._exec_lock = asyncio.Lock()

    @property
    def sandbox_id(self) -> str:
        return self._machine_id

    @property
    def computer_id(self) -> str:
        return self._computer_id

    @property
    def temporary(self) -> bool:
        return self._temporary

    def _remote_command(self, request: ExecRequest) -> str:
        if request.command is not None:
            command = f"{shlex.quote(self._backend._config.shell)} -c {shlex.quote(request.command)}"
        else:
            assert request.args is not None
            command = shlex.join(request.args)
        if request.working_directory:
            command = f"cd -- {shlex.quote(request.working_directory)} && {command}"
        if request.stdin is not None:
            encoded = base64.b64encode(request.stdin.encode("utf-8")).decode("ascii")
            command = f"printf %s {shlex.quote(encoded)} | base64 -d | {command}"
        return command

    @staticmethod
    def _exec_payload(payload: object) -> dict[str, object] | None:
        if isinstance(payload, list) and payload:
            payload = payload[0]
        if not isinstance(payload, dict):
            return None
        result = cast("dict[str, object]", payload)
        for key in ("result", "Result"):
            nested = result.get(key)
            if isinstance(nested, dict):
                result = cast("dict[str, object]", nested)
                break
        return result

    async def _do_exec(self, request: ExecRequest) -> ExecResult:
        if not self._backend._config.app:
            raise BackendError("Fly App is not configured.")
        started = time.monotonic()
        returncode, stdout, stderr = await self._backend._run_fly(
            "machine",
            "exec",
            "--app",
            self._backend._config.app,
            "--json",
            "--timeout",
            str(request.timeout),
            self._machine_id,
            self._remote_command(request),
            check=False,
            timeout=request.timeout + 15,
        )
        duration = int((time.monotonic() - started) * 1000)
        decoded_stdout = stdout.decode("utf-8", errors="replace")
        decoded_stderr = stderr.decode("utf-8", errors="replace")
        payload: dict[str, object] | None = None
        try:
            payload = self._exec_payload(json.loads(decoded_stdout))
        except json.JSONDecodeError:
            pass
        if payload is not None:
            out = str(payload.get("stdout", payload.get("Stdout", "")) or "")
            err = str(payload.get("stderr", payload.get("Stderr", "")) or "")
            exit_value = payload.get(
                "exit_code",
                payload.get("exitCode", payload.get("ExitCode", returncode)),
            )
            if isinstance(exit_value, int | float | str):
                try:
                    exit_code = int(exit_value)
                except ValueError:
                    exit_code = returncode
            else:
                exit_code = returncode
        else:
            out, err, exit_code = decoded_stdout, decoded_stderr, returncode

        combined_size = len(out.encode("utf-8")) + len(err.encode("utf-8"))
        if combined_size > request.output_limit:
            return ExecResult(
                stdout="",
                stderr=(
                    f"[kilntainers: output limit exceeded ({request.output_limit} "
                    "bytes). No output returned.]"
                ),
                exit_code=1,
                exec_duration_ms=duration,
            )
        return ExecResult(
            stdout=out,
            stderr=err,
            exit_code=exit_code,
            exec_duration_ms=duration,
        )

    async def _verify_readiness(self) -> None:
        result = await self._do_exec(
            ExecRequest(
                command="echo kilntainers-ready",
                timeout=20,
                output_limit=4096,
            )
        )
        if result.exit_code != 0 or "kilntainers-ready" not in result.stdout:
            raise BackendError(
                f"Fly Machine {self._machine_id} started but readiness failed: "
                f"{result.stderr or result.stdout}"
            )

    async def exec(self, request: ExecRequest) -> ExecResult:
        if self._stopped:
            raise SandboxDiedError(f"Computer '{self._computer_id}' has been stopped.")
        async with self._exec_lock:
            return await self._do_exec(request)

    async def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_requested = True
        if not self._backend._config.app:
            return
        try:
            if self._temporary:
                await self._backend._run_fly(
                    "machine",
                    "destroy",
                    "--force",
                    "--app",
                    self._backend._config.app,
                    self._machine_id,
                    timeout=60,
                )
            else:
                await self._backend._run_fly(
                    "machine",
                    "stop",
                    self._machine_id,
                    "--app",
                    self._backend._config.app,
                    timeout=45,
                )
        except BackendError:
            pass

    async def wait_for_death(self) -> None:
        while True:
            await asyncio.sleep(2)
            rows = await self._backend._list_machine_rows()
            row = self._backend._computer_row(rows, self._computer_id)
            if row is not None:
                state = str(self._backend._value(row, "state", "State") or "").lower()
                if state in {"started", "running", "starting"}:
                    continue
            if self._stop_requested:
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    return
            return

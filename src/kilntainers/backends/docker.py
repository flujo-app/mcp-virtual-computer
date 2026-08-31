"""Docker backend implementation."""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass, field
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

DEFAULT_IMAGE = "debian:bookworm-slim"
COMPUTER_NAME_PREFIX = "kilntainer-"
COMPUTER_ID_LABEL = "kilntainers.computer-id"
TEMPORARY_LABEL = "kilntainers.temporary"
IMAGE_LABEL = "kilntainers.image"


@dataclass(frozen=True, slots=True, kw_only=True)
class DockerBackendConfig(BackendConfig):
    """Configuration for the Docker backend.

    Populated from CLI args by DockerBackend.config_from_args().
    Consumed by DockerBackend.
    """

    engine: str = "docker"
    host: str | None = None
    image: str = "debian:bookworm-slim"
    shell: str = "/bin/bash"
    network_enabled: bool = False
    cpu: str | None = None
    memory: str | None = None
    docker_run_flags: list[str] = field(default_factory=list)


class _OutputLimitExceeded(Exception):
    """Internal signal: combined output exceeded the configured limit."""

    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class _DockerSandboxState:
    """State shared between DockerBackend and DockerSandbox.

    The sandbox receives these values at construction instead of the full
    config to keep the interface minimal.
    """

    engine: str
    host: str | None
    shell: str
    container_id: str
    computer_id: str | None = None
    temporary: bool = True
    image: str | None = None


class DockerBackend(Backend):
    """Docker backend implementation.

    Manages Docker container lifecycle and command execution through
    subprocess calls to the Docker CLI (or compatible engine like podman).
    """

    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        """Register Docker-specific CLI arguments."""
        group.add_argument(
            "--engine",
            default="docker",
            help="Container CLI binary (default: docker). Supports podman.",
        )
        group.add_argument(
            "--docker-host",
            default=None,
            dest="docker_host",
            help=(
                "Docker daemon socket/address, passed as -H to the Docker CLI "
                '(e.g., "ssh://user@remote-host", "tcp://host:2375")'
            ),
        )
        group.add_argument(
            "--image",
            default="debian:bookworm-slim",
            help="Docker image (default: debian:bookworm-slim)",
        )
        group.add_argument(
            "--cpu",
            default=None,
            help='Docker CPU limit (e.g., "1.5")',
        )
        group.add_argument(
            "--memory",
            default=None,
            help='Docker memory limit (e.g., "512m")',
        )
        group.add_argument(
            "--docker-run-flag",
            action="append",
            default=None,
            dest="docker_run_flags",
            help=(
                "Additional flag passed to docker run. Repeatable. "
                '(e.g., --docker-run-flag "--pids-limit=256")'
            ),
        )

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        """Build DockerBackendConfig from parsed CLI arguments."""
        # Use core --shell with a backend-specific default
        shell = args.shell if args.shell is not None else "/bin/bash"
        return DockerBackendConfig(
            engine=args.engine,
            host=args.docker_host,
            image=args.image,
            shell=shell,
            network_enabled=args.network,
            cpu=args.cpu,
            memory=args.memory,
            docker_run_flags=args.docker_run_flags or [],
            default_timeout=args.timeout,
        )

    def __init__(self, config: DockerBackendConfig) -> None:
        super().__init__(config)
        # Override parent's _config with more specific type for type checker
        self._config: DockerBackendConfig = config

    @property
    def _engine_prefix(self) -> list[str]:
        """Return the base engine command, including -H if a host is configured."""
        prefix = [self._config.engine]
        if self._config.host is not None:
            prefix.extend(["-H", self._config.host])
        return prefix

    async def _run_docker(
        self,
        *args: str,
        stdin_data: bytes | None = None,
        check: bool = True,
        timeout: float = 30,
    ) -> tuple[int, bytes, bytes]:
        """Run a Docker CLI command and return (returncode, stdout, stderr).

        Args:
            args: Command arguments after the engine name (e.g., "info", "run", "-d").
            stdin_data: Bytes to pipe to the command's stdin.
            check: If True, raise BackendError on non-zero exit.
            timeout: Seconds to wait before killing the subprocess.

        Returns:
            Tuple of (return_code, stdout_bytes, stderr_bytes).

        Raises:
            BackendError: If check=True and the command exits non-zero,
                or if the command times out.
        """
        cmd = [*self._engine_prefix, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BackendError(
                f"Docker command timed out after {timeout}s: {' '.join(cmd)}"
            )

        if check and proc.returncode is not None and proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            raise BackendError(
                f"Docker command failed (exit {proc.returncode}): "
                f"{' '.join(cmd)}\n{stderr_text}"
            )
        assert proc.returncode is not None
        return proc.returncode, stdout, stderr

    async def _validate(self) -> None:
        """Validate Docker prerequisites.

        Checks that the Docker engine is reachable and responsive.
        """
        try:
            await self._run_docker("info", timeout=10)
        except BackendError:
            raise BackendError(
                f"Cannot connect to {self._config.engine}. "
                f"Is the {self._config.engine} daemon running?"
            )

    async def _ensure_image(self) -> None:
        """Pull the configured image if not available locally."""
        # Check if image exists locally
        returncode, _, _ = await self._run_docker(
            "image",
            "inspect",
            self._config.image,
            check=False,
            timeout=10,
        )
        if returncode == 0:
            return  # Image already available

        # Pull with progress output to stderr
        # Don't use _run_docker because we want stderr to pass through
        # to the parent process (for progress display to the user)
        proc = await asyncio.create_subprocess_exec(
            *self._engine_prefix,
            "pull",
            self._config.image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=None,  # inherit parent stderr — shows pull progress
        )
        await proc.wait()
        if proc.returncode != 0:
            raise BackendError(
                f"Failed to pull image '{self._config.image}'. "
                f"Check that the image name is correct and the registry is reachable."
            )

    def _build_run_command(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> list[str]:
        """Build the docker run argument list."""
        computer_id = computer_id or random_computer_id()
        cmd = [
            "run",
            "-d",  # detached mode
            "--label",
            "kilntainers=true",  # identification label
            "--label",
            f"{COMPUTER_ID_LABEL}={computer_id}",
            "--label",
            f"{TEMPORARY_LABEL}={str(temporary).lower()}",
            "--label",
            f"{IMAGE_LABEL}={self._config.image}",
            "--name",
            f"{COMPUTER_NAME_PREFIX}{computer_id}",
        ]

        if temporary:
            # Temporary computers disappear when stopped by session cleanup.
            cmd.append("--rm")

        # Network isolation (default: disabled)
        if not self._config.network_enabled:
            cmd.extend(["--network", "none"])

        # Resource limits
        if self._config.cpu is not None:
            cmd.extend(["--cpus", self._config.cpu])
        if self._config.memory is not None:
            cmd.extend(["--memory", self._config.memory])

        # User-provided extra flags (escape hatch)
        for flag in self._config.docker_run_flags:
            cmd.append(flag)

        # Image and keep-alive command
        cmd.append(self._config.image)
        cmd.extend(["tail", "-f", "/dev/null"])

        return cmd

    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> "DockerSandbox":
        """Create a Docker sandbox.

        Performs the full startup sequence:
        1. Ensure image is available (pull if needed)
        2. Build and run docker run command
        3. Create sandbox object
        4. Verify readiness (cleanup if fails)
        """
        computer_id = computer_id or random_computer_id()

        # 1. Ensure image is available (pull if needed)
        await self._ensure_image()

        # 2. Build docker run command
        cmd = self._build_run_command(
            computer_id=computer_id,
            temporary=temporary,
        )

        # 3. Create and start container
        _, stdout, _ = await self._run_docker(*cmd, timeout=30)
        container_id = stdout.decode().strip()

        # 4. Create sandbox state
        state = _DockerSandboxState(
            engine=self._config.engine,
            host=self._config.host,
            shell=self._config.shell,
            container_id=container_id,
            computer_id=computer_id,
            temporary=temporary,
            image=self._config.image,
        )

        # 5. Create sandbox object
        sandbox = DockerSandbox(state)

        # 6. Verify readiness
        try:
            await sandbox._verify_readiness()
        except Exception:
            # Clean up the container if readiness check fails
            await sandbox.stop()
            raise

        return sandbox

    async def _inspect_computer(self, computer_id: str) -> dict[str, object] | None:
        """Return Docker inspect data for an owned named computer."""
        returncode, stdout, _ = await self._run_docker(
            "container",
            "inspect",
            f"{COMPUTER_NAME_PREFIX}{computer_id}",
            check=False,
            timeout=10,
        )
        if returncode != 0:
            return None
        try:
            items = json.loads(stdout.decode("utf-8"))
            data = items[0]
        except (json.JSONDecodeError, IndexError, TypeError):
            raise BackendError(
                f"Docker returned invalid inspect data for computer '{computer_id}'."
            )
        if not isinstance(data, dict):
            raise BackendError(
                f"Docker returned invalid inspect data for computer '{computer_id}'."
            )
        labels = data.get("Config", {})
        labels = labels.get("Labels", {}) if isinstance(labels, dict) else {}
        if not isinstance(labels, dict) or labels.get(COMPUTER_ID_LABEL) != computer_id:
            raise BackendError(
                f"Docker name '{COMPUTER_NAME_PREFIX}{computer_id}' is already in "
                "use by a container not owned by this server."
            )
        return data

    def _sandbox_from_inspect(self, data: dict[str, object]) -> "DockerSandbox":
        """Build a live sandbox handle from Docker inspect JSON."""
        config = data.get("Config", {})
        config = cast("dict[str, object]", config) if isinstance(config, dict) else {}
        labels = config.get("Labels", {})
        labels = cast("dict[str, object]", labels) if isinstance(labels, dict) else {}
        computer_id = str(labels.get(COMPUTER_ID_LABEL, ""))
        temporary = str(labels.get(TEMPORARY_LABEL, "true")).lower() == "true"
        image = str(labels.get(IMAGE_LABEL) or self._config.image)
        return DockerSandbox(
            _DockerSandboxState(
                engine=self._config.engine,
                host=self._config.host,
                shell=self._config.shell,
                container_id=str(data.get("Id", "")),
                computer_id=computer_id,
                temporary=temporary,
                image=image,
            )
        )

    async def attach_sandbox(self, computer_id: str) -> "DockerSandbox | None":
        """Attach to a named container, starting it first when necessary."""
        await self.validate()
        data = await self._inspect_computer(computer_id)
        if data is None:
            return None
        state = data.get("State", {})
        state = cast("dict[str, object]", state) if isinstance(state, dict) else {}
        running = bool(state.get("Running"))
        if not running:
            await self._run_docker("start", str(data.get("Id", "")), timeout=30)
            data = await self._inspect_computer(computer_id)
            if data is None:  # pragma: no cover - provider race
                raise BackendError(
                    f"Computer '{computer_id}' disappeared while it was starting."
                )
        sandbox = self._sandbox_from_inspect(data)
        await sandbox._verify_readiness()
        return sandbox

    async def list_computers(self) -> list[ComputerInfo]:
        """List all Docker computers owned by this server."""
        await self.validate()
        _, stdout, _ = await self._run_docker(
            "ps",
            "-aq",
            "--filter",
            f"label={COMPUTER_ID_LABEL}",
            timeout=10,
        )
        container_ids = stdout.decode("utf-8").split()
        if not container_ids:
            return []
        _, inspect_stdout, _ = await self._run_docker(
            "container",
            "inspect",
            *container_ids,
            timeout=15,
        )
        try:
            rows = json.loads(inspect_stdout.decode("utf-8"))
        except json.JSONDecodeError:
            raise BackendError("Docker returned invalid computer inventory data.")

        computers: list[ComputerInfo] = []
        for data in rows if isinstance(rows, list) else []:
            if not isinstance(data, dict):
                continue
            config = data.get("Config", {})
            labels = config.get("Labels", {}) if isinstance(config, dict) else {}
            if not isinstance(labels, dict):
                continue
            computer_id = labels.get(COMPUTER_ID_LABEL)
            if not isinstance(computer_id, str) or not computer_id:
                continue
            state_data = data.get("State", {})
            state = (
                str(state_data.get("Status", "unknown"))
                if isinstance(state_data, dict)
                else "unknown"
            )
            computers.append(
                ComputerInfo(
                    computer_id=computer_id,
                    sandbox_id=str(data.get("Id", ""))[:12],
                    backend="docker",
                    state=state,
                    temporary=(
                        str(labels.get(TEMPORARY_LABEL, "true")).lower() == "true"
                    ),
                    image=str(labels.get(IMAGE_LABEL) or self._config.image),
                    created_at=(
                        str(data.get("Created")) if data.get("Created") else None
                    ),
                )
            )
        return computers

    async def restart_computer(self, computer_id: str) -> "DockerSandbox | None":
        """Restart a Docker computer without replacing its writable layer."""
        data = await self._inspect_computer(computer_id)
        if data is None:
            return None
        await self._run_docker("restart", "-t", "5", str(data.get("Id", "")))
        refreshed = await self._inspect_computer(computer_id)
        if refreshed is None:  # pragma: no cover - provider race
            raise BackendError(f"Computer '{computer_id}' disappeared after restart.")
        sandbox = self._sandbox_from_inspect(refreshed)
        await sandbox._verify_readiness()
        return sandbox

    async def delete_computer(self, computer_id: str) -> bool:
        """Force-remove a Docker computer and all writable state."""
        data = await self._inspect_computer(computer_id)
        if data is None:
            return False
        await self._run_docker("rm", "-f", str(data.get("Id", "")), timeout=20)
        return True

    async def factory_reset_computer(self, computer_id: str) -> "DockerSandbox | None":
        """Recreate a named computer from its original image."""
        data = await self._inspect_computer(computer_id)
        if data is None:
            return None
        config = data.get("Config", {})
        config = cast("dict[str, object]", config) if isinstance(config, dict) else {}
        labels = config.get("Labels", {})
        labels = cast("dict[str, object]", labels) if isinstance(labels, dict) else {}
        temporary = str(labels.get(TEMPORARY_LABEL, "true")).lower() == "true"
        await self.delete_computer(computer_id)
        return await self._create_sandbox(
            computer_id=computer_id,
            temporary=temporary,
        )

    def tool_instructions(self) -> str | None:
        """Return tool description for Docker backend.

        Returns None if using a custom image (baked-in description only
        applies to default Debian image).
        """
        if self._config.image != DEFAULT_IMAGE:
            return None

        shell_name = self._config.shell.rsplit("/", 1)[-1]  # basename
        timeout = self._config.default_timeout

        return (
            f"Execute a shell command in an isolated Debian Linux sandbox. "
            f"Commands run in {shell_name}. Each call is independent — "
            f"no state (shell variables, working directory) persists between calls (however filesystem does persist). Use the working_directory "
            f"parameter or chain commands with && to control execution context. "
            f"\n\n"
            f"To write files or pass data without shell escaping, use the "
            f'stdin parameter (e.g., command="cat > file.txt" with content '
            f"in stdin). Commands time out after {timeout} seconds by default "
            f"(override with the timeout parameter for long-running operations)."
        )


class DockerSandbox(Sandbox):
    """Docker sandbox implementation.

    Wraps a running Docker container. Handles command execution with
    timeout and output-limit enforcement, stop, and death detection.
    """

    def __init__(self, state: _DockerSandboxState) -> None:
        self._engine = state.engine
        self._host = state.host
        self._shell = state.shell
        self._container_id = state.container_id
        self._computer_id = state.computer_id or state.container_id[:12]
        self._temporary = state.temporary
        self._image = state.image
        self._stopped = False
        self._stop_requested = False
        self._exec_lock = asyncio.Lock()

    @property
    def _engine_prefix(self) -> list[str]:
        """Return the base engine command, including -H if a host is configured."""
        prefix = [self._engine]
        if self._host is not None:
            prefix.extend(["-H", self._host])
        return prefix

    @property
    def sandbox_id(self) -> str:
        """Return the short form (first 12 chars) of the container ID."""
        return self._container_id[:12]

    @property
    def computer_id(self) -> str:
        """Return the stable computer slug rather than the container hash."""
        return self._computer_id

    @property
    def temporary(self) -> bool:
        """Return the provider-side cleanup mode recorded in Docker labels."""
        return self._temporary

    async def _run_docker(
        self,
        *args: str,
        stdin_data: bytes | None = None,
        check: bool = True,
        timeout: float = 30,
    ) -> tuple[int, bytes, bytes]:
        """Run a Docker CLI command and return (returncode, stdout, stderr).

        Shared helper method for Docker CLI calls within the sandbox.
        """
        cmd = [*self._engine_prefix, *args]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_data),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise BackendError(
                f"Docker command timed out after {timeout}s: {' '.join(cmd)}"
            )

        if check and proc.returncode is not None and proc.returncode != 0:
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            raise BackendError(
                f"Docker command failed (exit {proc.returncode}): "
                f"{' '.join(cmd)}\n{stderr_text}"
            )
        assert proc.returncode is not None
        return proc.returncode, stdout, stderr

    async def _verify_readiness(self) -> None:
        """Verify the sandbox accepts exec calls and the shell works."""
        _, stdout, _ = await self._run_docker(
            "exec",
            self._container_id,
            self._shell,
            "-c",
            "echo kilntainers-ready",
            timeout=15,
        )
        if b"kilntainers-ready" not in stdout:
            raise BackendError(
                f"Container {self.sandbox_id} started but "
                f"readiness check failed (unexpected output)"
            )

    def _build_exec_command(self, request: ExecRequest) -> list[str]:
        """Build the docker exec argument list."""
        cmd = [*self._engine_prefix, "exec"]

        # -i keeps stdin open (needed when piping stdin data)
        if request.stdin is not None:
            cmd.append("-i")

        # -w sets the working directory inside the container
        if request.working_directory is not None:
            cmd.extend(["-w", request.working_directory])

        cmd.append(self._container_id)

        if request.command is not None:
            # Command mode: wrap in shell
            cmd.extend([self._shell, "-c", request.command])
        else:
            # Args mode: pass directly, no shell
            assert request.args is not None  # guaranteed by ExecRequest validation
            cmd.extend(request.args)

        return cmd

    async def _communicate_with_limit(
        self,
        proc: asyncio.subprocess.Process,
        stdin_data: bytes | None,
        output_limit: int,
    ) -> tuple[bytes, bytes, int]:
        """Read process output with combined size enforcement.

        Similar to proc.communicate() but monitors combined stdout+stderr
        byte count and raises _OutputLimitExceeded if the limit is breached.

        Returns (stdout_bytes, stderr_bytes, returncode).
        """
        # Write stdin if provided
        if stdin_data is not None and proc.stdin is not None:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
            proc.stdin.close()

        total_bytes = 0
        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        async def read_stream(
            stream: asyncio.StreamReader | None, chunks: list[bytes]
        ) -> None:
            nonlocal total_bytes
            if stream is None:
                return
            while True:
                chunk = await stream.read(8192)
                if not chunk:
                    break  # EOF
                total_bytes += len(chunk)
                if total_bytes > output_limit:
                    raise _OutputLimitExceeded()
                chunks.append(chunk)

        # Read both streams concurrently. TaskGroup cancels the other
        # reader if one raises _OutputLimitExceeded.
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(read_stream(proc.stdout, stdout_chunks))
                tg.create_task(read_stream(proc.stderr, stderr_chunks))
        except* _OutputLimitExceeded:
            # Re-raise to signal output limit exceeded
            raise

        await proc.wait()

        assert proc.returncode is not None
        return (
            b"".join(stdout_chunks),
            b"".join(stderr_chunks),
            proc.returncode,
        )

    async def _kill_subprocess(self, proc: asyncio.subprocess.Process) -> None:
        """Kill a subprocess and wait for it to exit."""
        try:
            proc.kill()  # SIGKILL
        except ProcessLookupError:
            pass  # Already exited
        await proc.wait()

    async def _is_container_running(self) -> bool:
        """Check if the container is still running."""
        returncode, stdout, _ = await self._run_docker(
            "inspect",
            "--format",
            "{{.State.Running}}",
            self._container_id,
            check=False,
            timeout=5,
        )
        if returncode != 0:
            return False
        return b"true" in stdout

    async def _do_exec(self, request: ExecRequest) -> ExecResult:
        """Core exec implementation."""
        cmd = self._build_exec_command(request)
        stdin_data = request.stdin.encode("utf-8") if request.stdin else None

        start_time = time.monotonic()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes, returncode = await asyncio.wait_for(
                self._communicate_with_limit(proc, stdin_data, request.output_limit),
                timeout=request.timeout,
            )
            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            # Check if container died during exec
            if returncode != 0 and not await self._is_container_running():
                if not self._stop_requested:
                    raise SandboxDiedError(
                        f"Sandbox {self.sandbox_id} died during command execution"
                    )

            return ExecResult(
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                exit_code=returncode,
                exec_duration_ms=elapsed_ms,
            )

        except asyncio.TimeoutError:
            await self._kill_subprocess(proc)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            return ExecResult(
                stdout="",
                stderr=f"[kilntainers: command timed out after {request.timeout}s]",
                exit_code=124,
                exec_duration_ms=elapsed_ms,
            )
        except ExceptionGroup as eg:
            # Check if the ExceptionGroup contains _OutputLimitExceeded
            for exc in eg.exceptions:
                if isinstance(exc, _OutputLimitExceeded):
                    await self._kill_subprocess(proc)
                    elapsed_ms = int((time.monotonic() - start_time) * 1000)
                    return ExecResult(
                        stdout="",
                        stderr=(
                            f"[kilntainers: output limit exceeded "
                            f"({request.output_limit} bytes). Command terminated. "
                            f"No output returned. Re-run with head, tail, or grep "
                            f"to manage output size.]"
                        ),
                        exit_code=1,
                        exec_duration_ms=elapsed_ms,
                    )
            # If not, re-raise the ExceptionGroup
            raise

    async def exec(self, request: ExecRequest) -> ExecResult:
        """Execute a command in the sandbox.

        Uses a lock to serialize exec calls within this sandbox.
        """
        if self._stopped:
            raise SandboxDiedError("Sandbox has been stopped")

        async with self._exec_lock:
            return await self._do_exec(request)

    async def stop(self) -> None:
        """Stop the sandbox and release all resources.

        Idempotent — safe to call on an already-stopped sandbox.
        """
        if self._stopped:
            return
        self._stopped = True
        self._stop_requested = True

        try:
            # docker stop sends SIGTERM, waits grace period, then SIGKILL
            proc = await asyncio.create_subprocess_exec(
                *self._engine_prefix,
                "stop",
                "-t",
                "5",
                self._container_id,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                # 5s Docker grace + 5s buffer for Docker overhead
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        except Exception:
            # Best-effort cleanup — don't propagate errors from stop
            pass

    async def wait_for_death(self) -> None:
        """Block until the sandbox dies unexpectedly.

        Returns when the container exits for reasons other than stop()
        being called. Does not return when stop() is called — in that
        case, blocks until cancelled by the MCP layer.
        """
        proc = await asyncio.create_subprocess_exec(
            *self._engine_prefix,
            "wait",
            self._container_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            await proc.wait()
        except asyncio.CancelledError:
            # Normal shutdown — MCP layer cancelled this task before stop()
            proc.kill()
            await proc.wait()
            raise

        if self._stop_requested:
            # Container exited because stop() was called — this is expected.
            # Block forever; the MCP layer will cancel this task.
            try:
                await asyncio.Future()  # never completes
            except asyncio.CancelledError:
                return

        # Container exited unexpectedly (OOM, external kill, daemon crash).
        # Returning signals death to the MCP layer.

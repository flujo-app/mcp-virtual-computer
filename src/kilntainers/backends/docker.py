"""Docker backend implementation."""

import argparse
import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib.resources import files
from typing import cast

from kilntainers.backends.base import (
    Backend,
    ComputerInfo,
    ExecRequest,
    ExecResult,
    Sandbox,
)
from kilntainers.computers import random_computer_id
from kilntainers.config import BackendConfig, env_flag
from kilntainers.errors import BackendError, SandboxDiedError
from kilntainers.windows_docker import (
    DockerRuntimeProgress,
    initial_docker_runtime_status,
    prepare_windows_docker_runtime,
)

DEFAULT_IMAGE = "debian:bookworm-slim"
DEFAULT_DESKTOP_IMAGE = "mcp-virtual-computer-desktop:bookworm"
COMPUTER_NAME_PREFIX = "kilntainer-"
COMPUTER_ID_LABEL = "kilntainers.computer-id"
TEMPORARY_LABEL = "kilntainers.temporary"
IMAGE_LABEL = "kilntainers.image"
DESKTOP_LABEL = "mcp-virtual-computer.desktop"
WORKSPACE_LABEL = "mcp-virtual-computer.workspace"
DESKTOP_MODE_FILE = "/var/lib/mcp-virtual-computer/desktop-enabled"
NETWORK_MODE_FILE = "/var/lib/mcp-virtual-computer/network-enabled"
DESKTOP_IMAGE_VERSION_LABEL = "mcp-virtual-computer.image-version"
DESKTOP_IMAGE_VERSION = "3"


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
    network_enabled: bool = True
    cpu: str | None = None
    memory: str | None = None
    docker_run_flags: list[str] = field(default_factory=list)
    desktop_environment: bool = False
    workspace_directory: str = "/workspace"


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
    desktop_environment: bool = False
    network_access: bool = True
    workspace_directory: str = "/workspace"
    desktop_host_port: int | None = None


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
            default=DEFAULT_DESKTOP_IMAGE,
            help=f"Docker image (default: {DEFAULT_DESKTOP_IMAGE})",
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
        desktop_environment = env_flag("DESKTOP_ENVIRONMENT", default=False)
        image = args.image
        network_access = env_flag("NETWORK_ACCESS", default=args.network)
        return DockerBackendConfig(
            engine=args.engine,
            host=args.docker_host,
            image=image,
            shell=shell,
            network_enabled=network_access,
            cpu=args.cpu,
            memory=args.memory,
            docker_run_flags=args.docker_run_flags or [],
            default_timeout=args.timeout,
            desktop_environment=desktop_environment,
            workspace_directory="/workspace",
        )

    def __init__(self, config: DockerBackendConfig) -> None:
        super().__init__(config)
        # Override parent's _config with more specific type for type checker
        self._config: DockerBackendConfig = config
        self._runtime_task: asyncio.Task[None] | None = None
        self._runtime_error: BackendError | None = None
        self._runtime_progress = initial_docker_runtime_status(config.engine)
        self._runtime_reporters: dict[
            Callable[[DockerRuntimeProgress], Awaitable[None]], float
        ] = {}
        self._runtime_notification_tasks: set[asyncio.Task[None]] = set()

    async def _notify_runtime_reporter(
        self,
        reporter: Callable[[DockerRuntimeProgress], Awaitable[None]],
        update: DockerRuntimeProgress,
    ) -> None:
        try:
            await reporter(update)
        except Exception:
            # Progress is best-effort and must never break runtime preparation.
            pass

    def _accept_runtime_progress(self, update: DockerRuntimeProgress) -> None:
        self._runtime_progress = update
        for reporter, previous in tuple(self._runtime_reporters.items()):
            if update.progress <= previous:
                continue
            self._runtime_reporters[reporter] = update.progress
            task = asyncio.create_task(self._notify_runtime_reporter(reporter, update))
            self._runtime_notification_tasks.add(task)
            task.add_done_callback(self._runtime_notification_tasks.discard)

    async def _prepare_windows_runtime(self) -> None:
        loop = asyncio.get_running_loop()
        latest = self._runtime_progress

        def report(update: DockerRuntimeProgress) -> None:
            nonlocal latest
            latest = update
            loop.call_soon_threadsafe(self._accept_runtime_progress, update)

        try:
            await asyncio.to_thread(
                prepare_windows_docker_runtime,
                engine=self._config.engine,
                host=self._config.host,
                report=report,
            )
            self._accept_runtime_progress(latest)
        except BackendError as error:
            self._runtime_error = error
            self._accept_runtime_progress(
                DockerRuntimeProgress(
                    state="failed",
                    phase="failed",
                    message="Docker setup failed.",
                    progress=self._runtime_progress.progress,
                    error=str(error),
                )
            )
        except Exception as error:
            wrapped = BackendError(f"Unexpected Docker setup failure: {error}")
            self._runtime_error = wrapped
            self._accept_runtime_progress(
                DockerRuntimeProgress(
                    state="failed",
                    phase="failed",
                    message="Docker setup failed.",
                    progress=self._runtime_progress.progress,
                    error=str(wrapped),
                )
            )

    def start_runtime_preparation(self) -> None:
        """Start the Windows Docker check/install task on first use."""
        if self._runtime_progress.state == "ready":
            return
        if self._runtime_task is not None and not self._runtime_task.done():
            return
        if self._runtime_error is not None:
            # A new user invocation is an explicit retry after external repair.
            self._runtime_error = None
            self._runtime_progress = initial_docker_runtime_status(self._config.engine)
        self._runtime_task = asyncio.create_task(self._prepare_windows_runtime())

    async def ensure_runtime(
        self,
        progress: Callable[[DockerRuntimeProgress], Awaitable[None]] | None = None,
    ) -> None:
        """Wait for lazy Docker preparation while preserving it on cancellation."""
        if self._runtime_progress.state == "ready":
            return
        self.start_runtime_preparation()
        if progress is not None:
            self._runtime_reporters[progress] = -1.0
            if self._runtime_progress.state != "pending":
                self._runtime_reporters[progress] = self._runtime_progress.progress
                await self._notify_runtime_reporter(progress, self._runtime_progress)
        try:
            assert self._runtime_task is not None
            await asyncio.shield(self._runtime_task)
        finally:
            if progress is not None:
                self._runtime_reporters.pop(progress, None)
        if self._runtime_error is not None:
            raise self._runtime_error

    def runtime_status(self) -> dict[str, object]:
        """Expose genuine Windows Docker setup state to the MCP App."""
        return self._runtime_progress.to_dict()

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
        await self.ensure_runtime()
        try:
            await self._run_docker("info", timeout=10)
        except BackendError:
            raise BackendError(
                f"Cannot connect to {self._config.engine}. "
                f"Is the {self._config.engine} daemon running?"
            )

    async def _ensure_image(
        self,
        image: str | None = None,
        *,
        desktop_environment: bool | None = None,
    ) -> None:
        """Pull the configured image if not available locally."""
        image = image or self._config.image
        desktop_environment = (
            self._config.desktop_environment
            if desktop_environment is None
            else desktop_environment
        )
        # Check if image exists locally
        returncode, stdout, _ = await self._run_docker(
            "image",
            "inspect",
            image,
            check=False,
            timeout=10,
        )
        if returncode == 0:
            if image != DEFAULT_DESKTOP_IMAGE:
                return
            try:
                image_data = json.loads(stdout)
                labels = image_data[0]["Config"]["Labels"]
                if (
                    isinstance(labels, dict)
                    and labels.get(DESKTOP_IMAGE_VERSION_LABEL)
                    == DESKTOP_IMAGE_VERSION
                ):
                    return
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                pass
            # Rebuild a stale bundled image under the same local tag. Running
            # persistent containers keep their writable layer and are untouched.

        if image == DEFAULT_DESKTOP_IMAGE:
            context = files("kilntainers").joinpath("desktop_image")
            try:
                await self._run_docker(
                    "build",
                    "--tag",
                    image,
                    str(context),
                    timeout=900,
                )
            except BackendError as error:
                raise BackendError(
                    "Failed to build the bundled Xfce desktop image. "
                    f"Docker reported: {error}"
                )
            return

        # Pull with progress output to stderr
        # Don't use _run_docker because we want stderr to pass through
        # to the parent process (for progress display to the user)
        proc = await asyncio.create_subprocess_exec(
            *self._engine_prefix,
            "pull",
            image,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=None,  # inherit parent stderr — shows pull progress
        )
        await proc.wait()
        if proc.returncode != 0:
            raise BackendError(
                f"Failed to pull image '{image}'. "
                f"Check that the image name is correct and the registry is reachable."
            )

    def _build_run_command(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
        desktop_environment: bool | None = None,
        network_access: bool | None = None,
        image: str | None = None,
    ) -> list[str]:
        """Build the docker run argument list."""
        computer_id = computer_id or random_computer_id()
        desktop_environment = (
            self._config.desktop_environment
            if desktop_environment is None
            else desktop_environment
        )
        network_access = (
            self._config.network_enabled if network_access is None else network_access
        )
        image = image or self._config.image
        desktop_capable = desktop_environment or image == DEFAULT_DESKTOP_IMAGE
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
            f"{IMAGE_LABEL}={image}",
            "--label",
            f"{DESKTOP_LABEL}={str(desktop_environment).lower()}",
            "--label",
            f"{WORKSPACE_LABEL}={self._config.workspace_directory}",
            "--name",
            f"{COMPUTER_NAME_PREFIX}{computer_id}",
            "--workdir",
            self._config.workspace_directory,
        ]

        if temporary:
            # Temporary computers disappear when stopped by session cleanup.
            cmd.append("--rm")

        # Plain headless images can use Docker's complete network namespace
        # isolation. Desktop-capable computers keep the bridge so their local
        # noVNC transport stays available while Xfce is toggled off; their
        # bundled entrypoint applies an OUTPUT firewall instead.
        if not network_access and not desktop_capable:
            cmd.extend(["--network", "none"])

        # Resource limits
        if self._config.cpu is not None:
            cmd.extend(["--cpus", self._config.cpu])
        if self._config.memory is not None:
            cmd.extend(["--memory", self._config.memory])

        if desktop_capable:
            # Bind the browser transport to loopback on an ephemeral host port.
            cmd.extend(
                [
                    "--init",
                    "--publish",
                    "127.0.0.1::6080",
                    "--shm-size",
                    "256m",
                    "--cap-add",
                    "NET_ADMIN",
                    "--env",
                    f"NETWORK_ACCESS={str(network_access).lower()}",
                    "--env",
                    f"DESKTOP_ENVIRONMENT={str(desktop_environment).lower()}",
                ]
            )

        # User-provided extra flags (escape hatch)
        for flag in self._config.docker_run_flags:
            cmd.append(flag)

        # Image and keep-alive command
        cmd.append(image)
        if not desktop_capable:
            cmd.extend(["tail", "-f", "/dev/null"])

        return cmd

    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
        desktop_environment: bool | None = None,
        network_access: bool | None = None,
        image: str | None = None,
    ) -> "DockerSandbox":
        """Create a Docker sandbox.

        Performs the full startup sequence:
        1. Ensure image is available (pull if needed)
        2. Build and run docker run command
        3. Create sandbox object
        4. Verify readiness (cleanup if fails)
        """
        computer_id = computer_id or random_computer_id()
        desktop_environment = (
            self._config.desktop_environment
            if desktop_environment is None
            else desktop_environment
        )
        network_access = (
            self._config.network_enabled if network_access is None else network_access
        )
        image = image or self._config.image
        desktop_capable = desktop_environment or image == DEFAULT_DESKTOP_IMAGE

        # 1. Ensure image is available (pull if needed)
        await self._ensure_image(image, desktop_environment=desktop_environment)

        # 2. Build docker run command
        cmd = self._build_run_command(
            computer_id=computer_id,
            temporary=temporary,
            desktop_environment=desktop_environment,
            network_access=network_access,
            image=image,
        )

        # 3. Create and start container
        _, stdout, _ = await self._run_docker(*cmd, timeout=30)
        container_id = stdout.decode().strip()
        try:
            desktop_host_port = (
                await self._published_desktop_port(container_id)
                if desktop_capable
                else None
            )
        except Exception:
            await self._run_docker(
                "rm",
                "-f",
                container_id,
                check=False,
                timeout=20,
            )
            raise

        # 4. Create sandbox state
        state = _DockerSandboxState(
            engine=self._config.engine,
            host=self._config.host,
            shell=self._config.shell,
            container_id=container_id,
            computer_id=computer_id,
            temporary=temporary,
            image=image,
            desktop_environment=desktop_environment,
            network_access=network_access,
            workspace_directory=self._config.workspace_directory,
            desktop_host_port=desktop_host_port,
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

    async def _published_desktop_port(self, container_id: str) -> int:
        """Return Docker's ephemeral loopback port for the noVNC websocket."""
        for _ in range(20):
            returncode, stdout, _ = await self._run_docker(
                "port",
                container_id,
                "6080/tcp",
                check=False,
                timeout=5,
            )
            if returncode == 0:
                address = stdout.decode("utf-8", errors="replace").strip().splitlines()
                if address:
                    try:
                        return int(address[0].rsplit(":", 1)[1])
                    except (IndexError, ValueError):
                        pass
            await asyncio.sleep(0.1)
        raise BackendError("Docker did not publish the desktop websocket port.")

    @staticmethod
    def _desktop_port_from_inspect(data: dict[str, object]) -> int | None:
        network = data.get("NetworkSettings", {})
        network = (
            cast("dict[str, object]", network) if isinstance(network, dict) else {}
        )
        ports = network.get("Ports", {})
        ports = cast("dict[str, object]", ports) if isinstance(ports, dict) else {}
        bindings = ports.get("6080/tcp")
        if not isinstance(bindings, list) or not bindings:
            return None
        binding = bindings[0]
        if not isinstance(binding, dict):
            return None
        binding = cast("dict[str, object]", binding)
        try:
            return int(str(binding.get("HostPort", "")))
        except ValueError:
            return None

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
        desktop_environment = str(labels.get(DESKTOP_LABEL, "false")).lower() == "true"
        workspace_directory = str(
            labels.get(WORKSPACE_LABEL) or self._config.workspace_directory
        )
        network_data = data.get("NetworkSettings", {})
        network_data = (
            cast("dict[str, object]", network_data)
            if isinstance(network_data, dict)
            else {}
        )
        networks = network_data.get("Networks", {})
        networks = (
            cast("dict[str, object]", networks) if isinstance(networks, dict) else {}
        )
        desktop_host_port = self._desktop_port_from_inspect(data)
        desktop_capable = desktop_host_port is not None
        if desktop_capable:
            raw_environment = config.get("Env", [])
            environment = (
                cast("list[object]", raw_environment)
                if isinstance(raw_environment, list)
                else []
            )
            configured_network = next(
                (
                    str(value).split("=", 1)[1]
                    for value in environment
                    if isinstance(value, str) and value.startswith("NETWORK_ACCESS=")
                ),
                "true",
            )
            network_access = configured_network.casefold() == "true"
        else:
            network_access = (
                any(name != "none" for name in networks)
                if networks
                else self._config.network_enabled
            )
        return DockerSandbox(
            _DockerSandboxState(
                engine=self._config.engine,
                host=self._config.host,
                shell=self._config.shell,
                container_id=str(data.get("Id", "")),
                computer_id=computer_id,
                temporary=temporary,
                image=image,
                desktop_environment=desktop_environment,
                network_access=network_access,
                workspace_directory=workspace_directory,
                desktop_host_port=desktop_host_port,
            )
        )

    async def _read_desktop_mode(self, sandbox: "DockerSandbox") -> bool:
        """Read the live usable mode, falling back to the old label."""
        if sandbox._desktop_host_port is None:
            sandbox._desktop_environment = False
            return False
        returncode, stdout, _ = await self._run_docker(
            "exec",
            sandbox._container_id,
            "sh",
            "-c",
            f"cat {DESKTOP_MODE_FILE}",
            check=False,
            timeout=5,
        )
        if returncode == 0:
            value = stdout.decode("utf-8", errors="replace").strip().casefold()
            if value in {"true", "false"}:
                sandbox._desktop_environment = value == "true"
        if sandbox._desktop_environment:
            # The state file records intent. Do not advertise Xfce unless both
            # the desktop session and VNC backend are genuinely available.
            returncode, _, _ = await self._run_docker(
                "exec",
                sandbox._container_id,
                "sh",
                "-c",
                self._desktop_ready_check(),
                check=False,
                timeout=5,
            )
            sandbox._desktop_environment = returncode == 0
        return sandbox.desktop_environment

    async def _read_network_access(self, sandbox: "DockerSandbox") -> bool:
        """Read the live desktop firewall state without reapplying defaults."""
        if sandbox._desktop_host_port is None:
            return sandbox.network_access
        returncode, stdout, _ = await self._run_docker(
            "exec",
            sandbox._container_id,
            "sh",
            "-c",
            "if [ -f /run/mcp-network-disabled ]; then "
            "printf false; else printf true; fi",
            check=False,
            timeout=5,
        )
        if returncode == 0:
            sandbox._network_access = (
                stdout.decode("utf-8", errors="replace").strip().casefold()
                == "true"
            )
            value = str(sandbox.network_access).lower()
            await self._run_docker(
                "exec",
                "-u",
                "0",
                sandbox._container_id,
                "sh",
                "-c",
                "install -d -o computer -g computer "
                f"$(dirname {NETWORK_MODE_FILE}); "
                f"printf '%s\\n' {value} > {NETWORK_MODE_FILE}; "
                f"chown computer:computer {NETWORK_MODE_FILE}",
                timeout=5,
            )
        return sandbox.network_access

    async def _sync_desktop_helpers(self, sandbox: "DockerSandbox") -> None:
        """Upgrade helper scripts in place without restarting the computer."""
        if (
            sandbox._desktop_host_port is None
            or sandbox.image != DEFAULT_DESKTOP_IMAGE
        ):
            return
        context = files("kilntainers").joinpath("desktop_image")
        for source_name, destination_name in (
            ("desktop-control.py", "desktop-control"),
            ("start-desktop.sh", "start-desktop"),
        ):
            await self._run_docker(
                "cp",
                str(context.joinpath(source_name)),
                f"{sandbox._container_id}:/usr/local/bin/{destination_name}",
                timeout=15,
            )
        await self._run_docker(
            "exec",
            "-u",
            "0",
            sandbox._container_id,
            "chmod",
            "0755",
            "/usr/local/bin/desktop-control",
            "/usr/local/bin/start-desktop",
            timeout=10,
        )

    @staticmethod
    def _desktop_ready_check() -> str:
        """Shell predicate for a usable Xfce session and its VNC transport."""
        return (
            "ps -C xfce4-session -o stat= 2>/dev/null "
            "| grep -qv '^[[:space:]]*Z' && "
            "ps -C x11vnc -o stat= 2>/dev/null "
            "| grep -qv '^[[:space:]]*Z' && "
            "pgrep -f '[p]ython3 /usr/local/bin/wsproxy' >/dev/null"
        )

    async def _set_desktop_mode(
        self,
        sandbox: "DockerSandbox",
        enabled: bool,
    ) -> "DockerSandbox":
        """Start or stop Xfce inside one desktop-capable container."""
        if sandbox._desktop_host_port is None or sandbox.image != DEFAULT_DESKTOP_IMAGE:
            if enabled:
                raise BackendError(
                    "This existing computer was created from a headless or custom "
                    "image and cannot start Xfce in place. Its data was left "
                    "untouched; use factory_reset_computer only if replacing it "
                    "is intentional."
                )
            sandbox._desktop_environment = False
            return sandbox

        desired = str(enabled).lower()
        script = (
            "install -d -o computer -g computer "
            f"$(dirname {DESKTOP_MODE_FILE}); "
            f"printf '%s\\n' {desired} > {DESKTOP_MODE_FILE}; "
            f"chown computer:computer {DESKTOP_MODE_FILE}"
        )
        await self._run_docker(
            "exec",
            "-u",
            "0",
            sandbox._container_id,
            "sh",
            "-c",
            script,
            timeout=10,
        )

        # The supervisor checks the marker five times per second. Wait for the
        # actual session state so MCP callers never receive a fictional mode.
        state_check = (
            self._desktop_ready_check()
            if enabled
            else (
                "! ps -C xfce4-session -o stat= 2>/dev/null | grep -qv '^[[:space:]]*Z'"
            )
        )
        for _ in range(75):
            returncode, _, _ = await self._run_docker(
                "exec",
                sandbox._container_id,
                "sh",
                "-c",
                state_check,
                check=False,
                timeout=5,
            )
            if returncode == 0:
                sandbox._desktop_environment = enabled
                return sandbox
            await asyncio.sleep(0.2)
        raise BackendError(
            f"Xfce did not {'start' if enabled else 'stop'} within 15 seconds. "
            "The computer itself is still running and was not replaced."
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
        await self._sync_desktop_helpers(sandbox)
        await self._read_desktop_mode(sandbox)
        await self._read_network_access(sandbox)
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
        await self._read_desktop_mode(sandbox)
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
        current = self._sandbox_from_inspect(data)
        await self._read_desktop_mode(current)
        await self.delete_computer(computer_id)
        return await self._create_sandbox(
            computer_id=computer_id,
            temporary=temporary,
            desktop_environment=current.desktop_environment,
            network_access=current.network_access,
            image=current.image,
        )

    async def _set_desktop_firewall(
        self,
        sandbox: "DockerSandbox",
        enabled: bool,
    ) -> None:
        """Apply the desktop image's outbound-only firewall."""
        if enabled:
            script = (
                "command -v iptables >/dev/null 2>&1 || exit 0; "
                "iptables -D OUTPUT -j MCP_NO_NETWORK 2>/dev/null || true; "
                "iptables -F MCP_NO_NETWORK 2>/dev/null || true; "
                "iptables -X MCP_NO_NETWORK 2>/dev/null || true; "
                "rm -f /run/mcp-network-disabled; "
                f"printf 'true\\n' > {NETWORK_MODE_FILE}; "
                f"chown computer:computer {NETWORK_MODE_FILE}"
            )
        else:
            script = (
                "command -v iptables >/dev/null 2>&1 || { "
                "echo 'desktop image has no iptables support' >&2; exit 45; }; "
                "iptables -N MCP_NO_NETWORK 2>/dev/null || true; "
                "iptables -F MCP_NO_NETWORK; "
                "iptables -A MCP_NO_NETWORK -o lo -j ACCEPT; "
                "iptables -A MCP_NO_NETWORK -m conntrack "
                "--ctstate ESTABLISHED,RELATED -j ACCEPT; "
                "iptables -A MCP_NO_NETWORK -j REJECT; "
                "iptables -C OUTPUT -j MCP_NO_NETWORK 2>/dev/null || "
                "iptables -I OUTPUT 1 -j MCP_NO_NETWORK; "
                "touch /run/mcp-network-disabled; "
                f"printf 'false\\n' > {NETWORK_MODE_FILE}; "
                f"chown computer:computer {NETWORK_MODE_FILE}"
            )
        try:
            await self._run_docker(
                "exec",
                "-u",
                "0",
                sandbox._container_id,
                "sh",
                "-c",
                script,
                timeout=15,
            )
        except BackendError as error:
            raise BackendError(
                "Could not change desktop network access. Rebuild the bundled "
                f"desktop image and retry. Docker reported: {error}"
            )

    async def _set_headless_network(
        self,
        sandbox: "DockerSandbox",
        enabled: bool,
    ) -> None:
        """Attach a headless computer to bridge or Docker's none network."""
        data = await self._inspect_computer(sandbox.computer_id)
        if data is None:
            raise BackendError(f"Computer '{sandbox.computer_id}' was not found.")
        network_data = data.get("NetworkSettings", {})
        network_data = (
            cast("dict[str, object]", network_data)
            if isinstance(network_data, dict)
            else {}
        )
        raw_networks = network_data.get("Networks", {})
        networks = (
            list(cast("dict[str, object]", raw_networks))
            if isinstance(raw_networks, dict)
            else []
        )
        if not networks:
            # A real Docker inspect always includes a network entry. Keeping an
            # empty synthetic inspect unchanged makes provider test doubles and
            # third-party Docker-compatible engines safe to attach.
            return
        container_id = sandbox._container_id
        if enabled:
            if any(name != "none" for name in networks):
                return
            if "none" in networks:
                await self._run_docker("network", "disconnect", "none", container_id)
            try:
                await self._run_docker("network", "connect", "bridge", container_id)
            except Exception:
                await self._run_docker(
                    "network", "connect", "none", container_id, check=False
                )
                raise
            return

        if networks == ["none"]:
            return
        detached: list[str] = []
        try:
            for network in networks:
                if network == "none":
                    continue
                await self._run_docker("network", "disconnect", network, container_id)
                detached.append(network)
            await self._run_docker("network", "connect", "none", container_id)
        except Exception:
            for network in detached:
                await self._run_docker(
                    "network", "connect", network, container_id, check=False
                )
            raise

    async def _set_network_access_on_sandbox(
        self,
        sandbox: "DockerSandbox",
        enabled: bool,
    ) -> "DockerSandbox":
        if sandbox._desktop_host_port is not None:
            await self._set_desktop_firewall(sandbox, enabled)
        else:
            await self._set_headless_network(sandbox, enabled)
        sandbox._network_access = enabled
        return sandbox

    async def set_network_access(
        self,
        computer_id: str,
        enabled: bool,
    ) -> "DockerSandbox | None":
        """Really enable or isolate outbound traffic for a live computer."""
        data = await self._inspect_computer(computer_id)
        if data is None:
            return None
        sandbox = self._sandbox_from_inspect(data)
        await sandbox._verify_readiness()
        await self._read_desktop_mode(sandbox)
        return await self._set_network_access_on_sandbox(sandbox, enabled)

    async def switch_desktop_environment(
        self,
        computer_id: str,
        enabled: bool,
        *,
        network_access: bool | None = None,
    ) -> "DockerSandbox | None":
        """Start or stop Xfce without replacing the Docker computer."""
        data = await self._inspect_computer(computer_id)
        if data is None:
            return None
        current = self._sandbox_from_inspect(data)
        await current._verify_readiness()
        await self._read_desktop_mode(current)
        network_access = (
            current.network_access if network_access is None else network_access
        )
        if current.desktop_environment != enabled:
            await self._set_desktop_mode(current, enabled)
        return await self._set_network_access_on_sandbox(current, network_access)

    def tool_instructions(self) -> str | None:
        """Return tool description for Docker backend.

        Returns None if using a custom image (baked-in description only
        applies to default Debian image).
        """
        if self._config.image not in {DEFAULT_IMAGE, DEFAULT_DESKTOP_IMAGE}:
            return None

        shell_name = self._config.shell.rsplit("/", 1)[-1]  # basename
        timeout = self._config.default_timeout

        return (
            f"Execute a shell command in one persistent Debian Docker computer. "
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
        self._desktop_environment = state.desktop_environment
        self._network_access = state.network_access
        self._workspace_directory = state.workspace_directory
        self._desktop_host_port = state.desktop_host_port
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

    @property
    def desktop_url(self) -> str | None:
        if not self._desktop_environment or self._desktop_host_port is None:
            return None
        return f"ws://127.0.0.1:{self._desktop_host_port}/websockify"

    @property
    def desktop_environment(self) -> bool:
        return self._desktop_environment

    @property
    def network_access(self) -> bool:
        return self._network_access

    @property
    def image(self) -> str | None:
        return self._image

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

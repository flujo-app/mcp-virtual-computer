"""Fly Machines backend for one persistent remote Xfce computer."""

import argparse
import asyncio
import base64
import json
import os
import secrets
import shlex
import ssl
import time
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import cast

import certifi
from websockets.asyncio.client import connect as connect_websocket
from websockets.typing import Subprotocol

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
from kilntainers.fly_runtime import ensure_flyctl

DEFAULT_FLY_IMAGE = "mcp-virtual-computer-desktop:fly"
COMPUTER_ID_METADATA = "kilntainers-computer-id"
TEMPORARY_METADATA = "kilntainers-temporary"
IMAGE_METADATA = "kilntainers-image"
DESKTOP_METADATA = "mcp-virtual-computer-desktop"
NETWORK_METADATA = "mcp-virtual-computer-network"
VNC_TOKEN_METADATA = "mcp-virtual-computer-vnc-token"
DESKTOP_MODE_FILE = "/var/lib/mcp-virtual-computer/desktop-enabled"
NETWORK_MODE_FILE = "/var/lib/mcp-virtual-computer/network-enabled"
DESKTOP_CONTAINER_PORT = 6080


@dataclass(frozen=True, slots=True, kw_only=True)
class FlyBackendConfig(BackendConfig):
    """Configuration for Fly Machines orchestration through flyctl."""

    fly_cli: str = "fly"
    app: str | None = None
    org: str | None = None
    token: str | None = None
    image: str | None = None
    region: str | None = None
    shell: str = "/bin/bash"
    cpu_kind: str = "shared"
    cpus: int = 1
    memory_mb: int = 1024
    rootfs_size_gb: int | None = None
    desktop_environment: bool = True
    network_enabled: bool = True
    workspace_directory: str = "/workspace"
    computer_id: str = "virtual-computer"


@dataclass(frozen=True, slots=True, kw_only=True)
class _FlySandboxState:
    machine_id: str
    computer_id: str
    temporary: bool
    image: str
    desktop_environment: bool
    network_access: bool
    vnc_token: str


class FlyBackend(Backend):
    """Provision and manage a persistent Xfce Fly Machine."""

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
            help=(
                "Fly App that owns sandbox Machines (default: FLY_APP_NAME, "
                "otherwise create and remember one automatically)"
            ),
        )
        group.add_argument(
            "--fly-org",
            default=os.getenv("FLY_ORG"),
            help="Fly organization (default: FLY_ORG, personal, or first available)",
        )
        group.add_argument(
            "--fly-token",
            default=os.getenv("FLY_API_TOKEN") or os.getenv("FLY_TOKEN"),
            help="Fly API token (default: FLY_API_TOKEN or FLY_TOKEN)",
        )
        group.add_argument(
            "--fly-image",
            default=None,
            help="Prebuilt Fly Machine image (default: build the bundled Xfce image)",
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
            default=1024,
            dest="fly_memory",
            help="Fly Machine memory in MB (default: 1024)",
        )
        group.add_argument(
            "--fly-rootfs-size",
            type=int,
            default=None,
            help="Optional Fly Machine root filesystem size in GB",
        )

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        desktop_environment = env_flag("DESKTOP_ENVIRONMENT", default=True)
        network_access = env_flag("NETWORK_ACCESS", default=args.network)
        return FlyBackendConfig(
            fly_cli=args.fly_cli,
            app=args.fly_app,
            org=args.fly_org,
            token=args.fly_token,
            image=args.fly_image,
            region=args.fly_region,
            shell=args.shell,
            cpu_kind=args.fly_cpu_kind,
            cpus=args.fly_cpus,
            memory_mb=args.fly_memory,
            rootfs_size_gb=args.fly_rootfs_size,
            default_timeout=args.timeout,
            desktop_environment=desktop_environment,
            network_enabled=network_access,
            workspace_directory="/workspace",
            computer_id=os.getenv("COMPUTER_ID", "virtual-computer"),
        )

    def __init__(self, config: FlyBackendConfig) -> None:
        super().__init__(config)
        self._config: FlyBackendConfig = config
        self._fly_cli = config.fly_cli
        self._app = config.app

    @property
    def app(self) -> str | None:
        """Resolved Fly App, including an automatically created app."""
        return self._app

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
        cwd: Path | None = None,
    ) -> tuple[int, bytes, bytes]:
        """Run flyctl without putting the API token in the process arguments."""
        cmd = [self._fly_cli, *args]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._fly_env(),
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError:
            raise BackendError(
                f"Fly CLI '{self._fly_cli}' was not found. Install flyctl or pass "
                "--fly-cli."
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

    @staticmethod
    def _json_rows(payload: object, *container_names: str) -> list[dict[str, object]]:
        if isinstance(payload, dict):
            payload_dict = cast("dict[str, object]", payload)
            for name in container_names:
                nested = payload_dict.get(name)
                if isinstance(nested, list):
                    payload = nested
                    break
        if not isinstance(payload, list):
            return []
        return [cast("dict[str, object]", row) for row in payload if isinstance(row, dict)]

    @staticmethod
    def _named_value(row: dict[str, object], *names: str) -> str | None:
        for name in names:
            value = row.get(name)
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _state_file() -> Path:
        override = os.getenv("KILNTAINERS_FLY_STATE_FILE")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".mcp-virtual-computer" / "fly.json"

    def _load_saved_app(self) -> str | None:
        try:
            payload = json.loads(self._state_file().read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        apps = payload.get("apps")
        if not isinstance(apps, dict):
            return None
        app = apps.get(self._config.computer_id)
        return app if isinstance(app, str) and app else None

    def _save_app(self, app: str) -> None:
        destination = self._state_file()
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        try:
            payload = json.loads(destination.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        raw_apps = payload.get("apps")
        apps = dict(raw_apps) if isinstance(raw_apps, dict) else {}
        apps[self._config.computer_id] = app
        temporary.write_text(json.dumps({"apps": apps}, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)

    async def _visible_apps(self) -> set[str]:
        _, stdout, _ = await self._run_fly("apps", "list", "--json", timeout=20)
        try:
            payload = json.loads(stdout.decode("utf-8") or "[]")
        except json.JSONDecodeError as error:
            raise BackendError("Fly CLI returned invalid app inventory JSON.") from error
        rows = self._json_rows(payload, "apps", "Apps")
        return {
            name
            for row in rows
            if (name := self._named_value(row, "name", "Name", "app_name", "AppName"))
        }

    async def _default_org(self) -> str:
        if self._config.org:
            return self._config.org
        _, stdout, _ = await self._run_fly("orgs", "list", "--json", timeout=20)
        try:
            payload = json.loads(stdout.decode("utf-8") or "[]")
        except json.JSONDecodeError as error:
            raise BackendError("Fly CLI returned invalid organization JSON.") from error
        rows = self._json_rows(payload, "orgs", "Orgs", "organizations")
        slugs = [
            slug
            for row in rows
            if (slug := self._named_value(row, "slug", "Slug", "name", "Name"))
        ]
        if isinstance(payload, dict) and not slugs:
            # Current flyctl releases return {"slug": "display name"} here.
            slugs = [
                slug
                for slug, display_name in payload.items()
                if isinstance(slug, str)
                and slug
                and isinstance(display_name, str)
            ]
        if not slugs:
            raise BackendError(
                "No Fly organization is available for this account. Create one in "
                "Fly.io or set FLY_ORG."
            )
        return "personal" if "personal" in slugs else slugs[0]

    async def _create_app(self) -> str:
        org = await self._default_org()
        _, stdout, _ = await self._run_fly(
            "apps",
            "create",
            "--generate-name",
            "--org",
            org,
            "--json",
            "--yes",
            timeout=45,
        )
        try:
            payload = json.loads(stdout.decode("utf-8") or "{}")
        except json.JSONDecodeError as error:
            raise BackendError("Fly created an app but returned invalid JSON.") from error
        rows = self._json_rows(payload, "apps", "Apps")
        if isinstance(payload, dict):
            rows.insert(0, cast("dict[str, object]", payload))
        app = next(
            (
                name
                for row in rows
                if (name := self._named_value(row, "name", "Name", "app_name", "AppName"))
            ),
            None,
        )
        if not app:
            raise BackendError("Fly created an app but did not return its name.")
        self._save_app(app)
        return app

    async def _resolve_app(self) -> None:
        visible = await self._visible_apps()
        if self._app:
            if self._app not in visible:
                raise BackendError(
                    f"Fly App '{self._app}' is not visible to the current account or token."
                )
            return
        saved = self._load_saved_app()
        if saved and saved in visible:
            self._app = saved
            return
        self._app = await self._create_app()

    async def _validate(self) -> None:
        if self._config.cpus < 1:
            raise BackendError("--fly-cpus must be at least 1.")
        if self._config.memory_mb < 256:
            raise BackendError("--fly-memory must be at least 256 MB.")
        self._fly_cli = await asyncio.to_thread(ensure_flyctl, self._config.fly_cli)
        await self._run_fly("version", timeout=10)
        returncode, _, stderr = await self._run_fly(
            "auth", "whoami", "--json", check=False, timeout=20
        )
        if returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            raise BackendError(
                "flyctl is installed but is not signed in. Run "
                f"'{self._fly_cli} auth login' once, or set FLY_API_TOKEN."
                + (f"\n{detail}" if detail else "")
            )
        await self._resolve_app()
        await self._list_machine_rows()

    async def _list_machine_rows(self) -> list[dict[str, object]]:
        if not self._app:
            return []
        _, stdout, _ = await self._run_fly(
            "machine",
            "list",
            "--app",
            self._app,
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
        temporary = str(metadata.get(TEMPORARY_METADATA, "false")).lower() == "true"
        image = str(metadata.get(IMAGE_METADATA) or self._config.image or DEFAULT_FLY_IMAGE)
        desktop_environment = (
            str(metadata.get(DESKTOP_METADATA, "true")).lower() == "true"
        )
        network_access = str(metadata.get(NETWORK_METADATA, "true")).lower() == "true"
        vnc_token = str(metadata.get(VNC_TOKEN_METADATA, ""))
        machine_id = str(self._value(row, "id", "ID", "machine_id") or "")
        return FlySandbox(
            self,
            _FlySandboxState(
                machine_id=machine_id,
                computer_id=computer_id,
                temporary=temporary,
                image=image,
                desktop_environment=desktop_environment,
                network_access=network_access,
                vnc_token=vnc_token,
            ),
        )

    async def _ensure_public_ips(self) -> None:
        """Ensure the app has the free shared IPv4 and public IPv6 routes."""
        if not self._app:
            raise BackendError("Fly App is not configured.")
        _, stdout, _ = await self._run_fly(
            "ips", "list", "--app", self._app, "--json", timeout=20
        )
        try:
            payload = json.loads(stdout.decode("utf-8") or "[]")
        except json.JSONDecodeError as error:
            raise BackendError("Fly CLI returned invalid IP inventory JSON.") from error
        rows = self._json_rows(payload, "ips", "IPs", "addresses")
        def version_of(row: dict[str, object]) -> str:
            raw = self._value(row, "version", "Version", "family", "Family")
            return str(raw or "").casefold()

        descriptions = [json.dumps(row).casefold() for row in rows]
        versions = [version_of(row) for row in rows]
        has_v4 = any(value in {"4", "v4", "ipv4"} for value in versions) or any(
            "ipv4" in item or '"v4"' in item for item in descriptions
        )
        has_v6 = any(
            value in {"6", "v6", "ipv6"} and "private" not in description
            for value, description in zip(versions, descriptions, strict=True)
        ) or any(
            ("ipv6" in item or '"v6"' in item) and "private" not in item
            for item in descriptions
        )
        if not has_v4:
            await self._run_fly(
                "ips", "allocate-v4", "--shared", "--yes", "--app", self._app,
                timeout=30,
            )
        if not has_v6:
            await self._run_fly(
                "ips", "allocate-v6", "--app", self._app, timeout=30
            )

    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> "FlySandbox":
        if not self._app:  # validated by create_sandbox
            raise BackendError("Fly App is not configured.")
        computer_id = computer_id or random_computer_id()
        if self._computer_row(await self._list_machine_rows(), computer_id) is not None:
            raise BackendError(f"Computer '{computer_id}' already exists in Fly.")
        await self._ensure_public_ips()

        # The virtual-computer product is intentionally permanent. The server
        # always asks for temporary=false, but enforce the invariant here too.
        temporary = False
        vnc_token = secrets.token_urlsafe(24)
        image_name = self._config.image or DEFAULT_FLY_IMAGE
        args = [
            "machine",
            "run",
            "--app",
            self._app,
            "--name",
            computer_id,
            "--detach",
            "--metadata",
            "kilntainers=true",
            "--metadata",
            f"{COMPUTER_ID_METADATA}={computer_id}",
            "--metadata",
            f"{TEMPORARY_METADATA}={str(temporary).lower()}",
            "--metadata",
            f"{IMAGE_METADATA}={image_name}",
            "--metadata",
            f"{DESKTOP_METADATA}={str(self._config.desktop_environment).lower()}",
            "--metadata",
            f"{NETWORK_METADATA}={str(self._config.network_enabled).lower()}",
            "--metadata",
            f"{VNC_TOKEN_METADATA}={vnc_token}",
            "--env",
            f"DESKTOP_ENVIRONMENT={str(self._config.desktop_environment).lower()}",
            "--env",
            f"NETWORK_ACCESS={str(self._config.network_enabled).lower()}",
            "--env",
            f"VNC_PATH_TOKEN={vnc_token}",
            "--port",
            "80:6080/tcp:http",
            "--port",
            "443:6080/tcp:http:tls",
            "--vm-cpu-kind",
            self._config.cpu_kind,
            "--vm-cpus",
            str(self._config.cpus),
            "--vm-memory",
            str(self._config.memory_mb),
            "--rootfs-persist",
            "always",
            "--restart",
            "always",
        ]
        if self._config.region:
            args.extend(["--region", self._config.region])
        if self._config.rootfs_size_gb is not None:
            args.extend(["--rootfs-size", str(self._config.rootfs_size_gb)])
        build_context: Path | None = None
        if self._config.image:
            args.append(self._config.image)
        else:
            build_context = Path(str(files("kilntainers").joinpath("desktop_image")))
            if not build_context.joinpath("Dockerfile").is_file():
                raise BackendError("The bundled Xfce Fly build context is unavailable.")
            args.append(".")
        await self._run_fly(*args, timeout=900, cwd=build_context)

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
            await self._read_runtime_modes(sandbox)
            if sandbox.desktop_environment:
                await sandbox._verify_desktop_transport()
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
            assert self._app is not None
            await self._run_fly(
                "machine", "start", machine_id, "--app", self._app, timeout=45
            )
            row = self._computer_row(await self._list_machine_rows(), computer_id)
            if row is None:  # pragma: no cover - provider race
                raise BackendError(
                    f"Computer '{computer_id}' disappeared while starting."
                )
        sandbox = self._row_to_sandbox(row)
        if not sandbox._vnc_token:
            raise BackendError(
                f"Fly computer '{computer_id}' predates protected VNC support. "
                "Factory-reset or delete it once to rebuild the Xfce Machine."
            )
        await self._ensure_public_ips()
        await sandbox._verify_readiness()
        await self._read_runtime_modes(sandbox)
        if sandbox.desktop_environment:
            await sandbox._verify_desktop_transport()
        return sandbox

    async def refresh_sandbox(
        self,
        computer_id: str,
        sandbox: Sandbox,
    ) -> "FlySandbox":
        if not isinstance(sandbox, FlySandbox):
            replacement = await self.attach_sandbox(computer_id)
            if replacement is None:
                raise BackendError(f"Computer '{computer_id}' was not found.")
            return replacement
        await self._read_runtime_modes(sandbox)
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
        assert self._app is not None
        machine_id = str(self._value(row, "id", "ID", "machine_id") or "")
        await self._run_fly(
            "machine", "restart", machine_id, "--app", self._app, timeout=60
        )
        refreshed = self._computer_row(await self._list_machine_rows(), computer_id)
        if refreshed is None:  # pragma: no cover - provider race
            raise BackendError(f"Computer '{computer_id}' disappeared after restart.")
        return self._row_to_sandbox(refreshed)

    async def delete_computer(self, computer_id: str) -> bool:
        row = self._computer_row(await self._list_machine_rows(), computer_id)
        if row is None:
            return False
        assert self._app is not None
        machine_id = str(self._value(row, "id", "ID", "machine_id") or "")
        await self._run_fly(
            "machine",
            "destroy",
            "--force",
            "--app",
            self._app,
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

    @staticmethod
    def _desktop_ready_check() -> str:
        return (
            "ps -C xfce4-session -o stat= 2>/dev/null "
            "| grep -qv '^[[:space:]]*Z' && "
            "ps -C x11vnc -o stat= 2>/dev/null "
            "| grep -qv '^[[:space:]]*Z' && "
            "pgrep -f '[p]ython3 /usr/local/bin/wsproxy' >/dev/null"
        )

    async def _mode_command(
        self,
        sandbox: "FlySandbox",
        command: str,
        *,
        timeout: int = 20,
    ) -> ExecResult:
        return await sandbox._do_exec(
            ExecRequest(command=command, timeout=timeout, output_limit=32_768)
        )

    async def _read_runtime_modes(self, sandbox: "FlySandbox") -> None:
        script = (
            f"desktop=$(cat {DESKTOP_MODE_FILE} 2>/dev/null || printf false); "
            f"if [ \"$desktop\" = true ] && ({self._desktop_ready_check()}); then "
            "desktop=true; else desktop=false; fi; "
            "if [ -f /run/mcp-network-disabled ]; then network=false; "
            "else network=true; fi; "
            "printf '%s\\n%s\\n' \"$desktop\" \"$network\""
        )
        result = await self._mode_command(sandbox, script)
        if result.exit_code != 0:
            raise BackendError(
                f"Could not read Xfce state from Fly Machine: {result.stderr or result.stdout}"
            )
        values = result.stdout.splitlines()
        if len(values) >= 2:
            sandbox._desktop_environment = values[0].strip().casefold() == "true"
            sandbox._network_access = values[1].strip().casefold() == "true"

    async def set_network_access(
        self,
        computer_id: str,
        enabled: bool,
    ) -> "FlySandbox | None":
        sandbox = await self.attach_sandbox(computer_id)
        if sandbox is None:
            return None
        if enabled:
            script = (
                "command -v iptables >/dev/null 2>&1 || exit 0; "
                "iptables -D INPUT -j MCP_NO_NETWORK_IN 2>/dev/null || true; "
                "iptables -D OUTPUT -j MCP_NO_NETWORK_OUT 2>/dev/null || true; "
                "iptables -F MCP_NO_NETWORK_IN 2>/dev/null || true; "
                "iptables -X MCP_NO_NETWORK_IN 2>/dev/null || true; "
                "iptables -F MCP_NO_NETWORK_OUT 2>/dev/null || true; "
                "iptables -X MCP_NO_NETWORK_OUT 2>/dev/null || true; "
                "rm -f /run/mcp-network-disabled; "
                f"printf 'true\\n' > {NETWORK_MODE_FILE}; "
                f"chown computer:computer {NETWORK_MODE_FILE}"
            )
        else:
            script = (
                "command -v iptables >/dev/null 2>&1 || { "
                "echo 'desktop image has no iptables support' >&2; exit 45; }; "
                "iptables -D INPUT -j MCP_NO_NETWORK_IN 2>/dev/null || true; "
                "iptables -D OUTPUT -j MCP_NO_NETWORK_OUT 2>/dev/null || true; "
                "iptables -N MCP_NO_NETWORK_IN 2>/dev/null || true; "
                "iptables -F MCP_NO_NETWORK_IN; "
                "iptables -A MCP_NO_NETWORK_IN -i lo -j ACCEPT; "
                f"iptables -A MCP_NO_NETWORK_IN -p tcp --dport {DESKTOP_CONTAINER_PORT} -j ACCEPT; "
                "iptables -A MCP_NO_NETWORK_IN -p tcp -j REJECT --reject-with tcp-reset; "
                "iptables -A MCP_NO_NETWORK_IN -j REJECT; "
                "iptables -N MCP_NO_NETWORK_OUT 2>/dev/null || true; "
                "iptables -F MCP_NO_NETWORK_OUT; "
                "iptables -A MCP_NO_NETWORK_OUT -o lo -j ACCEPT; "
                f"iptables -A MCP_NO_NETWORK_OUT -p tcp --sport {DESKTOP_CONTAINER_PORT} -j ACCEPT; "
                "iptables -A MCP_NO_NETWORK_OUT -p tcp -j REJECT --reject-with tcp-reset; "
                "iptables -A MCP_NO_NETWORK_OUT -j REJECT; "
                "iptables -I INPUT 1 -j MCP_NO_NETWORK_IN; "
                "iptables -I OUTPUT 1 -j MCP_NO_NETWORK_OUT; "
                "touch /run/mcp-network-disabled; "
                f"printf 'false\\n' > {NETWORK_MODE_FILE}; "
                f"chown computer:computer {NETWORK_MODE_FILE}"
            )
        result = await self._mode_command(sandbox, script)
        if result.exit_code != 0:
            raise BackendError(
                f"Could not change Fly computer network access: {result.stderr or result.stdout}"
            )
        sandbox._network_access = enabled
        return sandbox

    async def switch_desktop_environment(
        self,
        computer_id: str,
        enabled: bool,
        *,
        network_access: bool | None = None,
    ) -> "FlySandbox | None":
        sandbox = await self.attach_sandbox(computer_id)
        if sandbox is None:
            return None
        desired = str(enabled).lower()
        result = await self._mode_command(
            sandbox,
            "install -d -o computer -g computer "
            f"$(dirname {DESKTOP_MODE_FILE}); "
            f"printf '%s\\n' {desired} > {DESKTOP_MODE_FILE}; "
            f"chown computer:computer {DESKTOP_MODE_FILE}",
        )
        if result.exit_code != 0:
            raise BackendError(
                f"Could not change Xfce mode on Fly: {result.stderr or result.stdout}"
            )
        expected = self._desktop_ready_check() if enabled else (
            "! ps -C xfce4-session -o stat= 2>/dev/null "
            "| grep -qv '^[[:space:]]*Z'"
        )
        for _ in range(75):
            state = await self._mode_command(sandbox, expected, timeout=5)
            if state.exit_code == 0:
                sandbox._desktop_environment = enabled
                break
            await asyncio.sleep(0.2)
        else:
            raise BackendError(
                f"Xfce did not {'start' if enabled else 'stop'} within 15 seconds."
            )
        if network_access is not None and network_access != sandbox.network_access:
            updated = await self.set_network_access(computer_id, network_access)
            if updated is not None:
                sandbox._network_access = updated.network_access
        return sandbox

    def tool_instructions(self) -> str | None:
        return (
            "Execute shell commands in one persistent Debian Xfce computer hosted "
            "as a Fly Machine. Its writable root filesystem, workspace, desktop, "
            "and stable computer ID survive MCP restarts."
        )


class FlySandbox(Sandbox):
    """Command and lifecycle handle for one Fly Machine."""

    def __init__(self, backend: FlyBackend, state: _FlySandboxState) -> None:
        self._backend = backend
        self._machine_id = state.machine_id
        self._computer_id = state.computer_id
        self._temporary = state.temporary
        self._image = state.image
        self._desktop_environment = state.desktop_environment
        self._network_access = state.network_access
        self._vnc_token = state.vnc_token
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

    @property
    def desktop_url(self) -> str | None:
        if not self._desktop_environment or not self._vnc_token or not self._backend.app:
            return None
        return (
            f"wss://{self._backend.app}.fly.dev/"
            f"{self._vnc_token}/websockify"
        )

    @property
    def desktop_environment(self) -> bool:
        return self._desktop_environment

    @property
    def network_access(self) -> bool:
        return self._network_access

    @property
    def image(self) -> str:
        return self._image

    def _remote_command(self, request: ExecRequest) -> str:
        if request.command is not None:
            command = request.command
        else:
            assert request.args is not None
            command = shlex.join(request.args)
        if request.stdin is not None:
            encoded = base64.b64encode(request.stdin.encode("utf-8")).decode("ascii")
            command = f"printf %s {shlex.quote(encoded)} | base64 -d | {command}"
        if request.working_directory:
            command = f"cd -- {shlex.quote(request.working_directory)} && {command}"
        # fly machine exec tokenizes its command rather than evaluating shell
        # operators. Wrap every request so cwd, stdin, pipes, and argv quoting
        # retain their normal shell semantics on every host OS.
        return f"{shlex.quote(self._backend._config.shell)} -lc {shlex.quote(command)}"

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
        if not self._backend.app:
            raise BackendError("Fly App is not configured.")
        started = time.monotonic()
        returncode, stdout, stderr = await self._backend._run_fly(
            "machine",
            "exec",
            "--app",
            self._backend.app,
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

    async def _verify_desktop_transport(self) -> None:
        """Wait until Fly TLS routing reaches the protected noVNC websocket."""
        url = self.desktop_url
        if url is None:
            raise BackendError("The Fly Xfce Machine has no protected VNC URL.")
        last_error = "no response"
        tls_context = ssl.create_default_context(cafile=certifi.where())
        for _ in range(30):
            try:
                async with connect_websocket(
                    url,
                    ssl=tls_context,
                    subprotocols=[cast(Subprotocol, "binary")],
                    open_timeout=5,
                    close_timeout=1,
                    max_size=None,
                ) as websocket:
                    greeting = await asyncio.wait_for(websocket.recv(), timeout=5)
                    if isinstance(greeting, bytes) and greeting.startswith(b"RFB "):
                        return
                    last_error = "the server did not return an RFB greeting"
            except Exception as error:
                last_error = str(error)
            await asyncio.sleep(2)
        raise BackendError(
            "The Fly Machine is running, but its protected VNC websocket did not "
            f"become reachable within 60 seconds: {last_error}"
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
        if not self._backend.app:
            return
        try:
            if self._temporary:
                await self._backend._run_fly(
                    "machine",
                    "destroy",
                    "--force",
                    "--app",
                    self._backend.app,
                    self._machine_id,
                    timeout=60,
                )
            # Permanent virtual computers intentionally keep running when the
            # MCP client disconnects so their desktop remains reachable.
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

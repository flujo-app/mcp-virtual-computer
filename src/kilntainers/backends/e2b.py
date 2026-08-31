"""E2B backend implementation."""

import argparse
import asyncio
import shlex
import time
from dataclasses import dataclass

from e2b import AsyncSandbox
from e2b.exceptions import TimeoutException
from e2b.sandbox.commands.command_handle import CommandExitException

from kilntainers.backends.base import Backend, ExecRequest, ExecResult, Sandbox
from kilntainers.config import BackendConfig
from kilntainers.errors import BackendError, SandboxDiedError


@dataclass(frozen=True, slots=True, kw_only=True)
class E2BBackendConfig(BackendConfig):
    """Configuration for the E2B backend.

    Populated from CLI args by E2BBackend.config_from_args().
    """

    # Authentication (optional — falls back to E2B_API_KEY env var)
    api_key: str | None = None

    # Sandbox template
    template: str = "base"
    shell: str = "/bin/bash"
    network_enabled: bool = False

    # Sandbox lifetime
    sandbox_timeout: int = 3600  # seconds

    # Custom metadata
    metadata: dict[str, str] | None = None

    # Environment variables for sandbox
    envs: dict[str, str] | None = None


class E2BBackend(Backend):
    """E2B backend implementation.

    Manages E2B cloud sandbox lifecycle and command execution
    through the E2B Python SDK.
    """

    @classmethod
    def add_cli_arguments(cls, group: argparse._ArgumentGroup) -> None:
        """Register E2B-specific CLI arguments."""
        group.add_argument(
            "--e2b-api-key",
            default=None,
            help="E2B API key (overrides E2B_API_KEY environment variable)",
        )
        group.add_argument(
            "--e2b-template",
            default="base",
            help="E2B template name or ID (default: base)",
        )
        group.add_argument(
            "--e2b-sandbox-timeout",
            type=int,
            default=3600,
            help="Sandbox lifetime timeout in seconds (default: 3600)",
        )
        group.add_argument(
            "--e2b-metadata",
            action="append",
            default=None,
            help="Metadata key=value pairs (can be used multiple times)",
        )
        group.add_argument(
            "--e2b-env",
            action="append",
            default=None,
            help="Environment variable key=value pairs (can be used multiple times)",
        )

    @classmethod
    def config_from_args(cls, args: argparse.Namespace) -> BackendConfig:
        """Build E2BBackendConfig from parsed CLI arguments."""
        # Parse metadata key=value pairs
        metadata = None
        if args.e2b_metadata:
            metadata = {}
            for pair in args.e2b_metadata:
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    metadata[key] = value

        # Parse env key=value pairs
        envs = None
        if args.e2b_env:
            envs = {}
            for pair in args.e2b_env:
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    envs[key] = value

        # Use core --shell with a backend-specific default
        shell = args.shell if args.shell is not None else "/bin/bash"

        return E2BBackendConfig(
            api_key=args.e2b_api_key,
            template=args.e2b_template,
            shell=shell,
            network_enabled=args.network,
            sandbox_timeout=args.e2b_sandbox_timeout,
            metadata=metadata,
            envs=envs,
            default_timeout=args.timeout,
        )

    def __init__(self, config: E2BBackendConfig) -> None:
        super().__init__(config)
        # Override parent's _config with more specific type for type checker
        self._config: E2BBackendConfig = config

    def _get_api_params(self) -> dict:
        """Get API parameters for E2B SDK calls."""
        params = {}
        if self._config.api_key is not None:
            params["api_key"] = self._config.api_key
        return params

    async def _validate(self) -> None:
        """Validate E2B prerequisites.

        Checks that E2B authentication is configured and the API is reachable.
        """
        try:
            # Validate auth by listing sandboxes and fetching one page.
            # AsyncSandbox.list() is lazy and returns a paginator; next_items()
            # forces the API call so auth/connection errors surface here.
            paginator = AsyncSandbox.list(limit=1, **self._get_api_params())
            next_items = getattr(paginator, "next_items", None)
            if callable(next_items):
                await next_items()
        except Exception as e:
            raise BackendError(f"E2B validation failed: {e}")

    async def _create_sandbox(
        self,
        *,
        computer_id: str | None = None,
        temporary: bool = True,
    ) -> "E2BSandbox":
        """Create an E2B sandbox.

        Performs the full startup sequence:
        1. Build creation parameters
        2. Create sandbox via SDK
        3. Create sandbox wrapper
        4. Verify readiness (cleanup if fails)
        """
        # Build creation parameters with explicit typing for type checker
        template: str = self._config.template
        timeout: int = self._config.sandbox_timeout
        allow_internet_access: bool = self._config.network_enabled

        try:
            sb = await AsyncSandbox.create(
                template=template,
                timeout=timeout,
                allow_internet_access=allow_internet_access,
                metadata=self._config.metadata,
                envs=self._config.envs,
                **self._get_api_params(),
            )
        except Exception as e:
            raise BackendError(f"Failed to create E2B sandbox: {e}")

        # Create sandbox wrapper
        sandbox = E2BSandbox(
            e2b_sandbox=sb,
            shell=self._config.shell,
        )

        # Verify readiness
        try:
            await sandbox._verify_readiness()
        except Exception:
            await sandbox.stop()
            raise

        return sandbox

    def tool_instructions(self) -> str | None:
        """Return tool description for E2B backend.

        Returns None if using a custom template (baked-in description only
        applies to default 'base' template).
        """
        if self._config.template != "base":
            return None

        shell_name = self._config.shell.rsplit("/", 1)[-1]  # basename
        timeout = self._config.default_timeout

        return (
            f"Execute a shell command in a remote cloud sandbox (E2B). "
            f"Commands run in {shell_name}. Each call is independent — "
            f"no state (shell variables, working directory) persists between calls. Use the working_directory "
            f"parameter or chain commands with && to control execution context.\n\n"
            f"To write files or pass data without shell escaping, use the "
            f'stdin parameter (e.g., command="cat > file.txt" with content '
            f"in stdin). Commands time out after {timeout} seconds by default "
            f"(override with the timeout parameter for long-running operations)."
        )


class E2BSandbox(Sandbox):
    """E2B sandbox implementation.

    Wraps a running E2B Sandbox. Handles command execution with
    timeout and output-limit enforcement, stop, and death detection.
    """

    def __init__(
        self,
        *,
        e2b_sandbox: "E2BSandbox._E2BSandboxType",
        shell: str,
    ) -> None:
        self._e2b_sandbox = e2b_sandbox
        self._shell = shell
        self._stopped = False
        self._stop_requested = False
        self._exec_lock = asyncio.Lock()

    @property
    def sandbox_id(self) -> str:
        """Return E2B's sandbox_id for this sandbox."""
        return self._e2b_sandbox.sandbox_id

    async def _verify_readiness(self) -> None:
        """Verify the sandbox accepts exec calls and the shell works."""
        try:
            result = await self._e2b_sandbox.commands.run(
                f"{self._shell} -c 'echo kilntainers-ready'",
                timeout=10,
            )
            stdout = result.stdout or ""
        except Exception as e:
            raise BackendError(f"Sandbox readiness check failed: {e}")

        if "kilntainers-ready" not in stdout:
            raise BackendError(
                f"Sandbox started but readiness check failed (unexpected output). "
                f"Shell '{self._shell}' may not be available in the template. "
                f"Use --shell /bin/sh for templates without bash."
            )

    def _build_command(self, request: ExecRequest) -> str:
        """Build the command string for E2B commands.run()."""
        if request.command is not None:
            # Command mode: wrap in shell
            return f"{self._shell} -c {shlex.quote(request.command)}"
        else:
            # Args mode: join arguments with proper quoting
            assert request.args is not None
            return " ".join(shlex.quote(arg) for arg in request.args)

    def _output_limit_result(
        self,
        stdout: str,
        stderr: str,
        request: ExecRequest,
        start_time: float,
    ) -> ExecResult | None:
        """Return an ExecResult if combined output exceeds the limit, else None."""
        combined_size = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
        if combined_size > request.output_limit:
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
        return None

    def _build_run_kwargs(self, request: ExecRequest) -> dict:
        """Build keyword args for E2B commands.run()."""
        kwargs: dict = {
            "timeout": request.timeout,
        }
        if request.working_directory is not None:
            kwargs["cwd"] = request.working_directory
        return kwargs

    async def _do_exec(self, request: ExecRequest) -> ExecResult:
        """Core exec implementation."""
        cmd = self._build_command(request)
        run_kwargs = self._build_run_kwargs(request)
        stdin_data = request.stdin  # capture for type narrowing

        if stdin_data is not None:
            # Pipe stdin through SDK's native stdin mechanism rather than
            # shell-escaping user data into the command string.
            # Use head -c to read exactly the right number of bytes, providing
            # EOF to the downstream command (the SDK has no close-stdin API).
            stdin_byte_count = len(stdin_data.encode("utf-8"))
            cmd = f"head -c {stdin_byte_count} | {cmd}"

        start_time = time.monotonic()

        try:
            if stdin_data is not None:
                # Run in background with stdin enabled
                handle = await self._e2b_sandbox.commands.run(
                    cmd,
                    background=True,
                    stdin=True,
                    **run_kwargs,
                )
                # Send data through SDK — never touches shell string
                await self._e2b_sandbox.commands.send_stdin(handle.pid, stdin_data)
                # Wait for command to complete
                result = await handle.wait()
            else:
                # No stdin: foreground run waits for completion
                result = await self._e2b_sandbox.commands.run(
                    cmd,
                    **run_kwargs,
                )

            stdout_str = result.stdout or ""
            stderr_str = result.stderr or ""
            exit_code = result.exit_code

            # Check output limit
            limit_result = self._output_limit_result(
                stdout_str, stderr_str, request, start_time
            )
            if limit_result is not None:
                return limit_result

            elapsed_ms = int((time.monotonic() - start_time) * 1000)

            return ExecResult(
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=exit_code,
                exec_duration_ms=elapsed_ms,
            )

        except TimeoutException:
            # E2B SDK raises TimeoutException when command exceeds timeout
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            return ExecResult(
                stdout="",
                stderr=f"[kilntainers: command timed out after {request.timeout}s]",
                exit_code=124,
                exec_duration_ms=elapsed_ms,
            )

        except CommandExitException as e:
            # E2B SDK raises this for non-zero exit codes
            # Convert to ExecResult with the exit code and output
            stdout_str = e.stdout or ""
            stderr_str = e.stderr or ""

            # Check output limit
            limit_result = self._output_limit_result(
                stdout_str, stderr_str, request, start_time
            )
            if limit_result is not None:
                return limit_result

            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            return ExecResult(
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=e.exit_code,
                exec_duration_ms=elapsed_ms,
            )

        except asyncio.TimeoutError:
            # Fallback for asyncio-level timeouts (shouldn't normally happen)
            elapsed_ms = int((time.monotonic() - start_time) * 1000)
            return ExecResult(
                stdout="",
                stderr=f"[kilntainers: command timed out after {request.timeout}s]",
                exit_code=124,
                exec_duration_ms=elapsed_ms,
            )

        except Exception as e:
            error_msg = str(e).lower()
            if "sandbox" in error_msg and (
                "killed" in error_msg or "terminated" in error_msg
            ):
                if not self._stop_requested:
                    raise SandboxDiedError(
                        f"Sandbox {self.sandbox_id} died during command execution: {e}"
                    )
                raise SandboxDiedError("Sandbox has been stopped")
            # Re-raise unexpected errors
            raise

    async def exec(self, request: ExecRequest) -> ExecResult:
        """Execute a command in the sandbox.

        Uses a lock to serialize exec calls within this sandbox.
        Death is detected at exec time by checking the _stopped flag.
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
            await asyncio.wait_for(
                self._e2b_sandbox.kill(),  # ty: ignore[no-matching-overload]
                timeout=10,
            )
        except asyncio.TimeoutError:
            pass  # Best-effort — E2B may take time to terminate
        except Exception:
            pass  # Best-effort cleanup

    async def wait_for_death(self) -> None:
        """Block until cancelled.

        E2B doesn't provide a monitoring API. We don't poll E2B for sandbox status.
        Instead, sandbox death is detected at exec time when the E2B SDK
        raises an error. This method just blocks forever until cancelled
        by the MCP layer during normal shutdown.
        """
        try:
            # Block forever until cancelled
            await asyncio.Future()
        except asyncio.CancelledError:
            return

    # Type alias for the E2B Sandbox type (for type hints)
    _E2BSandboxType = AsyncSandbox

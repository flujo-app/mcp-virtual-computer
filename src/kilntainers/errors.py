"""Exception hierarchy for kilntainers."""


class KilntainersError(Exception):
    """Base exception for all Kilntainers errors.

    Catching KilntainersError catches any error originating from
    Kilntainers code (as opposed to stdlib or third-party exceptions).
    """

    pass


class BackendError(KilntainersError):
    """Raised by backend operations when something goes wrong.

    Used for:
    - Prerequisite validation failures (Docker not running)
    - Sandbox startup failures (image pull failed, readiness check failed)
    - Internal backend errors (unexpected Docker CLI failure)

    The message MUST be actionable — tell the operator what to fix.
    """

    pass


class SandboxDiedError(KilntainersError):
    """Raised when a sandbox has died unexpectedly.

    Raised by Sandbox.exec() if the sandbox is dead (detected before
    or during execution). The MCP layer catches this, returns an
    isError: true response, and drops the connection.
    """

    pass

"""Backend implementations and registry."""

import importlib.metadata

from kilntainers.backends.base import Backend
from kilntainers.errors import BackendError


def _discover_entry_points() -> dict[str, importlib.metadata.EntryPoint]:
    """Discover registered backends via entry points.

    Returns a dict mapping backend names to their EntryPoint objects.
    Entry points are not loaded (imported) at discovery time.
    """
    eps = importlib.metadata.entry_points(group="kilntainers.backends")
    discovered = {ep.name: ep for ep in eps}
    # Source checkouts can be newer than an already-running editable install
    # (especially on Windows, where the live CLI launcher cannot be replaced).
    # Keep the built-in Fly backend discoverable without weakening third-party
    # entry-point support.
    discovered.setdefault(
        "fly",
        importlib.metadata.EntryPoint(
            name="fly",
            value="kilntainers.backends.fly:FlyBackend",
            group="kilntainers.backends",
        ),
    )
    return discovered


# Discovered at import time (fast — no imports, just metadata scan)
_ENTRY_POINTS = _discover_entry_points()


def get_backend_class(name: str) -> type[Backend]:
    """Look up and load a backend class by name.

    Raises KeyError if the backend name is not registered.
    Raises BackendError if the backend's dependencies are not installed.

    Args:
        name: The backend name from the --backend CLI argument.

    Returns:
        The Backend subclass for the given name.

    Raises:
        KeyError: If the backend name is not in the registry.
        BackendError: If the backend's dependencies are not installed.
    """
    if name not in _ENTRY_POINTS:
        available = ", ".join(sorted(_ENTRY_POINTS.keys()))
        raise KeyError(f"Unknown backend: '{name}'. Available backends: {available}")

    ep = _ENTRY_POINTS[name]
    try:
        cls = ep.load()
    except ImportError:
        raise BackendError(
            f"Backend '{name}' requires additional dependencies. "
            f"Install with: uv add kilntainers[{_get_extra_for_backend(name)}]"
        )

    if not (isinstance(cls, type) and issubclass(cls, Backend)):
        raise BackendError(f"Entry point '{name}' does not point to a Backend subclass")

    return cls


def get_available_backend_names() -> list[str]:
    """Return names of all discovered backends (for --backend choices)."""
    return sorted(_ENTRY_POINTS.keys())


def _get_extra_for_backend(name: str) -> str:
    """Return the pip extra name for a backend.

    Maps backend names to their optional dependency group names.
    Only WASM backends have optional dependencies.
    """
    extra_mapping = {
        "wasm": "wasm",
        "go_busybox": "wasm",
    }
    return extra_mapping.get(name, "")

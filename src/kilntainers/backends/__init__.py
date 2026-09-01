"""Docker-only backend registry for the first virtual-computer slice."""

from kilntainers.backends.base import Backend


def get_backend_class(name: str) -> type[Backend]:
    """Return the only supported backend."""
    if name != "docker":
        raise KeyError(f"Unknown backend {name!r}. Available backends: docker")
    from kilntainers.backends.docker import DockerBackend

    return DockerBackend


def get_available_backend_names() -> list[str]:
    """Return the deliberately narrow backend surface."""
    return ["docker"]

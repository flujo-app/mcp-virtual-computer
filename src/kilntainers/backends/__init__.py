"""Built-in backend registry for local Docker and remote Fly computers."""

from kilntainers.backends.base import Backend


def get_backend_class(name: str) -> type[Backend]:
    """Return a built-in backend class."""
    if name == "docker":
        from kilntainers.backends.docker import DockerBackend

        return DockerBackend
    if name == "fly":
        from kilntainers.backends.fly import FlyBackend

        return FlyBackend
    raise KeyError(f"Unknown backend {name!r}. Available backends: docker, fly")


def get_available_backend_names() -> list[str]:
    """Return the supported persistent-computer backends."""
    return ["docker", "fly"]

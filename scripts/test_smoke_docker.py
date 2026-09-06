"""Deletion boundaries for the disposable Docker acceptance fixture."""

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_spec = importlib.util.spec_from_file_location(
    "smoke_docker", Path(__file__).with_name("smoke-docker.py")
)
assert _spec is not None and _spec.loader is not None
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)

RUN_ID = "a" * 32
COMPUTER_ID = f"mcp-smoke-{RUN_ID}"
CONTAINER_ID = "b" * 64


def fixture_record() -> dict[str, Any]:
    return {
        "Id": CONTAINER_ID,
        "Name": f"/kilntainer-{COMPUTER_ID}",
        "Config": {
            "Image": "debian:bookworm-slim",
            "Labels": {
                "kilntainers": "true",
                "kilntainers.computer-id": COMPUTER_ID,
                "kilntainers.temporary": "false",
                "kilntainers.smoke-run": RUN_ID,
            },
        },
    }


@pytest.mark.parametrize("mismatch", ["name", "id", "computer", "run", "image"])
def test_cleanup_refuses_foreign_container(monkeypatch, mismatch):
    record = copy.deepcopy(fixture_record())
    if mismatch == "name":
        record["Name"] = "/kilntainer-agent-workstation"
    elif mismatch == "id":
        record["Id"] = "c" * 64
    elif mismatch == "computer":
        record["Config"]["Labels"]["kilntainers.computer-id"] = "agent-workstation"
    elif mismatch == "run":
        record["Config"]["Labels"]["kilntainers.smoke-run"] = "other-run"
    else:
        record["Config"]["Image"] = "another-image"
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("container", "ls"):
            return CONTAINER_ID + "\n"
        if args[:2] == ("container", "inspect"):
            return json.dumps([record])
        raise AssertionError("Must never remove an unverified container")

    monkeypatch.setattr(smoke, "docker", docker)
    with pytest.raises(RuntimeError, match="ownership mismatch"):
        smoke.cleanup(COMPUTER_ID, RUN_ID)
    assert not any("rm" in args for args in calls)


def test_cleanup_removes_only_verified_immutable_id(monkeypatch):
    calls = []
    removed = False

    def docker(*args, **kwargs):
        nonlocal removed
        calls.append(args)
        if args[:2] == ("container", "ls"):
            return "" if removed else CONTAINER_ID + "\n"
        if args[:2] == ("container", "inspect"):
            return json.dumps([fixture_record()])
        assert args == ("container", "rm", "--force", CONTAINER_ID)
        removed = True
        return CONTAINER_ID + "\n"

    monkeypatch.setattr(smoke, "docker", docker)
    smoke.cleanup(COMPUTER_ID, RUN_ID)
    assert [args for args in calls if "rm" in args] == [
        ("container", "rm", "--force", CONTAINER_ID)
    ]


def test_cleanup_never_queries_non_smoke_identity(monkeypatch):
    def docker(*args, **kwargs):
        raise AssertionError("A non-smoke identity must never reach Docker")

    monkeypatch.setattr(smoke, "docker", docker)
    with pytest.raises(RuntimeError, match="non-smoke"):
        smoke.cleanup("agent-workstation", RUN_ID)

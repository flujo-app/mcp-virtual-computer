"""WSL first-use regressions. No test can launch an elevated process."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kilntainers import windows_wsl as wsl
from kilntainers.errors import BackendError

_REAL_MACHINE_STATUS = wsl._machine_status


@pytest.mark.integration
@pytest.mark.skipif(wsl.sys.platform != "win32", reason="Requires Windows PowerShell and CIM")
def test_machine_status_query_executes_on_windows():
    """Exercise the real read-only script so PowerShell syntax errors are caught."""
    status = _REAL_MACHINE_STATUS()
    assert isinstance(status["hypervisor"], bool)
    assert isinstance(status["virtualization"], list)
    assert "virtualMachinePlatform" in status


@pytest.fixture(autouse=True)
def windows(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        wsl.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(
            product_type=1,
            build=26100,
        ),
        raising=False,
    )
    monkeypatch.setattr(
        wsl,
        "_machine_status",
        lambda: {
            "hypervisor": True,
            "virtualization": [True],
            "virtualMachinePlatform": 1,
        },
    )
    monkeypatch.setattr(wsl, "_wsl_version", lambda: (2, 6, 0))
    configure = MagicMock(return_value=0)
    monkeypatch.setattr(wsl, "_configure_wsl", configure)
    return configure


def test_healthy_wsl_never_elevates(windows):
    wsl.prepare_windows_wsl(lambda message: None)
    windows.assert_not_called()


def test_windows_server_is_rejected_before_install(monkeypatch, windows):
    monkeypatch.setattr(
        wsl.sys,
        "getwindowsversion",
        lambda: SimpleNamespace(product_type=3, build=26100),
    )
    with pytest.raises(BackendError, match="not Windows Server"):
        wsl.prepare_windows_wsl(lambda message: None)
    windows.assert_not_called()


def test_firmware_virtualization_error_is_actionable(monkeypatch, windows):
    monkeypatch.setattr(
        wsl,
        "_machine_status",
        lambda: {
            "hypervisor": False,
            "virtualization": [False],
            "virtualMachinePlatform": 2,
        },
    )
    with pytest.raises(BackendError, match="BIOS/UEFI"):
        wsl.prepare_windows_wsl(lambda message: None)
    windows.assert_not_called()


def test_missing_wsl_installs_without_a_linux_distribution(monkeypatch, windows):
    versions = iter([None, (2, 6, 0)])
    monkeypatch.setattr(wsl, "_wsl_version", lambda: next(versions))
    messages = []
    wsl.prepare_windows_wsl(messages.append)
    windows.assert_called_once_with(install=True)
    assert any("administrator prompt" in message for message in messages)


def test_old_wsl_updates(monkeypatch, windows):
    versions = iter([(1, 2, 0), (2, 6, 0)])
    monkeypatch.setattr(wsl, "_wsl_version", lambda: next(versions))
    wsl.prepare_windows_wsl(lambda message: None)
    windows.assert_called_once_with(install=False)


@pytest.mark.parametrize(
    ("code", "message"),
    [(1223, "cancelled"), (3010, "restart"), (1641, "restart"), (1, "failed")],
)
def test_setup_outcomes_are_not_reported_ready(monkeypatch, windows, code, message):
    monkeypatch.setattr(wsl, "_wsl_version", lambda: None)
    windows.return_value = code
    with pytest.raises(BackendError, match=message):
        wsl.prepare_windows_wsl(lambda message: None)
    windows.assert_called_once()


def test_successful_install_waits_for_reboot(monkeypatch, windows):
    monkeypatch.setattr(
        wsl,
        "_machine_status",
        lambda: {
            "hypervisor": False,
            "virtualization": [True],
            "virtualMachinePlatform": 2,
        },
    )
    with pytest.raises(BackendError, match="Restart Windows"):
        wsl.prepare_windows_wsl(lambda message: None)
    windows.assert_called_once_with(install=True)


def test_wsl_output_is_decoded_before_parsing():
    text = "WSL-Version: 2.6.0.0\r\nKernelversion: 6.6.87.2"
    assert wsl.decode_windows_output(text.encode("utf-16-le")) == text
    assert wsl.decode_windows_output(text.encode("utf-16")) == text
    assert wsl.decode_windows_output(text.encode()) == text


def test_localized_version_does_not_pick_the_kernel(monkeypatch):
    monkeypatch.setattr(
        wsl.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout="WSL-Version: 2.6.0.0\r\nKernel: 6.6.87.2".encode("utf-16-le"),
        ),
    )
    assert wsl._wsl_version() == (2, 6, 0)

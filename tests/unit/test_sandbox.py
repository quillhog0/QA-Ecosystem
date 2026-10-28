from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core.orchestrator import Vesper
from core.packets import Verdict
from core.sandbox import (
    get_sandbox_status,
    is_bwrap_available,
    resolve_sandbox_mode,
    wrap_command,
)
from tests.support import mini_project


def test_build_bwrap_command_without_network(tmp_path: Path):
    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()

    cmd = ["python", "-m", "pytest", "-q"]
    wrapped = wrap_command(
        cmd,
        isolated_root=iso,
        work_dir=work,
        python=Path("/usr/bin/python3"),
        allow_network=False,
        bwrap_path="/usr/bin/bwrap",
    )

    assert wrapped[0] == "/usr/bin/bwrap"
    assert "--unshare-all" in wrapped
    assert "--share-net" not in wrapped
    assert "--die-with-parent" in wrapped

    # Read-only system bindings
    assert "--ro-bind" in wrapped
    assert "/usr" in wrapped

    # tmpfs for /tmp and /home
    assert "--tmpfs" in wrapped
    assert "/tmp" in wrapped
    assert "/home" in wrapped

    # remount /home read-only
    assert "--remount-ro" in wrapped
    assert "/home" in wrapped

    # Read-write bindings for iso and work
    assert "--bind" in wrapped
    assert str(iso.resolve()) in wrapped
    assert str(work.resolve()) in wrapped

    # Home set to sandbox home and chdir to iso
    assert "--setenv" in wrapped
    assert "/home/sandbox" in wrapped
    assert "--chdir" in wrapped
    assert str(iso.resolve()) in wrapped

    # Trailing command
    assert wrapped[-4:] == ["python", "-m", "pytest", "-q"]


def test_build_bwrap_command_with_network(tmp_path: Path):
    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()

    cmd = ["pytest"]
    wrapped = wrap_command(
        cmd,
        isolated_root=iso,
        work_dir=work,
        allow_network=True,
        bwrap_path="bwrap",
    )

    assert "--unshare-all" in wrapped
    assert "--share-net" in wrapped
    assert wrapped[-1] == "pytest"


def test_sandbox_mode_resolution():
    assert resolve_sandbox_mode(None) == "auto"
    assert resolve_sandbox_mode({}) == "auto"
    assert resolve_sandbox_mode({"sandbox": {"mode": "required"}}) == "required"
    assert resolve_sandbox_mode({"sandbox": {"mode": "off"}}) == "off"
    assert resolve_sandbox_mode({"sandbox": {"mode": "UNKNOWN"}}) == "auto"

    # Status tests
    assert get_sandbox_status({"sandbox": {"mode": "off"}}) == (False, "off", None)

    with patch("core.sandbox.is_bwrap_available", return_value=False):
        assert get_sandbox_status({"sandbox": {"mode": "auto"}}) == (False, "unavailable", None)
        enabled, status, err = get_sandbox_status({"sandbox": {"mode": "required"}})
        assert enabled is False
        assert status == "unavailable"
        assert err is not None
        assert "required" in err

    with patch("core.sandbox.is_bwrap_available", return_value=True):
        assert get_sandbox_status({"sandbox": {"mode": "auto"}}) == (True, "bwrap", None)
        assert get_sandbox_status({"sandbox": {"mode": "required"}}) == (True, "bwrap", None)


def test_sandbox_mode_required_vetoes_when_bwrap_missing(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    reports = tmp_path / "reports"
    config = {
        "sandbox": {"mode": "required"},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    with patch("core.sandbox.is_bwrap_available", return_value=False):
        report = Vesper(config).run(project, project / "docs")

    assert report.verdict == Verdict.VETO.value
    assert report.sandbox == "unavailable"
    assert any("sandbox required" in r.summary for r in report.role_results)


def test_project_config_cannot_disable_sandbox(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    # Tested project tries to disable sandbox
    (project / "config.yaml").write_text("sandbox:\n  mode: off\n", encoding="utf-8")
    reports = tmp_path / "reports"
    system_config = {
        "sandbox": {"mode": "auto"},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(system_config).run(project, project / "docs")

    if is_bwrap_available():
        assert report.sandbox == "bwrap"
    else:
        assert report.sandbox == "unavailable"


@pytest.mark.skipif(not is_bwrap_available(), reason="bwrap is not available on this system")
def test_bwrap_integration_blocks_home_network_and_outside_reads(tmp_path: Path):
    project = mini_project(tmp_path / "sec_proj")
    tests_dir = project / "tests"

    test_content = '''import os
import socket
from pathlib import Path
import pytest

def test_write_to_home_fails():
    home = Path(os.environ.get("HOME", "/home/sandbox"))
    with pytest.raises(OSError):
        (home / "should_never_exist.txt").write_text("pwn")

def test_open_socket_fails():
    with pytest.raises((OSError, RuntimeError)):
        s = socket.socket()
        s.connect(("1.1.1.1", 80))

def test_read_outside_copy_fails():
    with pytest.raises(OSError):
        Path("/etc/shadow").read_text()
'''
    (tests_dir / "test_sec.py").write_text(test_content, encoding="utf-8")

    reports = tmp_path / "reports"
    config = {
        "sandbox": {"mode": "auto"},
        "sable": {
            "allow_in_tests": [
                {"path": "tests/test_sec.py", "call": "socket", "reason": "sec integration test"},
                {"path": "tests/test_sec.py", "call": "socket.socket", "reason": "sec integration test"},
            ]
        },
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(config).run(project, project / "docs")
    assert report.verdict == Verdict.CLEAR.value
    assert report.sandbox == "bwrap"


@pytest.mark.skipif(not is_bwrap_available(), reason="bwrap is not available on this system")
def test_bwrap_integration_catches_unhandled_home_write(tmp_path: Path):
    project = tmp_path / "evil_proj"
    project.mkdir(parents=True, exist_ok=True)
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = project / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / "overview.md").write_text("# Evil test\n", encoding="utf-8")

    # Test attempts to write to $HOME without expecting failure
    test_content = '''import os
from pathlib import Path

def test_unauthorized_home_write():
    home = Path(os.environ.get("HOME", "/home/sandbox"))
    (home / "evil.txt").write_text("evil")
'''
    (tests_dir / "test_evil.py").write_text(test_content, encoding="utf-8")
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")

    reports = tmp_path / "reports"
    config = {
        "sandbox": {"mode": "auto"},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(config).run(project, docs_dir)
    assert report.verdict == Verdict.VETO.value
    assert report.sandbox == "bwrap"

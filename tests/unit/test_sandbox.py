from __future__ import annotations

import os
import types
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

    # Read-only binding for work_dir, read-write only for iso, pytest_cache, pytest_tmp, and /home/sandbox
    resolved_iso = str(iso.resolve())
    resolved_work = str(work.resolve())
    assert "--bind" in wrapped
    assert resolved_iso in wrapped

    ro_pairs = [
        (wrapped[i + 1], wrapped[i + 2])
        for i, tok in enumerate(wrapped[:-2])
        if tok == "--ro-bind"
    ]
    rw_pairs = [
        (wrapped[i + 1], wrapped[i + 2])
        for i, tok in enumerate(wrapped[:-2])
        if tok == "--bind"
    ]
    assert (resolved_work, resolved_work) in ro_pairs
    assert (resolved_work, resolved_work) not in rw_pairs
    assert ("/usr/bin", "/usr/bin") not in ro_pairs
    assert ("/bin", "/bin") not in [p for p in ro_pairs if Path("/bin").is_symlink()]
    assert (resolved_iso, resolved_iso) in rw_pairs
    assert (str(work.resolve() / "pytest_cache"), str(work.resolve() / "pytest_cache")) in rw_pairs
    assert (str(work.resolve() / "pytest_tmp"), str(work.resolve() / "pytest_tmp")) in rw_pairs
    assert (str(work.resolve() / "home"), "/home/sandbox") in rw_pairs
    assert (work.resolve() / "home").is_dir()

    # Bind /home/sandbox happens before --remount-ro /home
    bind_home_idx = next(
        i
        for i in range(len(wrapped) - 2)
        if wrapped[i] == "--bind" and wrapped[i + 2] == "/home/sandbox"
    )
    remount_ro_home_idx = next(
        i
        for i in range(len(wrapped) - 1)
        if wrapped[i] == "--remount-ro" and wrapped[i + 1] == "/home"
    )
    assert bind_home_idx < remount_ro_home_idx

    # Home set to sandbox home and chdir to iso
    assert "--setenv" in wrapped
    assert "/home/sandbox" in wrapped
    assert "--chdir" in wrapped
    assert resolved_iso in wrapped

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
    assert resolve_sandbox_mode({"sandbox": None}) == "auto"
    assert resolve_sandbox_mode({"sandbox": {}}) == "auto"
    assert resolve_sandbox_mode({"sandbox": {"mode": ""}}) == "auto"
    assert resolve_sandbox_mode({"sandbox": ""}) == "auto"
    assert resolve_sandbox_mode({"sandbox": "required"}) == "required"
    assert resolve_sandbox_mode({"sandbox": {"mode": "required"}}) == "required"
    assert resolve_sandbox_mode({"sandbox": {"mode": "off"}}) == "off"

    with pytest.raises(ValueError, match="Unknown sandbox mode"):
        resolve_sandbox_mode({"sandbox": {"mode": "UNKNOWN"}})
    with pytest.raises(ValueError, match="Unknown sandbox mode"):
        resolve_sandbox_mode({"sandbox": {"mode": "requried"}})
    with pytest.raises(ValueError, match="Unknown sandbox mode"):
        resolve_sandbox_mode({"sandbox": "invalid"})
    with pytest.raises(ValueError, match="Invalid sandbox mode"):
        resolve_sandbox_mode({"sandbox": {"mode": 123}})
    with pytest.raises(ValueError, match="Invalid sandbox configuration"):
        resolve_sandbox_mode({"sandbox": 123})

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


def test_sandbox_auto_fallback_reports_ran_unsandboxed(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    reports = tmp_path / "reports"
    config = {
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    with patch("core.sandbox.is_bwrap_available", return_value=False):
        report = Vesper(config).run(project, project / "docs")

    assert report.sandbox == "unavailable"
    md_text = (reports / f"{report.run_id}.md").read_text(encoding="utf-8")
    assert "sandbox: unavailable (ran unsandboxed)" in md_text
    assert any("sandbox: unavailable (ran unsandboxed)" in r.summary for r in report.role_results)


def test_extra_ro_binds_rejects_home_and_root_and_ancestors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()
    host_home = Path.home().resolve()

    # Python directly inside host $HOME
    with pytest.raises(ValueError, match="Refusing to bind unsafe read-only path"):
        wrap_command(["pytest"], isolated_root=iso, work_dir=work, python=host_home / "python3")

    # Python directly inside /
    with pytest.raises(ValueError, match="Refusing to bind unsafe read-only path"):
        wrap_command(["pytest"], isolated_root=iso, work_dir=work, python=Path("/python3"))

    # Python directly inside ancestor of $HOME (e.g. /home/python3)
    if host_home.parent != Path("/"):
        with pytest.raises(ValueError, match="Refusing to bind unsafe read-only path"):
            wrap_command(["pytest"], isolated_root=iso, work_dir=work, python=host_home.parent / "python3")

    # sys.prefix equal to host $HOME or /
    monkeypatch.setattr("sys.prefix", str(host_home))
    monkeypatch.setattr("sys.base_prefix", "/usr")
    with pytest.raises(ValueError, match="Refusing to bind unsafe read-only path"):
        wrap_command(["pytest"], isolated_root=iso, work_dir=work, python=Path("/usr/bin/python3"))

    monkeypatch.setattr("sys.prefix", "/")
    with pytest.raises(ValueError, match="Refusing to bind unsafe read-only path"):
        wrap_command(["pytest"], isolated_root=iso, work_dir=work, python=Path("/usr/bin/python3"))


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
        assert report.sandbox.startswith("unavailable")


@pytest.mark.skipif(not is_bwrap_available(), reason="bwrap is not available on this system")
def test_bwrap_integration_blocks_home_network_and_outside_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PYTEST_ADDOPTS", "--unknown-evil-flag-from-host")
    project = mini_project(tmp_path / "sec_proj")
    tests_dir = project / "tests"

    test_content = '''import os
import socket
from pathlib import Path
import _sable_guard
import pytest

def test_sandbox_home_writable_only_at_home_sandbox():
    home = Path(os.environ.get("HOME", ""))
    assert str(home) == "/home/sandbox"
    assert home.is_dir()
    probe = home / "writable_probe.txt"
    probe.write_text("ok", encoding="utf-8")
    assert probe.read_text(encoding="utf-8") == "ok"
    with pytest.raises(OSError):
        (home.parent / "should_never_exist.txt").write_text("pwn", encoding="utf-8")

def test_sable_guard_and_work_dir_are_read_only():
    guard_file = Path(_sable_guard.__file__).resolve()
    with pytest.raises(OSError):
        guard_file.write_text("# overwritten", encoding="utf-8")
    with pytest.raises(OSError):
        (guard_file.parent / "unauthorized.txt").write_text("pwn", encoding="utf-8")
    with pytest.raises(OSError):
        (guard_file.parent / "home" / "via_work_dir.txt").write_text("pwn", encoding="utf-8")

def test_host_pytest_addopts_is_not_visible():
    assert "PYTEST_ADDOPTS" not in os.environ

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

    # Test attempts to write to /home outside /home/sandbox without expecting failure
    test_content = '''from pathlib import Path

def test_unauthorized_home_write():
    Path("/home/evil.txt").write_text("evil", encoding="utf-8")
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


def test_bwrap_probe_usable_and_cached(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import MagicMock
    import core.sandbox as sb_mod

    sb_mod.clear_probe_cache()
    mock_run = MagicMock()
    mock_run.return_value = types.SimpleNamespace(
        args=["bwrap", "--unshare-all", "--ro-bind", "/", "/", "true"],
        returncode=0,
        stdout="",
        stderr="",
    )
    monkeypatch.setattr("core.sandbox.subprocess.run", mock_run)
    monkeypatch.setattr("core.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr("sys.platform", "linux")

    usable, reason = sb_mod._probe_bwrap("/usr/bin/bwrap")
    assert usable is True
    assert reason is None
    assert mock_run.call_count == 1

    # Second call uses cache
    usable2, reason2 = sb_mod._probe_bwrap("/usr/bin/bwrap")
    assert usable2 is True
    assert reason2 is None
    assert mock_run.call_count == 1


def test_bwrap_probe_failure_mode_auto_runs_unsandboxed_and_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import core.sandbox as sb_mod

    sb_mod.clear_probe_cache()
    real_run = sb_mod.subprocess.run

    def probe_failure_run(cmd, *args, **kwargs):
        if cmd and "true" in cmd and "--unshare-all" in cmd:
            return types.SimpleNamespace(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="bwrap: setting up uid map: Permission denied\n",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("core.sandbox.subprocess.run", probe_failure_run)
    monkeypatch.setattr("core.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr("sys.platform", "linux")

    assert is_bwrap_available() is False

    enabled, status, failure = get_sandbox_status({"sandbox": {"mode": "auto"}})
    assert enabled is False
    assert status == "unavailable (bwrap present but not usable: bwrap: setting up uid map: Permission denied)"
    assert failure is None

    project = mini_project(tmp_path / "proj")
    reports = tmp_path / "reports"
    config = {
        "sandbox": {"mode": "auto"},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(config).run(project, project / "docs")
    assert report.verdict == Verdict.CLEAR.value
    assert report.sandbox == "unavailable (bwrap present but not usable: bwrap: setting up uid map: Permission denied)"
    md_text = (reports / f"{report.run_id}.md").read_text(encoding="utf-8")
    assert "sandbox: unavailable (bwrap present but not usable: bwrap: setting up uid map: Permission denied)" in md_text
    assert any("sandbox: unavailable (bwrap present but not usable: bwrap: setting up uid map: Permission denied)" in r.summary for r in report.role_results)


def test_bwrap_probe_failure_mode_required_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import core.sandbox as sb_mod

    sb_mod.clear_probe_cache()
    real_run = sb_mod.subprocess.run

    def probe_failure_run(cmd, *args, **kwargs):
        if cmd and "true" in cmd and "--unshare-all" in cmd:
            return types.SimpleNamespace(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="bwrap: Creating new namespace failed: Permission denied\n",
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr("core.sandbox.subprocess.run", probe_failure_run)
    monkeypatch.setattr("core.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr("sys.platform", "linux")

    assert is_bwrap_available() is False

    enabled, status, failure = get_sandbox_status({"sandbox": {"mode": "required"}})
    assert enabled is False
    assert status == "unavailable (bwrap present but not usable: bwrap: Creating new namespace failed: Permission denied)"
    assert failure is not None
    assert "bwrap present but not usable: bwrap: Creating new namespace failed: Permission denied" in failure

    project = mini_project(tmp_path / "proj")
    reports = tmp_path / "reports"
    config = {
        "sandbox": {"mode": "required"},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(config).run(project, project / "docs")
    assert report.verdict == Verdict.VETO.value
    assert report.sandbox == "unavailable (bwrap present but not usable: bwrap: Creating new namespace failed: Permission denied)"
    assert any("sandbox required but bwrap unavailable" in r.summary for r in report.role_results)


def test_bwrap_probe_timeout_and_oserror_handling(monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import MagicMock
    import core.sandbox as sb_mod

    sb_mod.clear_probe_cache()
    mock_run = MagicMock(side_effect=sb_mod.subprocess.TimeoutExpired(cmd="bwrap", timeout=5))
    monkeypatch.setattr("core.sandbox.subprocess.run", mock_run)
    monkeypatch.setattr("core.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr("sys.platform", "linux")

    usable, reason = sb_mod._probe_bwrap("/usr/bin/bwrap")
    assert usable is False
    assert reason == "probe timed out"

    sb_mod.clear_probe_cache()
    mock_run = MagicMock(side_effect=OSError("failed to exec bwrap"))
    monkeypatch.setattr("core.sandbox.subprocess.run", mock_run)
    usable, reason = sb_mod._probe_bwrap("/usr/bin/bwrap")
    assert usable is False
    assert reason == "failed to exec bwrap"


def test_venv_python_symlink_binds_venv_root(tmp_path: Path):
    """A venv's bin/python links to the base interpreter; the venv root must still be bound."""
    iso = tmp_path / "iso"
    work = tmp_path / "work"
    venv = tmp_path / "other" / "venv"
    iso.mkdir()
    work.mkdir()
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (venv / "bin" / "python").symlink_to("/usr/bin/python3")

    wrapped = wrap_command(
        ["python", "-m", "pytest"],
        isolated_root=iso,
        work_dir=work,
        python=venv / "bin" / "python",
        allow_network=False,
        bwrap_path="/usr/bin/bwrap",
    )
    joined = " ".join(wrapped)
    assert f"--ro-bind {venv.resolve()} {venv.resolve()}" in joined


def test_probe_bwrap_missing_binary_returns_false_tuple(monkeypatch: pytest.MonkeyPatch):
    """Missing bwrap must yield (False, None), not None or a usable verdict."""
    import core.sandbox as sb_mod

    sb_mod.clear_probe_cache()
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setattr("core.sandbox.shutil.which", lambda _: None)
    try:
        assert sb_mod._probe_bwrap() == (False, None)

        enabled, status, failure = get_sandbox_status({"sandbox": {"mode": "auto"}})
        assert enabled is False
        assert status == "unavailable"
        assert failure is None

        enabled, status, failure = get_sandbox_status({"sandbox": {"mode": "required"}})
        assert enabled is False
        assert status == "unavailable"
        assert failure is not None
    finally:
        sb_mod.clear_probe_cache()

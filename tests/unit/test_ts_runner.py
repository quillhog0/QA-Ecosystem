"""Unit tests for core/ts_runner.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.ts_runner import (
    _find_node_binary,
    _lockfile_hash,
    _parse_mocha_json,
    _parse_vitest_json,
    prepare_node_modules,
    run_ts_tests,
)


def test_calculate_lockfile_hash(tmp_path: Path):
    assert _lockfile_hash(tmp_path) is None

    lock = tmp_path / "package-lock.json"
    lock.write_text('{"name": "test"}', encoding="utf-8")
    h1 = _lockfile_hash(tmp_path)
    assert len(h1) == 64

    lock.write_text('{"name": "test2"}', encoding="utf-8")
    h2 = _lockfile_hash(tmp_path)
    assert h1 != h2


def test_find_node_binary():
    bin_path = _find_node_binary()
    assert bin_path is not None
    assert isinstance(bin_path, str)


def test_parse_vitest_json(tmp_path: Path):
    data = {
        "numTotalTests": 2,
        "numPassedTests": 1,
        "numFailedTests": 1,
        "numPendingTests": 0,
        "testResults": [
            {
                "name": str(tmp_path / "tests" / "sample.test.ts"),
                "assertionResults": [
                    {
                        "title": "fails",
                        "status": "failed",
                        "ancestorTitles": ["Suite"],
                        "failureMessages": ["Expected 1 to be 2"],
                    }
                ],
            }
        ],
    }
    col, passed, failed, skipped, failures = _parse_vitest_json(data, tmp_path)
    assert col == 2
    assert passed == 1
    assert failed == 1
    assert skipped == 0
    assert len(failures) == 1
    assert failures[0]["node"] == "tests/sample.test.ts::Suite > fails"


def test_parse_mocha_json(tmp_path: Path):
    data = {
        "stats": {
            "tests": 3,
            "passes": 2,
            "failures": 1,
            "pending": 0,
        },
        "failures": [
            {
                "fullTitle": "Suite fails",
                "file": str(tmp_path / "tests" / "mocha.test.js"),
                "err": {"message": "mocha error", "stack": "error stack"},
            }
        ],
    }
    col, passed, failed, skipped, failures = _parse_mocha_json(data, tmp_path)
    assert col == 3
    assert passed == 2
    assert failed == 1
    assert skipped == 0
    assert len(failures) == 1
    assert failures[0]["node"] == "tests/mocha.test.js::Suite fails"


def test_prepare_node_modules_cached(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("QA_NODE_CACHE_DIR", str(cache_root))

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")

    # Mock subprocess.run for npm ci
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    def fake_install(*args, **kwargs):
        (proj / "node_modules").mkdir(parents=True, exist_ok=True)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("shutil.which", return_value="/usr/bin/npm"), patch("subprocess.run", side_effect=fake_install) as mock_subp:
        ok, status = prepare_node_modules(proj, work_dir)
        assert ok is True
        assert "installed" in status
        assert (proj / "node_modules").is_dir()

        # Second run should restore from cache without subprocess
        proj2 = tmp_path / "proj2"
        proj2.mkdir()
        (proj2 / "package.json").write_text('{"name": "test"}', encoding="utf-8")
        (proj2 / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")

        mock_subp.reset_mock()
        ok2, status2 = prepare_node_modules(proj2, work_dir)
        assert ok2 is True
        assert status2 == "restored from cache"
        assert mock_subp.call_count == 0


def test_prepare_node_modules_package_manager_not_found(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    with patch("shutil.which", return_value=None):
        ok, status = prepare_node_modules(proj, tmp_path / "work")
        assert ok is False
        assert "not found in PATH" in status


def test_prepare_node_modules_timeout_and_network_flag(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")

    def raise_timeout(*args, **kwargs):
        assert kwargs["env"]["HOME"]
        assert kwargs["capture_output"] is True
        import subprocess
        raise subprocess.TimeoutExpired(cmd=["npm"], timeout=5)

    with patch("shutil.which", return_value="/usr/bin/npm"), patch("subprocess.run", side_effect=raise_timeout):
        ok, status = prepare_node_modules(proj, tmp_path / "work")
        assert ok is False
        assert status == "package install timed out"


def test_run_ts_tests_execution_and_timeout(tmp_path: Path):
    import subprocess
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"scripts": {"test": "node --test"}}', encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    # Success run with exit_code == 0
    def fake_run_success(*args, **kwargs):
        if args and args[0] and "bwrap" in str(args[0][0]) and "true" in args[0]:
            return MagicMock(returncode=0, stdout="", stderr="")
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("check") is False
        assert kwargs.get("env", {}).get("NO_NETWORK") == "1"  # allow_network=False by default
        return MagicMock(returncode=0, stdout="pass", stderr=None)

    with patch("subprocess.run", side_effect=fake_run_success), patch("time.monotonic", side_effect=[100.0, 102.5]):
        res = run_ts_tests(
            isolated_root=proj,
            work_dir=work,
        )
        assert res.exit_code == 0
        assert res.passed == 1
        assert res.failed == 0
        assert isinstance(res.stderr, str)
        assert res.stderr == ""
        assert res.duration_seconds == 2.5

    # Run again with same work_dir to verify Path(HOME).mkdir exist_ok=True
    with patch("subprocess.run", side_effect=fake_run_success):
        res2 = run_ts_tests(isolated_root=proj, work_dir=work)
        assert res2.exit_code == 0

    # Failure run with exit_code == 1
    def fake_run_fail(*args, **kwargs):
        if "env" in kwargs and "/tmp/node:/opt/node:" in kwargs["env"].get("PATH", ""):
            return MagicMock(returncode=1, stdout="", stderr="some test error")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run_fail):
        res = run_ts_tests(
            isolated_root=proj,
            work_dir=work,
        )
        assert res.exit_code == 1
        assert res.failed == 1
        assert len(res.failures) == 1
        assert res.failures[0]["traceback"] == "some test error"

    # TimeoutExpired run with None stdout/stderr
    def fake_run_timeout(*args, **kwargs):
        if "env" in kwargs and "/tmp/node:/opt/node:" in kwargs["env"].get("PATH", ""):
            exc = subprocess.TimeoutExpired(cmd=["node"], timeout=10)
            exc.stdout = None
            exc.stderr = None
            raise exc
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_run_timeout):
        res = run_ts_tests(
            isolated_root=proj,
            work_dir=work,
        )
        assert res.exit_code == 124
        assert res.stdout == ""
        assert res.stderr == "\nTIMEOUT"


def test_fix_node_modules_symlinks(tmp_path: Path):
    from core.ts_runner import _fix_node_modules_symlinks

    # Non-existent directory
    assert _fix_node_modules_symlinks(tmp_path / "nonexistent") is None

    # Directory without .bin
    nm = tmp_path / "node_modules"
    nm.mkdir()
    assert _fix_node_modules_symlinks(nm) is None

    # Directory with .bin and absolute symlink
    bin_dir = nm / ".bin"
    bin_dir.mkdir()
    pkg_bin = nm / "pkg" / "bin"
    pkg_bin.mkdir(parents=True)
    cli = pkg_bin / "cli.js"
    cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")

    link = bin_dir / "cli"
    import os
    # Create absolute symlink pointing to cli
    link.symlink_to(cli.resolve())
    assert link.is_symlink()
    assert Path(os.readlink(link)).is_absolute()

    _fix_node_modules_symlinks(nm)

    target_after = os.readlink(link)
    assert not Path(target_after).is_absolute()
    assert target_after == "../pkg/bin/cli.js"
    assert (link.stat().st_mode & 0o111) != 0


def test_prepare_node_modules_existing_and_restore(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("QA_NODE_CACHE_DIR", str(cache_root))

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")

    # Node modules with .bin exists
    nm = proj / "node_modules"
    (nm / ".bin").mkdir(parents=True)
    (nm / "foo.txt").write_text("hello", encoding="utf-8")
    # Also add a symlink to verify symlinks=True
    (nm / "link.txt").symlink_to("foo.txt")

    work_dir = tmp_path / "work"
    work_dir.mkdir()

    ok, status = prepare_node_modules(proj, work_dir)
    assert ok is True
    assert status == "existing node_modules"
    # Should have populated cache with symlinks preserved
    h = _lockfile_hash(proj)
    cache_entry = cache_root / h
    assert (cache_entry / "valid.ok").is_file()
    assert (cache_entry / "node_modules" / "foo.txt").is_file()
    assert (cache_entry / "node_modules" / "link.txt").is_symlink()

    # If cache_marker exists but cache_tree is deleted, calling prepare_node_modules should re-populate cache_tree
    shutil.rmtree(cache_entry / "node_modules")
    assert not (cache_entry / "node_modules").exists()
    ok_repop, _ = prepare_node_modules(proj, work_dir)
    assert ok_repop is True
    assert (cache_entry / "node_modules" / "foo.txt").is_file()

    # Calling again when cache already exists
    ok2, status2 = prepare_node_modules(proj, work_dir)
    assert ok2 is True
    assert status2 == "existing node_modules"

    # Now test restore when local node_modules is missing or missing .bin
    proj2 = tmp_path / "proj2"
    proj2.mkdir()
    (proj2 / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj2 / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    (proj2 / "node_modules").mkdir()  # no .bin

    ok3, status3 = prepare_node_modules(proj2, work_dir)
    assert ok3 is True
    assert status3 == "restored from cache"
    assert (proj2 / "node_modules" / "foo.txt").read_text() == "hello"
    assert (proj2 / "node_modules" / "link.txt").is_symlink()

    # Verify that if cache_marker is missing, it does NOT restore from cache
    (cache_entry / "valid.ok").unlink()
    proj3 = tmp_path / "proj3"
    proj3.mkdir()
    (proj3 / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj3 / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    with patch("shutil.which", return_value="/bin/false"):
        ok4, status4 = prepare_node_modules(proj3, work_dir)
        assert status4 != "restored from cache"


def test_prepare_node_modules_install_fail_and_error(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("QA_NODE_CACHE_DIR", str(cache_root))

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    work_dir = tmp_path / "work"
    work_dir.mkdir()

    with patch("shutil.which", return_value="/usr/bin/npm"), patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="npm ERR! 404", stdout="")):
        ok, status = prepare_node_modules(proj, work_dir)
        assert ok is False
        assert "package install failed: npm ERR! 404" in status

    with patch("shutil.which", return_value="/usr/bin/npm"), patch("subprocess.run", side_effect=OSError("network down")):
        ok, status = prepare_node_modules(proj, work_dir)
        assert ok is False
        assert "package install error: network down" in status

    # Install success populating cache with network enabled
    def fake_install(*args, **kwargs):
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("check") is False
        env = kwargs.get("env", {})
        assert "HTTP_PROXY" not in env  # allow_network=True
        assert "NO_NETWORK" not in env
        nm = proj / "node_modules"
        nm.mkdir(parents=True, exist_ok=True)
        (nm / "installed.txt").write_text("ok", encoding="utf-8")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("shutil.which", return_value="/usr/bin/npm"), patch("subprocess.run", side_effect=fake_install):
        ok, status = prepare_node_modules(proj, work_dir)
        assert ok is True
        assert status == "installed"
        h = _lockfile_hash(proj)
        assert (cache_root / h / "valid.ok").is_file()
        assert (cache_root / h / "node_modules" / "installed.txt").is_file()


def test_find_node_binary_fallbacks():
    with patch("shutil.which", return_value=None), patch("pathlib.Path.is_file", return_value=True):
        assert _find_node_binary() == "/tmp/node/node"

    with patch("shutil.which", return_value=None), patch("pathlib.Path.is_file", return_value=False):
        assert _find_node_binary() == "node"


def test_parse_vitest_json_fallback_counts(tmp_path: Path):
    # None values
    data_none = {
        "numTotalTests": None,
        "numPassedTests": None,
        "numFailedTests": None,
        "numPendingTests": None,
        "testResults": [],
    }
    c, p, f, s, _ = _parse_vitest_json(data_none, tmp_path)
    assert c == 0 and p == 0 and f == 0 and s == 0

    # collected=0, passed=2, failed=1, pending=1 -> fallback to sum
    data = {
        "numTotalTests": 0,
        "numPassedTests": 2,
        "numFailedTests": 1,
        "numPendingTests": 1,
        "testResults": [],
    }
    col, passed, failed, skipped, failures = _parse_vitest_json(data, tmp_path)
    assert col == 4
    assert passed == 2
    assert failed == 1
    assert skipped == 1

    # collected=0, passed=0, failed=0, pending=5 -> passed+failed is 0, so collected remains 0!
    data_zero = {
        "numTotalTests": 0,
        "numPassedTests": 0,
        "numFailedTests": 0,
        "numPendingTests": 5,
        "testResults": [],
    }
    c0, p0, f0, s0, _ = _parse_vitest_json(data_zero, tmp_path)
    assert c0 == 0

    # collected=5, passed=2, failed=1 -> collected must remain 5, not be overwritten!
    data_explicit = {
        "numTotalTests": 5,
        "numPassedTests": 2,
        "numFailedTests": 1,
        "numPendingTests": 0,
        "testResults": [],
    }
    ce, _, _, _, _ = _parse_vitest_json(data_explicit, tmp_path)
    assert ce == 5

    # collected=0, passed=1, failed=0 -> under or, collected becomes 1
    data_pass_only = {
        "numTotalTests": 0,
        "numPassedTests": 1,
        "numFailedTests": 0,
        "numPendingTests": 0,
        "testResults": [],
    }
    cp, _, _, _, _ = _parse_vitest_json(data_pass_only, tmp_path)
    assert cp == 1


def test_parse_mocha_json_fallbacks(tmp_path: Path):
    data = {
        "stats": {"tests": 1, "failures": 1},
        "failures": [
            {
                "fullTitle": "anonymous failure",
                "err": {},
            }
        ],
    }
    col, passed, failed, skipped, failures = _parse_mocha_json(data, tmp_path)
    assert col == 1
    assert passed == 0
    assert failed == 1
    assert len(failures) == 1
    assert failures[0]["file"] == "tests"
    assert failures[0]["message"] == "failed"
    assert failures[0]["traceback"] == ""


def test_run_ts_tests_sandbox_failure(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    with patch("core.sandbox.get_sandbox_status", return_value=(False, "unavailable", "bwrap not found")):
        res = run_ts_tests(proj, work_dir=work)
        assert res.exit_code == 1
        assert res.stderr == "bwrap not found"
        assert res.sandbox_status == "unavailable"


def test_run_ts_tests_runners_and_targets(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    (proj / "node_modules" / ".bin").mkdir(parents=True)

    # Vitest runner without bin file
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "vitest"}), \
         patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="vitest not installed")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work, test_targets=["tests/a.test.ts"])
        assert res.exit_code == 1
        assert "vitest not installed" in mock_sub.call_args[0][0][2]

    # Vitest runner with bin file and test_targets
    vitest_bin = proj / "node_modules" / ".bin" / "vitest"
    vitest_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "vitest"}), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work, test_targets=["tests/a.test.ts"])
        assert res.exit_code == 0
        cmd = mock_sub.call_args[0][0]
        assert "tests/a.test.ts" in cmd

    # Jest runner without bin file
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "jest"}), \
         patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="jest not installed")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work)
        assert res.exit_code == 1
        assert "jest not installed" in mock_sub.call_args[0][0][2]

    # Jest runner with bin file and test_targets
    jest_bin = proj / "node_modules" / ".bin" / "jest"
    jest_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "jest"}), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work, test_targets=["tests/b.test.ts"])
        assert res.exit_code == 0
        cmd = mock_sub.call_args[0][0]
        assert "tests/b.test.ts" in cmd

    # Mocha runner without bin file
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "mocha"}), \
         patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="mocha not installed")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work)
        assert res.exit_code == 1
        assert "mocha not installed" in mock_sub.call_args[0][0][2]

    # Mocha runner with bin file and stdout json
    mocha_bin = proj / "node_modules" / ".bin" / "mocha"
    mocha_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    mocha_json = '{"stats": {"tests": 2, "passes": 2, "failures": 0, "pending": 0}, "failures": []}'
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "mocha"}), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=mocha_json, stderr="")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work)
        assert res.exit_code == 0
        assert res.collected == 2
        assert res.passed == 2

    # node_test runner with stdout looking like json should NOT parse mocha json
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "node_test"}), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout='{"stats": {"tests": 42, "passes": 42}}', stderr="")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work)
        assert res.exit_code == 0
        cmd = mock_sub.call_args[0][0]
        assert "--test" in cmd
        assert res.collected == 1  # exit_code fallback, not mocha stats

    # Unknown runner falls back to npm test (with shutil.which returning None)
    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "custom"}), \
         patch("shutil.which", return_value=None), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as mock_sub:
        res = run_ts_tests(proj, work_dir=work)
        assert res.exit_code == 0
        cmd = mock_sub.call_args[0][0]
        assert cmd[0] == "npm"
        assert cmd[1] == "test"


def test_run_ts_tests_work_dir_nested_and_reuse(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    work = tmp_path / "nested" / "work"

    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        res1 = run_ts_tests(proj, work_dir=work)
        assert res1.exit_code == 0
        res2 = run_ts_tests(proj, work_dir=work)
        assert res2.exit_code == 0


def test_prepare_node_modules_restore_rmtree_ignore_errors(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("QA_NODE_CACHE_DIR", str(cache_root))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    (proj / "node_modules").mkdir()
    h = _lockfile_hash(proj)
    cache_entry = cache_root / h
    (cache_entry / "node_modules").mkdir(parents=True)
    (cache_entry / "valid.ok").write_text(h, encoding="utf-8")

    with patch("shutil.rmtree") as mock_rm, patch("shutil.copytree"):
        ok, status = prepare_node_modules(proj, tmp_path / "work")
        assert ok is True
        assert status == "restored from cache"
        assert mock_rm.call_args[1].get("ignore_errors") is True


def test_run_ts_tests_with_type_errors_and_diff(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    work = tmp_path / "work"
    work.mkdir()

    type_error = {
        "file": "src/index.ts",
        "line": 42,
        "message": "Type mismatch",
        "evidence": "let x: number = 'hello';",
    }

    with patch("core.ts_runner.typecheck_project", return_value=[type_error]), \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        res = run_ts_tests(proj, work_dir=work)
        assert res.exit_code == 1
        assert res.failed == 1
        assert res.passed == 1
        assert res.collected == 2
        assert any(f.get("is_type_error") is True for f in res.failures)
        tf = next(f for f in res.failures if f.get("is_type_error"))
        assert tf["is_type_error"] is True
        assert tf["file"] == "src/index.ts"
        assert tf["line"] == 42

    # In diff mode with no TS files changed, typecheck_project is skipped
    with patch("core.ts_runner.typecheck_project") as mock_tc, \
         patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
        res = run_ts_tests(proj, work_dir=work, config={"_diff_changed": ["README.md"]})
        assert res.exit_code == 0
        mock_tc.assert_not_called()


def test_parse_vitest_none_failure_messages(tmp_path: Path):
    data = {
        "numTotalTests": 1,
        "numFailedTests": 1,
        "testResults": [
            {
                "name": str(tmp_path / "a.test.ts"),
                "assertionResults": [
                    {"title": "fail", "status": "failed", "failureMessages": None}
                ],
            }
        ],
    }
    _, _, failed, _, failures = _parse_vitest_json(data, tmp_path)
    assert failed == 1
    assert failures[0]["message"] == "test failed"


def test_parse_mocha_no_err_and_none_message(tmp_path: Path):
    data = {
        "stats": {"tests": 2, "failures": 2},
        "failures": [
            {"fullTitle": "f1"},
            {"fullTitle": "f2", "err": {"message": None, "stack": None}},
        ],
    }
    _, _, failed, _, failures = _parse_mocha_json(data, tmp_path)
    assert failed == 2
    assert failures[0]["message"] == "failed"
    assert failures[1]["message"] == "failed"


def test_prepare_node_modules_no_lockfile_install(tmp_path: Path):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    def fake_install(*args, **kwargs):
        nm = proj / "node_modules"
        nm.mkdir(parents=True, exist_ok=True)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("core.ts_runner._lockfile_hash", return_value=None), \
         patch("shutil.which", return_value="/bin/npm"), \
         patch("subprocess.run", side_effect=fake_install):
        ok, status = prepare_node_modules(proj, work)
        assert ok is True
        assert status == "installed"


def test_prepare_node_modules_install_cache_symlinks_and_parents(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "deep" / "cache"
    monkeypatch.setenv("QA_NODE_CACHE_DIR", str(cache_root))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    def fake_install(*args, **kwargs):
        nm = proj / "node_modules"
        nm.mkdir(parents=True, exist_ok=True)
        (nm / "target.txt").write_text("target", encoding="utf-8")
        (nm / "link.txt").symlink_to("target.txt")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("shutil.which", return_value="/bin/npm"), patch("subprocess.run", side_effect=fake_install):
        ok, status = prepare_node_modules(proj, work)
        assert ok is True
        h = _lockfile_hash(proj)
        assert (cache_root / h / "node_modules" / "link.txt").is_symlink()


def test_cache_rmtree_and_copytree_flags(tmp_path: Path, monkeypatch):
    cache_root = tmp_path / "cache"
    monkeypatch.setenv("QA_NODE_CACHE_DIR", str(cache_root))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text('{"name": "test"}', encoding="utf-8")
    (proj / "package-lock.json").write_text('{"lockfileVersion": 3}', encoding="utf-8")
    nm = proj / "node_modules"
    (nm / ".bin").mkdir(parents=True)
    h = _lockfile_hash(proj)
    cache_entry = cache_root / h
    (cache_entry / "node_modules").mkdir(parents=True)

    # 1. Existing node_modules cache populate
    with patch("shutil.rmtree") as mock_rm, patch("shutil.copytree") as mock_cp:
        ok, status = prepare_node_modules(proj, tmp_path / "work")
        assert ok is True
        assert mock_rm.call_args[1].get("ignore_errors") is True
        assert mock_cp.call_args[1].get("symlinks") is True

    # 2. Install node_modules cache populate
    (cache_entry / "valid.ok").unlink(missing_ok=True)
    cache_tree = cache_entry / "node_modules"
    cache_tree.mkdir(parents=True, exist_ok=True)
    (nm / ".bin").rmdir()
    nm.rmdir()
    def fake_install(*args, **kwargs):
        (proj / "node_modules").mkdir(parents=True, exist_ok=True)
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("shutil.which", return_value="/bin/npm"), \
         patch("subprocess.run", side_effect=fake_install), \
         patch("shutil.rmtree") as mock_rm, \
         patch("shutil.copytree") as mock_cp:
        ok, status = prepare_node_modules(proj, tmp_path / "work")
        assert ok is True
        assert status == "installed"
        assert mock_rm.call_args[1].get("ignore_errors") is True
        assert mock_cp.call_args[1].get("symlinks") is True


def test_run_ts_tests_flaky_rerun(tmp_path: Path):
    """A TS test that fails on the first run and passes on rerun is marked FLAKY."""
    proj = tmp_path / "proj"
    proj.mkdir()
    work = tmp_path / "work"
    work.mkdir()
    vitest_bin = proj / "node_modules" / ".bin" / "vitest"
    vitest_bin.parent.mkdir(parents=True)
    vitest_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    call_idx = {"n": 0}

    def fake_vitest_flaky(*args, **kwargs):
        cmd = args[0]
        if cmd and "bwrap" in str(cmd[0]) and "true" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        call_idx["n"] += 1
        if call_idx["n"] == 1:
            results_json = work / "pytest_tmp" / "ts-results.json"
            results_json.write_text(
                '{"numTotalTests": 1, "numPassedTests": 0, "numFailedTests": 1, "numPendingTests": 0, '
                '"testResults": [{"name": "tests/flaky.test.ts", "assertionResults": '
                '[{"title": "flaky spec", "ancestorTitles": [], "status": "failed", "failureMessages": ["transient error"]}]}]}',
                encoding="utf-8",
            )
            return MagicMock(returncode=1, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("core.ts_runner.detect_ts_runner", return_value={"runner": "vitest"}), \
         patch("subprocess.run", side_effect=fake_vitest_flaky):
        res = run_ts_tests(proj, work_dir=work, config={"sandbox": {"mode": "off"}})
        assert res.flaky == 1
        assert res.failed == 0
        assert res.passed == 1
        assert res.exit_code == 0
        assert res.status == "FLAKY"
        assert len(res.flaky_tests) == 1
        assert res.flaky_tests[0]["node"] == "tests/flaky.test.ts::flaky spec"






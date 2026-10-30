from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents.riley import parse_failure_root_cause
from core import deps
from core.deps import prepare_venv, _read_pyproject_deps
from tests.support import mini_project


def test_prepare_venv_no_manifest(tmp_path: Path):
    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    venv_path, status = prepare_venv(root, tmp_path / "work", timeout=1)
    assert venv_path is None
    assert status == "no dependency manifest"
    assert not (tmp_path / "work" / "venv").exists()


def test_read_pyproject_deps(tmp_path: Path):
    pyproject_toml = tmp_path / "pyproject.toml"
    pyproject_toml.write_text(
        """[project]
name = "my-package"
version = "0.1.0"
dependencies = ["foo", "bar"]

[project.optional-dependencies]
dev = ["baz"]
test = ["qux"]
""",
        encoding="utf-8",
    )
    deps = _read_pyproject_deps(pyproject_toml)
    assert sorted(deps) == sorted(["foo", "bar", "baz", "qux"])


def test_parse_failure_root_cause_missing_dependency():
    stdout = ""
    stderr = "ModuleNotFoundError: No module named 'solders'\nTraceback (most recent call last):\n  File \"<stdin>\", line 1, in <module>\nImportError: No module named 'solders'"
    root_cause = parse_failure_root_cause(stdout, stderr)
    assert root_cause == "missing dependency: solders (not installable or not declared in requirements)"


def test_prepare_venv_rejects_unsafe_requirement(tmp_path: Path):
    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    (root / "requirements.txt").write_text("-e ../x\npytest\n", encoding="utf-8")
    work_dir = tmp_path / "work"
    python_executable, status = prepare_venv(root, work_dir, timeout=30)
    assert python_executable is None
    assert "unsafe requirement: -e ../x" in status
    assert not (work_dir / "venv").exists()


def test_prepare_venv_rejects_unsafe_pyproject_dependency(tmp_path: Path):
    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    (root / "pyproject.toml").write_text(
        """[project]
name = "my-package"
version = "0.1.0"
dependencies = ["evil @ git+https://example.com/e.git"]
""",
        encoding="utf-8",
    )
    work_dir = tmp_path / "work"
    python_executable, status = prepare_venv(root, work_dir, timeout=30)
    assert python_executable is None
    assert "unsafe requirement: evil @ git+https://example.com/e.git" in status
    assert not (work_dir / "venv").exists()


@pytest.mark.skipif(os.environ.get("QA_NETWORK_TESTS") != "1", reason="network test")
def test_prepare_venv_installs_deps_and_pytest(tmp_path: Path):
    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    (root / "requirements.txt").write_text("requests", encoding="utf-8")
    work_dir = tmp_path / "work"
    python_executable, status = prepare_venv(root, work_dir, timeout=300)
    assert python_executable is not None
    assert status == "ok"
    assert (tmp_path / "work" / "venv").exists()
    # Verify pytest is installed
    result = subprocess.run(
        [str(python_executable), "-m", "pip", "show", "pytest"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Name: pytest" in result.stdout
    # Verify requests is installed
    result = subprocess.run(
        [str(python_executable), "-m", "pip", "show", "requests"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Name: requests" in result.stdout


def test_prepare_venv_reports_creation_failure_as_tuple(tmp_path: Path, monkeypatch):
    """deps.py:101 - a failed venv creation must return the (None, message)
    tuple, so callers that unpack it keep working."""
    Err = subprocess.CalledProcessError

    def fake_run(cmd, **kwargs):
        raise Err(returncode=1, cmd=cmd, stderr="ensurepip failed\n")

    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    monkeypatch.setattr("subprocess.run", fake_run)
    result = prepare_venv(root, tmp_path / "work", timeout=5)
    assert result == (None, "venv creation failed: ensurepip failed")


def _fake_working_venv_run(counter):
    """Simulate `python -m venv` + `pip install` without touching the network.

    Records how many subprocess calls happen and materialises a minimal venv
    tree (bin/python + pyvenv.cfg) so reuse can copy it back out of the cache.
    """

    def fake_run(cmd, *args, **kwargs):
        counter["n"] += 1
        if len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] == "venv":
            vdir = Path(cmd[3])
            (vdir / "bin").mkdir(parents=True, exist_ok=True)
            (vdir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            (vdir / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        return SimpleNamespace(stdout="", stderr="")

    return fake_run


def test_prepare_venv_reuses_cached_venv_without_subprocess(tmp_path, monkeypatch):
    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    monkeypatch.setenv("QA_VENV_CACHE_DIR", str(tmp_path / "cache"))
    counter = {"n": 0}
    monkeypatch.setattr(deps.subprocess, "run", _fake_working_venv_run(counter))

    work1 = tmp_path / "work1"
    work1.mkdir()
    python1, status1 = deps.prepare_venv(root, work1, timeout=30)
    assert status1 == "ok"
    assert python1 is not None
    assert counter["n"] == 2  # venv creation + pip install
    entries = list((tmp_path / "cache").iterdir())
    assert len(entries) == 1
    assert (entries[0] / "valid.ok").is_file()

    # A second run with a fresh work dir must hit the cache and spawn nothing.
    work2 = tmp_path / "work2"
    work2.mkdir()
    counter["n"] = 0
    python2, status2 = deps.prepare_venv(root, work2, timeout=30)
    assert status2 == "ok"
    assert counter["n"] == 0
    assert deps.venv_python(work2).exists()


def test_venv_cache_key_changes_with_requirements_content(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_VENV_CACHE_DIR", str(tmp_path / "cache"))
    req = tmp_path / "requirements.txt"
    req.write_text("pytest\n", encoding="utf-8")
    key_a = deps._venv_cache_key([req], [])
    req.write_text("pytest\nrequests\n", encoding="utf-8")
    key_b = deps._venv_cache_key([req], [])
    assert key_a and key_b
    assert key_a != key_b


def test_partial_cache_without_marker_is_not_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_VENV_CACHE_DIR", str(tmp_path / "cache"))
    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    key = deps._venv_cache_key([root / "requirements.txt"], [])
    entry, cache_venv, marker = deps._cache_paths(key)
    entry.mkdir(parents=True, exist_ok=True)
    cache_venv.mkdir(parents=True, exist_ok=True)  # partial install, no marker

    work = tmp_path / "work"
    work.mkdir()
    assert deps._reuse_cached_venv(key, work) is False
    assert not (work / "venv").exists()


def test_venv_cache_key_none_when_requirement_unreadable(tmp_path):
    """deps.py:92 - an unreadable manifest disables reuse by returning None,
    not a truthy value that would seed a bogus cache entry."""
    key = deps._venv_cache_key([tmp_path], [])  # a directory cannot be read_bytes
    assert key is None


def test_prepare_venv_reports_pip_failure_as_tuple(tmp_path, monkeypatch):
    """deps.py:228/241 - a non-zero pip install must raise (check=True) and be
    returned as the (None, message) tuple, never reused as a success."""
    monkeypatch.setenv("QA_VENV_CACHE_DIR", str(tmp_path / "cache"))
    root = mini_project(tmp_path / "proj", with_tests=False, docs="")
    (root / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()

    err_text = "ERROR: could not find a version that satisfies the requirement\n"
    Err = subprocess.CalledProcessError

    def fake_run(cmd, *, check=False, **kwargs):
        if len(cmd) >= 3 and cmd[1] == "-m" and cmd[2] == "venv":
            vdir = Path(cmd[3])
            (vdir / "bin").mkdir(parents=True, exist_ok=True)
            (vdir / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
            return SimpleNamespace(stdout="", stderr="")
        # pip install exits non-zero
        if check:
            raise Err(1, cmd, output=err_text, stderr=err_text)
        return SimpleNamespace(stdout="", stderr=err_text)

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    result = deps.prepare_venv(root, work, timeout=5)
    assert isinstance(result, tuple) and len(result) == 2
    python_executable, status = result
    assert python_executable is None
    assert status.startswith("pip install failed")
    assert "could not find a version" in status

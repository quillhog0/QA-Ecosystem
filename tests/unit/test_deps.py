from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agents.riley import parse_failure_root_cause
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

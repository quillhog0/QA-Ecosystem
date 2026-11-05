"""Regression tests for v0.4.3: the pinned TypeScript artifact must ship with the action."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

import core.toolchain as toolchain
from core.packets import FinalReport
from core.report import render_summary_markdown

REPO = Path(__file__).resolve().parents[2]
VENDOR_REL = "agents/vendor/typescript.js"


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, check=True).stdout


def test_vendor_typescript_is_not_tracked_in_git():
    if not (REPO / ".git").exists() or shutil.which("git") is None:
        pytest.skip("not a git checkout")
    tracked = _git("ls-files", "--", VENDOR_REL).split()
    assert tracked == [], "vendored typescript.js must be removed from git tracking in v0.5.0"
    assert not (REPO / VENDOR_REL).exists()


def test_vendor_license_is_apache():
    text = (REPO / "agents/vendor/LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in text and "TypeScript" in text


def test_ensure_ts_tooling_fresh_home_uses_only_vendor(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("QA_TOOLING_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    result = toolchain.ensure_ts_tooling()
    assert result == home / ".cache" / "qa-ecosystem" / "tooling"
    cached = result / "typescript.js"
    assert hashlib.sha256(cached.read_bytes()).hexdigest() == toolchain.PINNED_TS_SHA256
    assert (result / "typescript.sha256").read_text().strip() == toolchain.PINNED_TS_SHA256


def _ts_project(root: Path) -> Path:
    root.mkdir()
    (root / "package.json").write_text('{"name":"x","version":"1.0.0"}', encoding="utf-8")
    (root / "index.ts").write_text("export const a = 1;\n", encoding="utf-8")
    return root


def test_summary_languages_fallback_uses_detected_not_python(tmp_path, monkeypatch):
    monkeypatch.setenv("QA_LANGUAGES", "auto")
    proj = _ts_project(tmp_path / "p")
    report = FinalReport(run_id="r", verdict="BLOCKED_ENV", isolated_root=str(proj), finished_at="2026-11-05T10:00:00Z")
    md = render_summary_markdown(report)
    assert "- **Languages:** ts" in md
    assert "python" not in md.split("**Languages:**")[1].splitlines()[0]


def test_summary_languages_fallback_uses_selection_without_tree(monkeypatch):
    monkeypatch.setenv("QA_LANGUAGES", "ts")
    md = render_summary_markdown({"verdict": "BLOCKED_ENV", "isolated_root": "/nonexistent-qa-path"})
    assert "- **Languages:** ts" in md
    monkeypatch.setenv("QA_LANGUAGES", "auto")
    md = render_summary_markdown({"verdict": "BLOCKED_ENV"})
    assert "- **Languages:** unknown" in md


def test_bootstrap_ts_tooling_candidates_and_network_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from unittest.mock import MagicMock

    target = tmp_path / "target"
    target.mkdir()
    empty_cache = tmp_path / "empty_cache"
    empty_cache.mkdir()
    monkeypatch.setattr(toolchain, "_DEFAULT_CACHE_HOME", empty_cache)

    # Candidate filtering: when target == default_cached.parent, default_cached is excluded
    default_tooling = empty_cache / "qa-ecosystem" / "tooling"
    default_tooling.mkdir(parents=True)
    cands = toolchain._candidate_ts_sources(default_tooling, tmp_path / "vendor.js")
    assert (default_tooling / "typescript.js") not in cands

    # Offline with no candidates -> returns False
    assert toolchain.bootstrap_ts_tooling(target, allow_network=False) is False

    # Network enabled, npm install fails -> returns False
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: MagicMock(returncode=1))
    assert toolchain.bootstrap_ts_tooling(target, allow_network=True) is False

    # detect_project_cli_commands on non-existent directory -> empty set
    assert toolchain.detect_project_cli_commands(tmp_path / "does_not_exist") == set()
    pkg_dir = tmp_path / "pkg_empty_bin"
    pkg_dir.mkdir()
    (pkg_dir / "package.json").write_text('{"name": "my-cli", "bin": "   "}', encoding="utf-8")
    assert toolchain.detect_project_cli_commands(pkg_dir) == set()


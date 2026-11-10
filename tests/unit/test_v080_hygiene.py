from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

import main
from core.cache import AnalysisCache, compute_content_hash
from core.exceptions import ExceptionEntry
from core.findings import (
    _apply_precision_downgrade,
    build_feedback_export,
    write_feedback_export,
    write_findings,
)
from core.packets import FinalReport
from core.sandbox import (
    detect_container_runtime,
    get_sandbox_status,
    wrap_container_command,
)
from core.toolchain import select_node_version


def test_compute_content_hash_and_analysis_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h1 = compute_content_hash("hello world", namespace="lexi")
    h2 = compute_content_hash(b"hello world", namespace="lexi")
    h3 = compute_content_hash("hello world", namespace="aria")
    h4 = compute_content_hash("different", namespace="lexi")
    assert len(h1) == 64
    assert h1 == h2
    assert h1 != h3
    assert h1 != h4

    cache = AnalysisCache(cache_dir=tmp_path / "cache")
    assert cache.get(h1) is None

    out_path = cache.put(h1, {"symbols": ["add"]})
    assert out_path.is_file()
    assert out_path.parent.name == h1[:2]
    assert cache.get(h1) == {"symbols": ["add"]}
    assert cache.get(h3) is None

    # Corrupted JSON returns None
    out_path.write_text("{invalid json", encoding="utf-8")
    assert cache.get(h1) is None

    # Non-dict JSON returns None
    out_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert cache.get(h1) is None

    # Environment variable fallback
    monkeypatch.setenv("QA_ANALYSIS_CACHE_DIR", str(tmp_path / "env_cache"))
    env_cache = AnalysisCache()
    assert env_cache.root == tmp_path / "env_cache"


def test_select_node_version(tmp_path: Path) -> None:
    # Default fallback when no config exists
    assert select_node_version(tmp_path) == "22"

    # .node-version file with exact version
    (tmp_path / ".node-version").write_text("20.11.0\n", encoding="utf-8")
    assert select_node_version(tmp_path) == "20"

    # .nvmrc takes precedence over .node-version
    (tmp_path / ".nvmrc").write_text("v24.1.0\n", encoding="utf-8")
    assert select_node_version(tmp_path) == "24"

    # package.json engines.node takes precedence over .nvmrc
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "pkg", "engines": {"node": "^20.0.0"}}),
        encoding="utf-8",
    )
    assert select_node_version(tmp_path) == "20"

    # Unsupported major falls back to default 22
    (tmp_path / ".nvmrc").unlink()
    (tmp_path / ".node-version").unlink()
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "pkg", "engines": {"node": "16.x"}}),
        encoding="utf-8",
    )
    assert select_node_version(tmp_path) == "22"

    # Corrupted package.json falls back cleanly
    (tmp_path / "package.json").write_text("{bad", encoding="utf-8")
    assert select_node_version(tmp_path) == "22"


def test_container_sandbox_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. detect_container_runtime
    monkeypatch.setattr("core.sandbox.shutil.which", lambda b: "/usr/bin/podman" if b == "podman" else None)
    assert detect_container_runtime() == "podman"

    monkeypatch.setattr("core.sandbox.shutil.which", lambda b: "/usr/bin/docker" if b == "docker" else None)
    assert detect_container_runtime() == "docker"

    monkeypatch.setattr("core.sandbox.shutil.which", lambda b: None)
    assert detect_container_runtime() is None

    # 2. wrap_container_command
    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()
    cmd_offline = wrap_container_command(
        ["pytest", "-q"],
        isolated_root=iso,
        work_dir=work,
        allow_network=False,
        runtime="podman",
        image="python:3.12-slim",
    )
    assert cmd_offline[:4] == ["podman", "run", "--rm", "--read-only"]
    assert "--network" in cmd_offline
    assert "none" in cmd_offline
    assert cmd_offline[-2:] == ["pytest", "-q"]

    cmd_online = wrap_container_command(
        ["pytest", "-q"],
        isolated_root=iso,
        work_dir=work,
        allow_network=True,
        runtime="docker",
    )
    assert cmd_online[0] == "docker"
    assert "--network" not in cmd_online

    # 3. get_sandbox_status with fallback="container" when bwrap is unavailable
    monkeypatch.setattr("core.sandbox._probe_bwrap", lambda: (False, "bwrap not installed"))
    monkeypatch.setattr("core.sandbox.detect_container_runtime", lambda: "podman")
    use_bwrap, status, reason = get_sandbox_status(
        {"sandbox": {"mode": "auto", "fallback": "container"}}
    )
    assert use_bwrap is True
    assert status == "container:podman"
    assert reason is None

    monkeypatch.setattr("core.sandbox.detect_container_runtime", lambda: None)
    use_bwrap2, status2, reason2 = get_sandbox_status(
        {"sandbox": {"mode": "required", "fallback": "container"}}
    )
    assert use_bwrap2 is False
    assert "unavailable" in status2
    assert reason2 is not None and "bwrap not installed" in reason2


def test_precision_downgrade_and_feedback_export(tmp_path: Path) -> None:
    # Boundary test: 1 false-positive and 3 accepted-risk -> 1*4 == 4 (not > 4), so NOT downgraded
    findings_boundary = [
        {
            "id": "F001",
            "agent": "lexi",
            "type": "drift",
            "file": "docs/a.md",
            "line": 1,
            "priority": "P1",
            "class": "heuristic",
            "confidence": "high",
            "evidence": "heuristic",
            "verdict_effect": "VETO",
            "excepted": "fp",
            "exception_kind": "false-positive",
            "fingerprint": "fp1",
            "customer_impact": "c",
            "proposed_red_test": "t",
        },
        *[
            {
                "id": f"F00{i}",
                "agent": "lexi",
                "type": "drift",
                "file": f"docs/{i}.md",
                "line": i,
                "priority": "P3",
                "class": "heuristic",
                "confidence": "high",
                "evidence": "heuristic",
                "verdict_effect": "INFO",
                "excepted": "ar",
                "exception_kind": "accepted-risk",
                "fingerprint": f"ar{i}",
                "customer_impact": "c",
                "proposed_red_test": "t",
            }
            for i in range(2, 5)
        ],
        {
            "id": "F005",
            "agent": "lexi",
            "type": "drift",
            "file": "docs/unexcepted.md",
            "line": 10,
            "priority": "P1",
            "class": "heuristic",
            "confidence": "high",
            "evidence": "heuristic",
            "verdict_effect": "VETO",
            "fingerprint": "u1",
            "customer_impact": "c",
            "proposed_red_test": "t",
        },
    ]
    assert _apply_precision_downgrade(findings_boundary, {}) == []
    assert _apply_precision_downgrade(findings_boundary, {"lexi.drift": 1}) == []
    assert findings_boundary[-1]["priority"] == "P1"
    assert findings_boundary[-1]["confidence"] == "high"

    # Now with 1 false-positive and 2 accepted-risk -> 1*4 = 4 > 3 -> downgraded!
    findings_over = [f.copy() for f in findings_boundary if f["id"] != "F004"]
    downgraded = _apply_precision_downgrade(findings_over, {"lexi.drift": 1})
    assert downgraded == ["lexi.drift"]
    assert findings_over[-1]["confidence"] == "low"
    assert findings_over[-1]["priority"] == "P2"
    assert findings_over[-1]["verdict_effect"] == "WARN"

    # Test through write_findings + feedback_export
    report = FinalReport(
        run_id="run-080",
        verdict="VETO",
        project_root="/secret/project/path",
        docs_root="/secret/docs/path",
        isolated_root="/tmp/iso",
        dry_run=True,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        drift={
            "findings": [
                {
                    "kind": "missing_code",
                    "path": "secret_symbol_one",
                    "docs_file": "README.md",
                    "docs_line": 5,
                    "severity": "high",
                    "message": "missing secret_symbol_one",
                },
                {
                    "kind": "missing_code",
                    "path": "secret_symbol_two",
                    "docs_file": "README.md",
                    "docs_line": 9,
                    "severity": "high",
                    "message": "missing secret_symbol_two",
                },
            ],
        },
    )
    entries = [
        ExceptionEntry(
            match="lexi:drift:README.md:secret_symbol_one",
            reason="Known FP",
            expires=dt.date(2026, 12, 31),
            added_by="alice",
            kind="false-positive",
            rule="lexi.drift",
        )
    ]
    rep_base = tmp_path / "reports"
    write_findings(
        report,
        rep_base,
        exceptions=entries,
        today=dt.date(2026, 6, 1),
        feedback_export=True,
    )
    export_file = rep_base / "run-080" / "feedback-export.json"
    latest_export_file = rep_base / "latest" / "feedback-export.json"
    assert export_file.is_file()
    assert latest_export_file.is_file()
    export_data = json.loads(export_file.read_text(encoding="utf-8"))
    assert export_data["schema_version"] == "1"
    assert export_data["false_positive_counts"] == {"lexi.drift": 1}
    assert export_data["downgraded_rules"] == ["lexi.drift"]
    assert len(export_data["entries"]) == 2
    labels = {r["label"] for r in export_data["entries"]}
    assert labels == {"false-positive", "unlabelled"}
    # Verify no code symbols or file paths leaked into feedback-export.json
    raw_export = export_file.read_text(encoding="utf-8")
    assert "secret_symbol" not in raw_export
    assert "README.md" not in raw_export
    assert "/secret/" not in raw_export

    direct_path = write_feedback_export({"run_id": "r1", "findings": []}, tmp_path / "direct_fb")
    assert direct_path == tmp_path / "direct_fb" / "r1" / "feedback-export.json"


def test_cli_feedback_export_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = tmp_path / "proj"
    docs = proj / "docs"
    tests_dir = proj / "tests"
    docs.mkdir(parents=True)
    tests_dir.mkdir(parents=True)
    (proj / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (docs / "overview.md").write_text("# Calc\n\n`add` adds two integers.\n", encoding="utf-8")
    (tests_dir / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    rep_dir = tmp_path / "rep"
    rc = main.main(
        [
            "--project",
            str(proj),
            "--docs",
            str(docs),
            "--reports-dir",
            str(rep_dir),
            "--feedback-export",
            "true",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "feedback_export=" in out
    assert (rep_dir / "latest" / "feedback-export.json").is_file()

    rep_dir2 = tmp_path / "rep2"
    rc2 = main.main(
        [
            "--project",
            str(proj),
            "--docs",
            str(docs),
            "--reports-dir",
            str(rep_dir2),
            "--feedback-export",
            "false",
        ]
    )
    assert rc2 == 0
    out2 = capsys.readouterr().out
    # The key is always printed (empty value) so Action wrappers can grep it
    # under `set -e`. The export file itself is not written when the flag is off.
    export_lines = [ln for ln in out2.splitlines() if ln.startswith("feedback_export=")]
    assert export_lines and export_lines[0] == "feedback_export="
    assert not (rep_dir2 / "latest" / "feedback-export.json").exists()

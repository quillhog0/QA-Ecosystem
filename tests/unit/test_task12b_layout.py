"""Task 12b - per-run findings layout, ``latest/`` mirror, and the code anchor.

The IDE contract is two-fold: every run keeps its own immutable directory
under ``<findings-dir>/<run_id>/``, and the same pair is mirrored into
``<findings-dir>/latest/`` so a workspace can watch one stable path. A drift
finding's ``file``/``line`` stays the docs anchor; the new ``code_file``/
``code_line`` fields carry the nearest real code candidate the tool can name
(a ``missing_tests`` source file), and are ``null`` for kinds where no code
candidate is knowable (``missing_code``, ``domain_term``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import findings
from core.packets import DriftFinding


def _report(run_id: str = "run-abc", **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": run_id,
        "verdict": "VETO",
        "finished_at": "2026-11-03T00:00:00+00:00",
        "drift": {},
        "plan": {},
        "policy": {},
        "execution": {},
        "post_audit": {},
        "role_results": [],
    }
    base.update(overrides)
    return base


# --- layout -----------------------------------------------------------------

def test_write_findings_writes_per_run_dir_and_latest(tmp_path: Path) -> None:
    json_path, md_path = findings.write_findings(_report("run-abc"), tmp_path / "out")
    assert json_path == tmp_path / "out" / "run-abc" / "findings.json"
    assert md_path == tmp_path / "out" / "run-abc" / "findings.md"
    assert json_path.is_file() and md_path.is_file()
    latest_json = tmp_path / "out" / "latest" / "findings.json"
    latest_md = tmp_path / "out" / "latest" / "findings.md"
    assert latest_json.is_file() and latest_md.is_file()
    assert latest_json.read_bytes() == json_path.read_bytes()
    assert latest_md.read_bytes() == md_path.read_bytes()


def test_write_findings_latest_always_tracks_the_last_run(tmp_path: Path) -> None:
    findings.write_findings(_report("run-1"), tmp_path / "out")
    findings.write_findings(_report("run-2"), tmp_path / "out")
    first = tmp_path / "out" / "run-1" / "findings.json"
    second = tmp_path / "out" / "run-2" / "findings.json"
    latest = tmp_path / "out" / "latest" / "findings.json"
    assert first.is_file() and second.is_file(), "both per-run dirs must survive"
    assert latest.read_bytes() == second.read_bytes()
    assert json.loads(latest.read_text(encoding="utf-8"))["run_id"] == "run-2"


def test_write_findings_schema_validates_at_both_paths(tmp_path: Path) -> None:
    findings.write_findings(_report("run-abc"), tmp_path / "out")
    schema = findings.load_schema()
    for candidate in (
        tmp_path / "out" / "run-abc" / "findings.json",
        tmp_path / "out" / "latest" / "findings.json",
    ):
        loaded = json.loads(candidate.read_text(encoding="utf-8"))
        assert findings.validate_schema(loaded, schema) == []


# --- code anchor ------------------------------------------------------------

def test_missing_tests_drift_carries_a_code_anchor(tmp_path: Path) -> None:
    # A ``missing_tests`` finding has a real code file behind it, so the
    # projection must expose it as ``code_file`` while keeping ``file`` for
    # the docs side (which is null here because this kind has no doc anchor).
    build = _report(drift={
        "findings": [
            {
                "kind": "missing_tests",
                "path": "src/api.py",
                "message": "public names exist in code but tests never reference them",
                "severity": "medium",
                "docs_file": None,
                "docs_line": None,
                "code_file": "src/api.py",
                "code_line": None,
            },
        ],
    })
    items = findings.build_findings(build)
    assert len(items) == 1
    entry = items[0]
    assert entry["type"] == "weak_test"
    assert entry["file"] == "src/api.py"
    assert entry["code_file"] == "src/api.py"
    assert entry["code_line"] is None


def test_missing_code_drift_keeps_docs_anchor_and_null_code(tmp_path: Path) -> None:
    # The very definition of ``missing_code`` is "no code candidate": the
    # projection must keep ``code_file`` null while still carrying the docs
    # anchor the operator has to edit.
    build = _report(drift={
        "findings": [
            {
                "kind": "missing_code",
                "path": "ghostFn",
                "message": "docs mention `ghostFn` but no matching module or public name exists in code",
                "severity": "high",
                "docs_file": "docs/api.md",
                "docs_line": 42,
                "code_file": None,
                "code_line": None,
            },
        ],
    })
    entry = findings.build_findings(build)[0]
    assert entry["type"] == "drift"
    assert entry["file"] == "docs/api.md"
    assert entry["line"] == 42
    assert entry["code_file"] is None
    assert entry["code_line"] is None
    # Whole document must still validate with the new nullable fields present.
    assert findings.validate_schema(findings.build_document(build), findings.load_schema()) == []


def test_drift_finding_packet_round_trips_code_fields() -> None:
    finding = DriftFinding(
        kind="missing_tests",
        path="src/api.py",
        message="no test references the public surface",
        severity="medium",
        code_file="src/api.py",
    )
    payload = finding.to_dict()
    assert payload["code_file"] == "src/api.py"
    assert payload["code_line"] is None
    # Backwards-compatible with Task 12 packets that never set the new fields:
    plain = DriftFinding(kind="missing_code", path="x", message="m", severity="low")
    assert plain.to_dict()["code_file"] is None
    assert plain.to_dict()["code_line"] is None


def test_findings_md_line_still_shows_docs_anchor_only(tmp_path: Path) -> None:
    # ``findings.md`` stays short: the code anchor is a JSON-only enrichment so
    # the markdown line width and count remain IDE-friendly.
    build = _report(drift={
        "findings": [
            {
                "kind": "missing_tests",
                "path": "src/api.py",
                "message": "public names exist in code but tests never reference them",
                "severity": "medium",
                "code_file": "src/api.py",
            },
        ],
    })
    document = findings.build_document(build)
    text = findings.render_markdown(document)
    assert "src/api.py" in text
    # Only one location marker per line: no ":code" or duplicated anchor path.
    line = next(l for l in text.splitlines() if l.startswith("- [F-"))
    assert line.count("`") == 2


def test_all_findings_carry_code_fields_so_schema_stays_uniform(tmp_path: Path) -> None:
    # Every emitted finding must have the two new keys, even null, so an IDE
    # can consume them without per-type branches.
    build = _report(
        drift={
            "findings": [
                {"kind": "missing_code", "path": "ghostFn",
                 "message": "docs mention `ghostFn`", "severity": "high",
                 "docs_file": "docs/api.md", "docs_line": 3},
            ],
        },
        execution={
            "failures": [
                {"node": "tests/test_x.py::test_y", "message": "boom",
                 "traceback": "tests/test_x.py:5: in test_y"},
            ],
        },
    )
    items = findings.build_findings(build)
    assert items, "fixture must produce at least one finding"
    for entry in items:
        assert "code_file" in entry and "code_line" in entry
    schema = findings.load_schema()
    assert findings.validate_schema(findings.build_document(build), schema) == []

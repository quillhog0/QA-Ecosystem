"""Task 13 - operator exceptions and the fast diff mode.

Part A: an unexpired exception downgrades a finding to INFO with a reason, an
expired one only warns, an invalid one is a startup config error, and a P0 is
excepted only with allow_p0 and surfaces at the top of the report.

Part B: a computed diff scope restricts what each role inspects, and a change
that only touches docs still triggers Lexi drift.
"""

from __future__ import annotations

import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from core import findings
from core.diffscope import DiffScopeError, compute_diff_scope
from core.exceptions import (
    ExceptionEntry,
    ExceptionsConfigError,
    load_exceptions,
    parse_entries,
)
from tests.support import mini_project

TODAY = date(2026, 11, 3)


def _drift_report(**_: Any) -> dict[str, Any]:
    return {
        "run_id": "run-1",
        "verdict": "VETO",
        "finished_at": "2026-11-03T00:00:00+00:00",
        "drift": {
            "findings": [
                {"kind": "missing_code", "path": "missing_fn", "severity": "high",
                 "message": "docs promise missing_fn", "docs_file": "api.md", "docs_line": 3},
            ]
        },
        "plan": {}, "policy": {}, "execution": {}, "post_audit": {}, "role_results": [],
    }


# --- fingerprint ------------------------------------------------------------
def test_every_finding_carries_a_stable_fingerprint() -> None:
    items = findings.build_findings(_drift_report())
    assert items
    fp = items[0]["fingerprint"]
    assert fp == "lexi:drift:api.md:missing_fn"
    assert ":" in fp


def test_fingerprint_survives_a_line_shift() -> None:
    a = _drift_report()
    b = _drift_report()
    b["drift"]["findings"][0]["docs_line"] = 99
    fa = findings.build_findings(a)[0]["fingerprint"]
    fb = findings.build_findings(b)[0]["fingerprint"]
    assert fa == fb  # no line number in the identity


# --- apply: unexpired downgrade ---------------------------------------------
def test_unexpired_exception_downgrades_to_info_with_reason() -> None:
    entry = ExceptionEntry(
        match="lexi:drift:api.md:missing_fn",
        reason="tracked in OPS-1",
        expires=TODAY + timedelta(days=30),
        added_by="qa",
    )
    doc = findings.build_document(_drift_report(), exceptions=[entry], today=TODAY)
    f = doc["findings"][0]
    assert (f["priority"], f["verdict_effect"]) == ("P3", "INFO")
    assert f["excepted"] == "tracked in OPS-1"
    assert doc["counts"]["P1"] == 0 and doc["counts"]["P3"] == 1
    assert "excepted: tracked in OPS-1" in findings.render_markdown(doc)


def test_expired_exception_has_no_effect_and_warns() -> None:
    entry = ExceptionEntry(
        match="lexi:drift:api.md:missing_fn",
        reason="old",
        expires=TODAY - timedelta(days=1),
    )
    doc = findings.build_document(_drift_report(), exceptions=[entry], today=TODAY)
    f = doc["findings"][0]
    assert (f["priority"], f["verdict_effect"]) == ("P1", "VETO")  # unchanged
    assert "excepted" not in f
    assert doc["warnings"] == ["exception expired: lexi:drift:api.md:missing_fn"]
    assert "exception expired" in findings.render_markdown(doc)


# --- P0 gate ----------------------------------------------------------------
def _secret_report() -> dict[str, Any]:
    return {
        "run_id": "run-2", "verdict": "VETO", "finished_at": "x",
        "policy": {"preflight": {"checks": {"no_secret_env": False},
                                 "blocked_reasons": ["found AWS_SECRET in config.py"],
                                 "warnings": []}},
        "drift": {}, "plan": {}, "execution": {}, "post_audit": {}, "role_results": [],
    }


def _p0_finding_fingerprint() -> str:
    f = findings.build_findings(_secret_report())[0]
    return f["fingerprint"]


def test_p0_is_not_excepted_without_allow_p0() -> None:
    fp = _p0_finding_fingerprint()
    entry = ExceptionEntry(match=fp, reason="trust me", expires=TODAY + timedelta(days=10))
    doc = findings.build_document(_secret_report(), exceptions=[entry], today=TODAY)
    assert doc["findings"][0]["priority"] == "P0"
    assert doc["excepted_p0"] == []


def test_p0_excepted_with_allow_p0_and_listed_at_top() -> None:
    fp = _p0_finding_fingerprint()
    entry = ExceptionEntry(
        match=fp, reason="flag off in tenant", expires=TODAY + timedelta(days=10),
        added_by="sec", allow_p0=True,
    )
    doc = findings.build_document(_secret_report(), exceptions=[entry], today=TODAY)
    assert doc["findings"][0]["priority"] == "P3"
    assert doc["excepted_p0"] and doc["excepted_p0"][0]["fingerprint"] == fp
    md = findings.render_markdown(doc)
    assert "Excepted P0 findings" in md
    # the section is near the top: before the priority count table
    assert md.index("Excepted P0 findings") < md.index("| priority | count |")


# --- parsing / validation ---------------------------------------------------
def test_missing_reason_is_a_config_error() -> None:
    with pytest.raises(ExceptionsConfigError):
        parse_entries(
            [{"match": "a:b:c:d", "expires": (TODAY + timedelta(days=5)).isoformat()}],
            today=TODAY,
        )


def test_missing_expiry_is_a_config_error() -> None:
    with pytest.raises(ExceptionsConfigError):
        parse_entries([{"match": "a:b:c:d", "reason": "why"}], today=TODAY)


def test_expiry_beyond_max_days_is_a_config_error() -> None:
    far = (TODAY + timedelta(days=200)).isoformat()
    with pytest.raises(ExceptionsConfigError):
        parse_entries([{"match": "a:b:c:d", "reason": "why", "expires": far}], today=TODAY)


def test_load_missing_file_is_empty_not_an_error(tmp_path: Path) -> None:
    assert load_exceptions({}, "no-such-project", today=TODAY) == []


def test_load_valid_file_from_config_dir(tmp_path: Path) -> None:
    d = tmp_path / "exceptions"
    d.mkdir()
    (d / "widget.yaml").write_text(
        "exceptions:\n"
        "  - match: 'lexi:drift:api.md:x'\n"
        "    reason: known\n"
        f"    expires: {(TODAY + timedelta(days=10)).isoformat()}\n"
        "    added_by: qa\n",
        encoding="utf-8",
    )
    config = {"exceptions": {"dir": str(d)}}
    entries = load_exceptions(config, "widget", today=TODAY)
    assert len(entries) == 1 and entries[0].reason == "known"


# --- diff scope -------------------------------------------------------------
def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-C", str(root), *args],
        check=True, capture_output=True, text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


def test_compute_scope_bad_ref_raises(tmp_path: Path) -> None:
    root = mini_project(tmp_path / "proj")
    _init_repo(root)
    with pytest.raises(DiffScopeError):
        compute_diff_scope(root, "no-such-ref", root / "docs")


def test_compute_scope_non_repo_raises(tmp_path: Path) -> None:
    root = mini_project(tmp_path / "proj")
    with pytest.raises(DiffScopeError):
        compute_diff_scope(root, "HEAD", root / "docs")


def test_compute_scope_too_many_files_falls_back(tmp_path: Path) -> None:
    root = mini_project(tmp_path / "proj")
    _init_repo(root)
    for i in range(30):
        (root / f"src_{i}.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "many")
    with pytest.raises(DiffScopeError):
        compute_diff_scope(root, "HEAD~1", root / "docs", max_files=5)


def test_docs_only_change_still_triggers_lexi_drift(tmp_path: Path) -> None:
    """The Task 13 required test: touching only docs keeps the drift in scope."""
    root = mini_project(
        tmp_path / "proj",
        with_divide=True,
        docs={"overview.md": "# Calc\n\nHelpers `add` and `divide` exist.\n"},
    )
    _init_repo(root)
    # A docs-only change that promises a symbol the code does not have.
    (root / "docs" / "api.md").write_text(
        "# API\n\nThe helper `missing_fn` returns a number.\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "docs-only")

    scope = compute_diff_scope(root, "HEAD~1", root / "docs")
    assert scope.changed == ["docs/api.md"]           # only the doc changed
    assert "api.md" in scope.lexi_docs_allowlist or "docs/api.md" in scope.lexi_docs_allowlist

    from agents import lexi

    report = lexi.inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
        docs_allowlist=set(scope.lexi_docs_allowlist),
        changed_code=set(scope.changed_code),
    )
    assert report.verdict == "VETO"
    assert any(f.path == "missing_fn" for f in report.findings)


def test_diff_scope_filters_out_unchanged_docs(tmp_path: Path) -> None:
    """A drift on a doc outside the changed set is dropped, so it cannot veto."""
    root = mini_project(
        tmp_path / "proj",
        with_divide=True,
        docs={"old.md": "# Old\n\nThe helper `gone_fn` returns a number.\n"},
    )
    _init_repo(root)
    # Change an unrelated code file only; old.md is untouched.
    (root / "notes.txt").write_text("touch\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "code-only")

    scope = compute_diff_scope(root, "HEAD~1", root / "docs")
    assert "docs/old.md" not in scope.lexi_docs_allowlist

    from agents import lexi

    report = lexi.inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
        docs_allowlist=set(scope.lexi_docs_allowlist),
        changed_code=set(scope.changed_code),
    )
    assert not any(f.path == "gone_fn" for f in report.findings)


def test_nova_targets_resolve_to_related_tests(tmp_path: Path) -> None:
    root = mini_project(tmp_path / "proj")
    (root / "payments.py").write_text("def refund(x):\n    return x\n", encoding="utf-8")
    (root / "tests" / "test_payments.py").write_text(
        "import payments\n\ndef test_refund():\n    assert payments.refund(1) == 1\n",
        encoding="utf-8",
    )
    _init_repo(root)
    (root / "payments.py").write_text("def refund(x):\n    return x + 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "code")

    scope = compute_diff_scope(root, "HEAD~1", root / "docs")
    assert "payments.py" in scope.changed_code
    assert "tests/test_payments.py" in scope.nova_test_targets


def test_rescope_keeps_code_anchored_missing_tests(tmp_path: Path) -> None:
    """A missing_tests finding anchored in the changed code stays in scope.

    This exercises the ``changed_code`` branch of the rescope filter, where a
    code-anchored finding is kept only when its file is in the changed set.
    """
    from agents.lexi import _rescope_lexi
    from core.packets import DriftFinding, DriftReport, Verdict

    report = DriftReport(
        verdict=Verdict.CLEAR.value,
        findings=[
            DriftFinding(
                kind="missing_tests", path="solo.py", message="solo.py has no tests",
                severity="medium", code_file="solo.py",
            )
        ],
    )
    out = _rescope_lexi(report, docs_allowlist=None, changed_code={"solo.py"})
    assert len(out.findings) == 1
    assert out.findings[0].code_file == "solo.py"

    # The same finding is dropped when its file is outside the changed set.
    dropped = _rescope_lexi(
        DriftReport(
            verdict=Verdict.CLEAR.value,
            findings=[DriftFinding(kind="missing_tests", path="solo.py",
                                   message="m", severity="medium", code_file="solo.py")],
        ),
        docs_allowlist=None,
        changed_code={"other.py"},
    )
    assert dropped.findings == []


# --- validator edge that the exceptions/fingerprint schema relies on ---------
def test_unknown_schema_type_is_accepted_not_rejected() -> None:
    # The bundled validator supports a fixed subset; an unrecognised ``type``
    # keyword is ignored, never a hard rejection. A mutant that flips the
    # fallback to "reject everything unknown" must fail this.
    assert findings.validate_schema("anything", {"type": "some_custom_kind"}) == []


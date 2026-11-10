"""Task 11 - Lexi: a historical record document must never veto.

Incident logs, post-mortems, changelogs, release notes and decision (ADR) logs
*report on* code that existed (symbols, file:line citations); they do not promise
the project's current public surface. A name that only ever appears in such a
document is therefore capped below the veto line - even under ``--strict``, which
is about the operator being loud, not about trusting a changelog to be an API.

Each case pairs a historical-document false positive with the *same term* written
in an ordinary doc as a real API promise, which must stay high - so the fix is
proven to key on the document's place in the tree, not to blanket-suppress.
"""

from __future__ import annotations

from pathlib import Path

from agents.lexi import _heuristic_drift, _is_historical_doc


def _run(docs_root: Path, project_root: Path, **cfg) -> dict[str, str]:
    config = {"lexi": {"veto_on_missing_code": True}} if cfg.get("strict") else {}
    report = _heuristic_drift(docs_root=docs_root, project_root=project_root, config=config)
    return {f.path: f.severity for f in report.findings}


# --- an incident log under docs/incidents/ is a historical record ------------


def test_incidents_directory_caps_drift_at_low(tmp_path: Path):
    docs = tmp_path / "docs"
    incidents = docs / "incidents"
    incidents.mkdir(parents=True)
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (incidents / "INCIDENTS.md").write_text(
        "# Incident log\n\n"
        "Root cause was a bug in `calculate_platform_fee_lamports` "
        "(`core_verify.py:60-70`).\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert "calculate_platform_fee_lamports" not in sev


def test_same_term_in_ordinary_doc_stays_high(tmp_path: Path):
    # The control: the identical code-shaped name, promised in a normal doc, is
    # real drift and must keep vetoing.
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "api.md").write_text(
        "# API\n\nThe module exposes `calculate_platform_fee_lamports` for callers.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("calculate_platform_fee_lamports") == "high"


def test_historical_cap_holds_even_in_strict_mode(tmp_path: Path):
    docs = tmp_path / "docs"
    incidents = docs / "incidents"
    incidents.mkdir(parents=True)
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (incidents / "INCIDENTS.md").write_text(
        "# Incident log\n\nA defect in `settle_pending_payouts` caused the outage.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path, strict=True)
    assert "settle_pending_payouts" not in sev


def test_term_in_incident_and_ordinary_doc_stays_high(tmp_path: Path):
    # Conservative: only when *every* mention is historical does it demote. A term
    # that also makes a plain API promise somewhere is still real drift.
    docs = tmp_path / "docs"
    incidents = docs / "incidents"
    incidents.mkdir(parents=True)
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (incidents / "INCIDENTS.md").write_text(
        "# Incident log\n\n`payment_gateway` was misconfigured.\n", encoding="utf-8"
    )
    (docs / "api.md").write_text(
        "# API\n\nThe `payment_gateway` handler processes charges.\n", encoding="utf-8"
    )
    sev = _run(docs, tmp_path)
    assert sev.get("payment_gateway") == "high"


# --- changelog / postmortem / history file names are historical too -----------


def test_historical_and_cited_plan_mention_demotes(tmp_path: Path):
    # The Quillhog shape: a removed dead-code name is recorded in the incident
    # log (historical) AND restated in a task/plan document (an ordinary,
    # non-historical path) on a line that cites a ``file.ext:line`` location and
    # instructs deleting it. Neither mention promises the current API - one is a
    # historical record, the other narrates existing code - so the term must be
    # capped, not vetoed.
    docs = tmp_path / "docs"
    incidents = docs / "incidents"
    incidents.mkdir(parents=True)
    plan = docs / "notes"
    plan.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (incidents / "INCIDENTS.md").write_text(
        "# Incident log\n\nDead code `calculate_platform_fee_lamports` was removed.\n",
        encoding="utf-8",
    )
    (plan / "TASKLIST.md").write_text(
        "# Tasks\n\n- [ ] Delete `calculate_platform_fee_lamports` (`core_verify.py:60-70`).\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("calculate_platform_fee_lamports") == "low"


def test_historical_plus_real_promise_still_high_is_the_boundary(tmp_path: Path):
    # Guard: the demotion must stay keyed on *every* mention being a non-promise.
    # Here the ordinary-doc mention is a present-tense API promise with no source
    # citation, so it is not prose and the term still vetoes.
    docs = tmp_path / "docs"
    incidents = docs / "incidents"
    incidents.mkdir(parents=True)
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (incidents / "INCIDENTS.md").write_text(
        "# Incident log\n\n`orchestrate_dispatch` used to hang.\n", encoding="utf-8"
    )
    (docs / "api.md").write_text(
        "# API\n\nThe service exposes `orchestrate_dispatch` to clients.\n", encoding="utf-8"
    )
    sev = _run(docs, tmp_path)
    assert sev.get("orchestrate_dispatch") == "high"


def test_changelog_stem_is_historical(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "CHANGELOG.md").write_text(
        "# Changelog\n\n## 1.2.0\n- Added `rotate_api_keys` command.\n", encoding="utf-8"
    )
    sev = _run(docs, tmp_path)
    assert "rotate_api_keys" not in sev


def test_postmortem_named_file_is_historical(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "postmortem-2026-05.md").write_text(
        "# Postmortem\n\n`reconcile_ledger_balance` drifted overnight.\n", encoding="utf-8"
    )
    sev = _run(docs, tmp_path)
    assert "reconcile_ledger_balance" not in sev


# --- the path classifier itself is shape-based, not name-based ----------------


def test_is_historical_doc_detects_directories_and_stems(tmp_path: Path):
    docs = tmp_path / "docs"
    (docs / "incidents").mkdir(parents=True)
    (docs / "adr").mkdir(parents=True)
    assert _is_historical_doc(docs / "incidents" / "INCIDENTS.md", docs) is True
    assert _is_historical_doc(docs / "CHANGELOG.md", docs) is True
    assert _is_historical_doc(docs / "adr" / "0007-use-sqlite.md", docs) is True
    # Ordinary docs are not historical, so real API promises keep vetoing.
    assert _is_historical_doc(docs / "api.md", docs) is False
    assert _is_historical_doc(docs / "architecture" / "overview.md", docs) is False

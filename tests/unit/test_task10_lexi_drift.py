"""Task 10 #3 - generic Lexi drift fixes, proven on synthetic trees.

Nothing reads a real repository: each case builds a tiny docs+code tree under
``tmp_path`` and calls the pure ``_heuristic_drift`` directly (no LLM, no bwrap,
no network). Each test pairs a false positive (must fall below the veto line)
with a real-drift control that has the *same shape* and must stay high - so the
fix is proven narrow, not a blanket suppression.
"""

from __future__ import annotations

from pathlib import Path

from agents.lexi import _heuristic_drift


def _run(docs_root: Path, project_root: Path) -> dict[str, str]:
    report = _heuristic_drift(docs_root=docs_root, project_root=project_root, config={})
    return {f.path: f.severity for f in report.findings}


# --- block-quote asides are quoted words, not API promises -------------------


def test_blockquote_mention_is_not_high(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "notes.md").write_text(
        "# Ops\n\n"
        "> URL rewrites are evaluated in the `http_request_transform` phase.\n"
        "> Cache rules run afterwards in `http_request_cache_settings`.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("http_request_transform") != "high"
    assert sev.get("http_request_cache_settings") != "high"


def test_prose_mention_outside_quote_stays_high(tmp_path: Path):
    # The control: a code-shaped term named in an ordinary declarative line is
    # real drift and must keep vetoing, exactly as the seeded fixtures require.
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "overview.md").write_text(
        "# API\n\nDefines `MAX_UPLOAD_BYTES` as the hard limit.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("MAX_UPLOAD_BYTES") == "high"


def test_term_in_quote_and_plain_line_still_high(tmp_path: Path):
    # Conservative: only when *every* mention is a quoted aside does it demote.
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "mixed.md").write_text(
        "> See `payment_gateway`.\n\nThe `payment_gateway` handler processes charges.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("payment_gateway") == "high"


# --- CI pipeline inputs are real declared code, not missing symbols ----------


def test_workflow_input_is_not_missing_code(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "deploy.yml").write_text(
        "on:\n  workflow_dispatch:\n    inputs:\n      confirm_sha:\n        description: sha\n",
        encoding="utf-8",
    )
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "ops.md").write_text(
        "# Deploy\n\nDeployment runs through `deploy.yml` with the `confirm_sha` parameter.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert "confirm_sha" not in sev


def test_ordinary_config_key_is_still_drift(tmp_path: Path):
    # The control: a name that lives only in a non-pipeline config file is NOT
    # treated as code evidence (a project must not hide drift in its config).
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "settings.yaml").write_text("secret_internal_token: abc\n", encoding="utf-8")
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "api.md").write_text(
        "# Config\n\nReads `secret_internal_token` at startup.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("secret_internal_token") == "high"


# --- glossary list items define a term, they do not promise an API -----------


def test_glossary_definition_item_is_not_high(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "modes.md").write_text(
        "# Modes\n\n"
        "- `DIFF_WATCH`: analyze only the git diff.\n"
        "- `INCIDENT_LOG`: record the cause of the failure.\n"
        "- `WEEKLY_REPORT`: overview of the codebase state.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    for name in ("DIFF_WATCH", "INCIDENT_LOG", "WEEKLY_REPORT"):
        assert sev.get(name) != "high"


def test_glossary_with_parenthetical_alias_is_not_high(tmp_path: Path):
    # The same term defined a second way with an alias before the colon stays a
    # glossary entry: ``- `MODE_X` (Label): description``.
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "modes.md").write_text(
        "# Modes\n\n"
        "- `DIFF_WATCH`: read only the git diff.\n"
        "- `DIFF_WATCH` (Watch): read the git diff and the touched files.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("DIFF_WATCH") != "high"


def test_glossary_with_call_form_stays_high(tmp_path: Path):
    # The control: a definition item that shows a *call* form is still an API
    # promise, so it keeps vetoing (the call form outranks the glossary shape).
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "ref.md").write_text(
        "# API\n\n- `fetch_record()`: returns the record by key.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("fetch_record") == "high"


# --- incident / change-plan lines that cite a source location ----------------


def test_source_cited_narration_is_not_high(tmp_path: Path):
    # A name that only ever appears on lines pointing at a concrete file:line is
    # the docs *reporting about* code (a post-mortem / task list), not promising
    # an API - so it stays below the veto line.
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "incidents.md").write_text(
        "# Incident\n\n"
        "**Problem:** `calculate_platform_fee_lamports` (`core_verify.py:60-70`) "
        "has no caller outside tests (`test_core_verify.py:15`).\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("calculate_platform_fee_lamports") != "high"


def test_api_claim_without_citation_stays_high(tmp_path: Path):
    # The control: a plain API promise that cites no location is real drift.
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "api.md").write_text(
        "# API\n\nThe module exposes `parse_user_record` for callers.\n",
        encoding="utf-8",
    )
    sev = _run(docs, tmp_path)
    assert sev.get("parse_user_record") == "high"

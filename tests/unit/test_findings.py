"""Task 12: the IDE-ready findings projection.

Covers the documented priority mapping (docs/findings-priority.md), the verdict
rule, schema conformance of the emitted document, the ~40-line markdown bound
and the determinism of ids. The projection must never invent a verdict and must
never let an environment finding reach P0/P1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import findings


# --- helpers ---------------------------------------------------------------
def _report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "run-1",
        "verdict": "CLEAR",
        "finished_at": "2026-11-02T00:00:00+00:00",
        "drift": {},
        "plan": {},
        "policy": {},
        "execution": {},
        "post_audit": {},
        "role_results": [],
    }
    base.update(overrides)
    return base


def _by_type(ftype: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in items if f["type"] == ftype]


def _find(build: dict[str, Any], *, agent: str, ftype: str) -> dict[str, Any]:
    hits = [
        f for f in findings.build_findings(build)
        if f["agent"] == agent and f["type"] == ftype
    ]
    assert hits, f"no {agent}/{ftype} finding in {build}"
    return hits[0]


# --- verdict rule ----------------------------------------------------------
def test_only_p0_p1_with_evidence_may_veto() -> None:
    assert findings.verdict_effect_for("P0", "boom") == "VETO"
    assert findings.verdict_effect_for("P1", "boom") == "VETO"


def test_empty_evidence_never_vetoes() -> None:
    assert findings.verdict_effect_for("P0", "") == "WARN"
    assert findings.verdict_effect_for("P1", "   ") == "WARN"


def test_p2_warns_and_p3_is_info() -> None:
    assert findings.verdict_effect_for("P2", "boom") == "WARN"
    assert findings.verdict_effect_for("P3", "boom") == "INFO"
    assert findings.verdict_effect_for("P3", "") == "INFO"


def test_dropped_low_drift_without_evidence_is_info_not_veto() -> None:
    build = _report(drift={
        "findings": [
            {"kind": "missing_code", "path": "docs/a.md", "severity": "low",
             "message": "", "docs_file": "a.md", "docs_line": 3},
        ]
    })
    finding = _find(build, agent="lexi", ftype="drift")
    assert finding["priority"] == "P3"
    assert finding["verdict_effect"] == "INFO"


# --- priority mapping: drift ----------------------------------------------
def test_drift_severity_maps_high_medium_low() -> None:
    build = _report(drift={
        "findings": [
            {"kind": "missing_code", "path": "x()", "severity": "high",
             "message": "docs promise x()", "docs_file": "api.md", "docs_line": 7},
            {"kind": "missing_code", "path": "y()", "severity": "medium",
             "message": "docs mention y()", "docs_file": "api.md", "docs_line": 8},
            {"kind": "domain_term", "path": "word", "severity": "low",
             "message": "docs talk about word", "docs_file": "guide.md", "docs_line": 1},
        ]
    })
    got = {f["evidence"]: f["priority"] for f in _by_type("drift", findings.build_findings(build))}
    assert got["docs promise x()"] == "P1"
    assert got["docs mention y()"] == "P2"
    assert got["docs talk about word"] == "P3"


def test_drift_carries_the_docs_anchor_and_veto_effect() -> None:
    build = _report(drift={
        "findings": [
            {"kind": "missing_code", "path": "transfer()", "severity": "high",
             "message": "docs promise transfer()", "docs_file": "api.md", "docs_line": 42},
        ]
    })
    finding = _find(build, agent="lexi", ftype="drift")
    assert (finding["file"], finding["line"]) == ("api.md", 42)
    assert finding["verdict_effect"] == "VETO"
    assert finding["proposed_red_test"].count("transfer()") >= 1


def test_missing_tests_drift_projects_as_weak_test() -> None:
    build = _report(drift={
        "findings": [
            {"kind": "missing_tests", "path": "core/payments.py", "severity": "medium",
             "message": "core/payments.py has no tests"},
        ]
    })
    finding = _find(build, agent="lexi", ftype="weak_test")
    assert finding["priority"] == "P2"
    assert finding["file"] == "core/payments.py"


# --- priority mapping: claims ---------------------------------------------
def test_uncovered_aria_claim_is_p2_warn() -> None:
    build = _report(plan={
        "scenarios": [
            {"id": "C1", "title": "Refunds always settle", "acceptance": ["settled"],
             "covered_by": []},
            {"id": "S1", "title": "not a claim", "covered_by": []},
            {"id": "C2", "title": "covered", "covered_by": ["tests/test_a.py::t"]},
        ]
    })
    claims = _by_type("claim", findings.build_findings(build))
    assert len(claims) == 1
    assert claims[0]["agent"] == "aria"
    assert claims[0]["priority"] == "P2"
    assert claims[0]["verdict_effect"] == "WARN"


def test_riley_claim_gaps_project_as_claims() -> None:
    build = _report(post_audit={
        "riley": {"gaps": [
            "claim not covered by tests: refunds always settle",
            "claim 'zero fees' covered only by weak test tests/test_fees.py::t",
        ]},
    })
    claims = [f for f in _by_type("claim", findings.build_findings(build)) if f["agent"] == "riley"]
    assert len(claims) == 2
    assert all(f["priority"] == "P2" for f in claims)


# --- priority mapping: weak tests -----------------------------------------
def test_riley_weak_test_gap_carries_the_node_file() -> None:
    build = _report(post_audit={
        "riley": {"gaps": ["weak test: tests/test_login.py::test_ok (no assertions)"]},
    })
    finding = _find(build, agent="riley", ftype="weak_test")
    # Untagged "no assertions" is P2: only a pass-only body is tagged [P1].
    assert finding["priority"] == "P2"
    assert finding["file"] == "tests/test_login.py"
    assert finding["verdict_effect"] == "WARN"


def test_tagged_pass_only_no_assertions_is_p1() -> None:
    build = _report(post_audit={
        "riley": {"gaps": [
            "weak test: tests/test_login.py::test_ok ([P1] no assertions) @ tests/test_login.py:4"
        ]},
    })
    finding = _find(build, agent="riley", ftype="weak_test")
    assert finding["priority"] == "P1"
    assert finding["verdict_effect"] == "VETO"
    assert finding["line"] == 4


def test_riley_aggregate_repeats_are_skipped() -> None:
    build = _report(post_audit={
        "riley": {"gaps": [
            "test detail: something",
            "failed=3 in the suite",
            "pytest exit code 1",
            "pytest collection/execution errors seen",
            "environment-blocked failures=5",
        ]},
    })
    assert findings.build_findings(build) == []


# --- priority mapping: policy ----------------------------------------------
def test_secret_exposure_policy_failure_is_p0_veto() -> None:
    build = _report(policy={
        "preflight": {
            "checks": {"no_secret_env": False, "collected_gt_zero": True},
            "blocked_reasons": ["found AWS_SECRET in config.py"],
            "warnings": [],
        },
        "post": {"checks": {}, "blocked_reasons": [], "warnings": []},
    })
    finding = _find(build, agent="sable", ftype="policy")
    assert finding["priority"] == "P0"
    assert finding["verdict_effect"] == "VETO"


def test_policy_check_priorities_follow_the_documented_split() -> None:
    build = _report(policy={
        "preflight": {
            "checks": {"not_live_tree": False, "pytest_only": False, "collected_gt_zero": False},
            "blocked_reasons": [],
            "warnings": [],
        },
    })
    by_check = {}
    for f in _by_type("policy", findings.build_findings(build)):
        key = "not_live_tree" if "not_live_tree" in f["evidence"] else (
            "pytest_only" if "pytest_only" in f["evidence"] else "collected_gt_zero")
        by_check[key] = f["priority"]
    assert by_check == {"not_live_tree": "P0", "pytest_only": "P1", "collected_gt_zero": "P2"}


def test_policy_warning_is_p3_info() -> None:
    build = _report(policy={
        "preflight": {"checks": {}, "blocked_reasons": [],
                      "warnings": ["rm -rf tolerated under sandbox"]},
    })
    finding = _find(build, agent="sable", ftype="policy")
    assert finding["priority"] == "P3"
    assert finding["verdict_effect"] == "INFO"


def test_tests_ast_clean_failure_extracts_the_offending_file() -> None:
    build = _report(policy={
        "preflight": {
            "checks": {"tests_ast_clean": False},
            "blocked_reasons": ["conftest.py contains subprocess.call"],
            "warnings": [],
        },
    })
    finding = _find(build, agent="sable", ftype="policy")
    assert finding["file"] == "conftest.py"


# --- priority mapping: nova -------------------------------------------------
def test_product_failure_is_p1_with_the_red_test_kept() -> None:
    build = _report(execution={
        "failed": 1,
        "failures": [
            {"node": "tests/test_calc.py::test_divide", "classification": "product",
             "message": "assert 1 == 2", "traceback": "tests/test_calc.py:12: in test_divide"},
        ],
    })
    finding = _find(build, agent="nova", ftype="test_failure")
    assert finding["priority"] == "P1"
    assert (finding["file"], finding["line"]) == ("tests/test_calc.py", 12)
    assert finding["verdict_effect"] == "VETO"
    assert "test_divide" in finding["proposed_red_test"]


def test_money_security_product_failure_escalates_to_p0() -> None:
    build = _report(execution={
        "failed": 1,
        "failures": [
            {"node": "tests/test_refund.py::test_refund_double_payout",
             "classification": "product", "message": "wrong amount", "traceback": ""},
        ],
    })
    finding = _find(build, agent="nova", ftype="test_failure")
    assert finding["priority"] == "P0"


def test_environment_failure_is_never_p0_or_p1() -> None:
    build = _report(execution={
        "failed": 2,
        "environment_failed": 2,
        "failures": [
            {"node": "tests/test_ui.py::test_render", "classification": "environment",
             "environment_kind": "missing_tool", "environment_cause": "node not installed",
             "message": "FileNotFoundError: node", "traceback": "tests/test_ui.py:5: in test_render"},
            {"node": "tests/test_git.py::t", "classification": "environment",
             "environment_kind": "missing_vcs_metadata", "environment_cause": "git ls-files failed",
             "message": "", "traceback": ""},
        ],
    })
    env = _by_type("environment", findings.build_findings(build))
    assert len(env) == 2
    assert all(f["priority"] == "P2" for f in env)
    assert all(f["agent"] == "nova" for f in env)


def test_environment_cause_reaches_veto_effect_via_evidence_only_as_warn() -> None:
    # an environment P2 with evidence must still be WARN, never VETO
    build = _report(execution={
        "failed": 1,
        "failures": [
            {"node": "tests/test_ui.py::t", "classification": "environment",
             "environment_cause": "bwrap missing", "message": "boom", "traceback": ""},
        ],
    })
    env = _by_type("environment", findings.build_findings(build))[0]
    assert env["verdict_effect"] == "WARN"


def test_pip_install_failure_projects_as_environment() -> None:
    build = _report(execution={"failed": 0, "install_status": "pip install failed: no network"})
    env = _by_type("environment", findings.build_findings(build))
    assert env[0]["priority"] == "P2"
    assert "pip install failed" in env[0]["evidence"]


def test_blocked_env_without_failures_yields_one_environment_finding() -> None:
    build = _report(
        verdict="BLOCKED_ENV",
        execution={},
        post_audit={"root_cause": "sandbox unavailable: bwrap missing"},
    )
    env = _by_type("environment", findings.build_findings(build))
    assert len(env) == 1
    assert env[0]["priority"] == "P2"
    assert env[0]["evidence"] == "sandbox unavailable: bwrap missing"


def test_clipped_run_still_emits_aggregate_test_failure() -> None:
    build = _report(execution={"failed": 4, "errors": 0, "exit_code": 1, "failures": []})
    fails = _by_type("test_failure", findings.build_findings(build))
    assert len(fails) == 1
    assert fails[0]["priority"] == "P1"
    assert "failed=4" in fails[0]["evidence"]


# --- dedupe, ordering, ids --------------------------------------------------
def test_same_gap_twice_yields_one_finding() -> None:
    gap = {"riley": {"gaps": ["weak test: tests/t.py::a (no assertions)"]}}
    build = _report(post_audit=gap, role_results=[
        {"role": "riley", "stage": "adversarial_plan", "details": dict(gap["riley"])},
    ])
    assert len(findings.build_findings(build)) == 1


def test_findings_are_ordered_and_numbered_from_f001() -> None:
    build = _report(
        drift={"findings": [
            {"kind": "missing_code", "path": "lo", "severity": "low", "message": "m1"},
            {"kind": "missing_code", "path": "hi", "severity": "high", "message": "m2"},
        ]},
        execution={"failed": 1, "failures": [
            {"node": "tests/x.py::t", "classification": "product", "message": "m3", "traceback": ""},
        ]},
    )
    got = findings.build_findings(build)
    assert [f["id"] for f in got] == ["F-001", "F-002", "F-003"]
    assert [f["priority"] for f in got] == ["P1", "P1", "P3"]
    assert [f["agent"] for f in got] == ["lexi", "nova", "lexi"]


def test_projection_is_deterministic() -> None:
    build = _report(
        drift={"findings": [
            {"kind": "missing_code", "path": "a()", "severity": "high", "message": "docs a()",
             "docs_file": "api.md", "docs_line": 2},
        ]},
        plan={"scenarios": [{"id": "C1", "title": "claim one", "covered_by": []}]},
    )
    first = json.dumps(findings.build_document(build), sort_keys=False)
    second = json.dumps(findings.build_document(build), sort_keys=False)
    assert first == second


def test_evidence_is_single_line_and_clipped() -> None:
    long_message = "line one\nline two " + "x" * 400
    build = _report(drift={"findings": [
        {"kind": "missing_code", "path": "p", "severity": "high", "message": long_message},
    ]})
    evidence = findings.build_findings(build)[0]["evidence"]
    assert "\n" not in evidence
    assert len(evidence) <= findings.MAX_EVIDENCE_CHARS
    assert evidence.endswith("...")


# --- schema ----------------------------------------------------------------
def test_schema_file_matches_module_constants() -> None:
    schema = findings.load_schema()
    item = schema["properties"]["findings"]["items"]["properties"]
    assert sorted(item["agent"]["enum"]) == sorted(findings.FINDING_AGENTS)
    assert sorted(item["type"]["enum"]) == sorted(findings.FINDING_TYPES)
    assert sorted(item["priority"]["enum"]) == sorted(findings.PRIORITY_LEVELS)
    assert sorted(item["verdict_effect"]["enum"]) == sorted(findings.VERDICT_EFFECTS)
    assert set(item) >= {"id", "agent", "file", "line", "priority", "type",
                         "customer_impact", "evidence", "proposed_red_test", "verdict_effect"}


def test_built_document_validates_against_the_schema() -> None:
    build = _report(
        drift={"findings": [
            {"kind": "missing_code", "path": "a()", "severity": "high", "message": "docs a()",
             "docs_file": "api.md", "docs_line": 2},
        ]},
        policy={"preflight": {"checks": {"no_secret_env": False},
                              "blocked_reasons": ["secret in app.py"], "warnings": []}},
        execution={"failed": 1, "failures": [
            {"node": "tests/t.py::t", "classification": "product", "message": "boom",
             "traceback": "tests/t.py:3: in t"}]},
    )
    doc = findings.build_document(build)
    assert findings.validate_schema(doc, findings.load_schema()) == []


def test_schema_rejects_a_bad_document() -> None:
    schema = findings.load_schema()
    bad = {"schema_version": "1", "run_id": "r"}
    errors = findings.validate_schema(bad, schema)
    joined = " ".join(errors)
    assert "missing required 'verdict'" in joined
    assert "missing required 'counts'" in joined
    assert "missing required 'findings'" in joined


def test_schema_rejects_bad_enum_and_types() -> None:
    schema = findings.load_schema()
    doc = findings.build_document(_report(drift={"findings": [
        {"kind": "missing_code", "path": "a", "severity": "high", "message": "m"}]}))
    doc["findings"][0]["priority"] = "P9"
    doc["findings"][0]["line"] = "7"
    errors = " ".join(findings.validate_schema(doc, schema))
    assert "P9" in errors
    assert "expected type ['integer', 'null']" in errors


def test_null_file_and_line_are_schema_legal() -> None:
    build = _report(plan={"scenarios": [{"id": "C1", "title": "unproven", "covered_by": []}]})
    doc = findings.build_document(build)
    assert findings.validate_schema(doc, findings.load_schema()) == []
    claim = _by_type("claim", doc["findings"])[0]
    assert claim["file"] is None and claim["line"] is None


# --- markdown --------------------------------------------------------------
def _many_findings(count: int) -> dict[str, Any]:
    return _report(post_audit={"riley": {"gaps": [
        f"weak test: tests/test_{i}.py::t{i} (no assertions)" for i in range(count)
    ]}})


def test_markdown_has_the_documented_shape() -> None:
    doc = findings.build_document(_many_findings(3))
    text = findings.render_markdown(doc)
    lines = text.splitlines()
    assert lines[0].startswith("# Findings - run ")
    assert "| priority | count |" in lines
    assert "| P2 | 3 |" in lines
    assert "## Top 3 findings" in lines
    for line in lines:
        if line.startswith("- [F-"):
            assert "`tests/test_" in line
            assert line.endswith("(WARN)")


def test_markdown_caps_at_max_lines_with_a_hint() -> None:
    doc = findings.build_document(_many_findings(60))
    text = findings.render_markdown(doc)
    lines = text.splitlines()
    assert len(lines) <= findings.MD_MAX_LINES
    assert "## Top 10 findings" in lines
    assert sum(1 for line in lines if line.startswith("- [F-")) == findings.MD_TOP_COUNT
    assert any("findings.json" in line for line in lines[-2:])


def test_empty_run_markdown_says_no_findings() -> None:
    text = findings.render_markdown(findings.build_document(_report()))
    assert "No findings." in text
    assert "| P0 | 0 |" in text


# --- write_findings ---------------------------------------------------------
def test_write_findings_emits_json_and_markdown(tmp_path: Path) -> None:
    build = _many_findings(2)
    json_path, md_path = findings.write_findings(build, tmp_path / "out")
    assert json_path.name == "findings.json" and md_path.name == "findings.md"
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["run_id"] == "run-1"
    assert loaded["counts"]["total"] == 2
    assert findings.validate_schema(loaded, findings.load_schema()) == []
    assert md_path.read_text(encoding="utf-8").startswith("# Findings - run run-1")


# --- CLI surface ------------------------------------------------------------
def test_findings_dir_flag_defaults_to_empty_and_parses() -> None:
    import main as main_module

    default = main_module.parse_args(["--project", "p", "--docs", "d"])
    assert default.findings_dir == ""
    parsed = main_module.parse_args(
        ["--project", "p", "--docs", "d", "--findings-dir", "/tmp/fx"]
    )
    assert parsed.findings_dir == "/tmp/fx"


def test_counts_match_the_findings_list() -> None:
    doc = findings.build_document(_many_findings(5))
    assert doc["counts"]["total"] == len(doc["findings"]) == 5
    assert doc["counts"]["P2"] == 5
    assert doc["counts"]["P0"] == doc["counts"]["P1"] == doc["counts"]["P3"] == 0


def test_finding_class_and_low_confidence_heuristic_p2_cap() -> None:
    """Low-confidence heuristic findings must be capped at P2 (never P1/P0), while hard findings keep P0/P1."""
    build = _report(
        drift={
            "findings": [
                {
                    "kind": "missing_code",
                    "path": "uncertain_fn()",
                    "severity": "high",
                    "confidence": "low",
                    "class": "heuristic",
                    "message": "low confidence heuristic claim",
                    "docs_file": "README.md",
                    "docs_line": 10,
                },
                {
                    "kind": "missing_cli_flag",
                    "path": "--strict",
                    "severity": "high",
                    "confidence": "high",
                    "class": "heuristic",
                    "message": "high confidence missing flag",
                    "docs_file": "README.md",
                    "docs_line": 12,
                },
            ]
        },
        execution={
            "failed": 1,
            "failures": [
                {
                    "node": "tests/test_core.py::test_hard_fail",
                    "classification": "product",
                    "message": "AssertionError",
                    "traceback": "tests/test_core.py:5: in test_hard_fail",
                }
            ],
            "flaky": 1,
            "flaky_tests": [
                {
                    "node": "tests/test_core.py::test_flaky_one",
                    "classification": "flaky",
                    "message": "timed out once",
                    "traceback": "tests/test_core.py:20: in test_flaky_one",
                }
            ],
        },
    )
    items = findings.build_findings(build)
    by_term = {f["fingerprint"].split(":", 3)[-1]: f for f in items}

    low_heur = by_term["uncertain_fn()"]
    assert low_heur["class"] == "heuristic"
    assert low_heur["confidence"] == "low"
    assert low_heur["priority"] == "P2"
    assert low_heur["verdict_effect"] == "WARN"

    high_heur = by_term["--strict"]
    assert high_heur["class"] == "heuristic"
    assert high_heur["confidence"] == "high"
    assert high_heur["priority"] == "P1"
    assert high_heur["verdict_effect"] == "VETO"

    hard_fail = by_term["tests/test_core.py::test_hard_fail"]
    assert hard_fail["class"] == "hard"
    assert hard_fail["confidence"] == "high"
    assert hard_fail["priority"] == "P1"

    flaky_find = by_term["tests/test_core.py::test_flaky_one"]
    assert flaky_find["class"] == "hard"
    assert flaky_find["priority"] == "P2"
    assert flaky_find["verdict_effect"] == "WARN"
    assert "FLAKY" in flaky_find["evidence"]

    # DriftFinding.to_dict() and ExecutionResult status transitions
    from core.packets import DriftFinding, ExecutionResult

    df = DriftFinding(kind="missing_code", path="fn()", severity="warn", message="missing")
    assert df.to_dict()["class"] == "heuristic"
    assert ExecutionResult(exit_code=0).status == "PASS"
    assert ExecutionResult(exit_code=1).status == "FAIL"
    assert ExecutionResult(flaky=1, exit_code=0).status == "FLAKY"

    # apply_baseline direct checks
    assert findings.apply_baseline(items, None) is items
    baselined_items = findings.apply_baseline(items, {high_heur["fingerprint"]})
    assert baselined_items is items
    assert high_heur.get("baselined") is True
    assert high_heur["priority"] == "P3"


def test_verdict_effect_for_fail_on_levels_and_schema_v1_0_0() -> None:
    assert findings.verdict_effect_for("P0", "ev", fail_on="P0") == "VETO"
    assert findings.verdict_effect_for("P1", "ev", fail_on="P0") == "WARN"
    assert findings.verdict_effect_for("P2", "ev", fail_on="P2") == "VETO"
    assert findings.verdict_effect_for("P3", "ev", fail_on="P2") == "INFO"
    assert findings.verdict_effect_for("P3", "ev", fail_on="P3") == "VETO"
    assert findings.verdict_effect_for("P0", "", fail_on="P0") == "WARN"

    doc_p2 = findings.build_document(_many_findings(2), fail_on="P2")
    assert all(f["verdict_effect"] == "VETO" for f in doc_p2["findings"])
    doc_v100 = dict(doc_p2, schema_version="1.0.0")
    assert findings.validate_schema(doc_v100, findings.load_schema()) == []





def test_swallowed_privacy_weak_test_is_p1_with_file_line() -> None:
    build = _report(post_audit={
        "riley": {"gaps": [
            "weak test: tests/test_fulfillment.py::test_privacy_no_plaintext_email_anywhere "
            "([P1] assertion failures swallowed by try/except) @ tests/test_fulfillment.py:12"
        ]},
    })
    finding = _find(build, agent="riley", ftype="weak_test")
    assert finding["priority"] == "P1"
    assert finding["verdict_effect"] == "VETO"
    assert finding["file"] == "tests/test_fulfillment.py"
    assert finding["line"] == 12


def test_skipped_weak_test_stays_p2() -> None:
    build = _report(post_audit={
        "riley": {"gaps": ["weak test: tests/test_x.py::test_skip (skipped test) @ tests/test_x.py:4"]},
    })
    finding = _find(build, agent="riley", ftype="weak_test")
    assert finding["priority"] == "P2"
    assert finding["verdict_effect"] == "WARN"
    assert finding["line"] == 4


def test_claim_findings_carry_docs_file_line_and_never_veto() -> None:
    build = _report(
        plan={"scenarios": [{
            "id": "C3",
            "title": "must never store a plaintext email",
            "covered_by": [],
            "docs_file": "docs/README.md",
            "docs_line": 4,
        }]},
        post_audit={"riley": {"gaps": ["claim C3 is covered only by weak test (not counted as covered)"]}},
        verdict="CLEAR",
    )
    aria_claims = [f for f in _by_type("claim", findings.build_findings(build)) if f["agent"] == "aria"]
    riley_claims = [f for f in _by_type("claim", findings.build_findings(build)) if f["agent"] == "riley"]
    assert aria_claims and aria_claims[0]["file"] == "docs/README.md"
    assert aria_claims[0]["line"] == 4
    assert riley_claims and riley_claims[0]["file"] == "docs/README.md"
    assert riley_claims[0]["line"] == 4
    doc = findings.build_document(build, fail_on="P2")
    assert all(f["verdict_effect"] == "WARN" for f in doc["findings"] if f["type"] == "claim")


def test_verdict_from_findings_and_consistency_invariant() -> None:
    veto_finding = {
        "verdict_effect": "VETO",
        "priority": "P1",
        "evidence": "swallowed",
    }
    warn_finding = {"verdict_effect": "WARN", "priority": "P2", "evidence": "claim C1"}
    assert findings.verdict_from_findings("CLEAR", [veto_finding]) == "VETO"
    assert findings.verdict_from_findings("VETO", [warn_finding]) == "CLEAR"
    assert findings.verdict_from_findings("BLOCKED_ENV", [warn_finding]) == "BLOCKED_ENV"
    assert findings.verdict_from_findings("BLOCKED_ENV", [veto_finding]) == "BLOCKED_ENV"
    assert findings.verdict_from_findings("ESCALATE", [warn_finding]) == "ESCALATE"
    findings.check_verdict_consistent("VETO", [veto_finding])
    findings.check_verdict_consistent("CLEAR", [warn_finding])
    try:
        findings.check_verdict_consistent("VETO", [warn_finding])
    except findings.VerdictInconsistent as exc:
        assert "without a VETO finding" in str(exc)
    else:
        raise AssertionError("expected VerdictInconsistent")
    try:
        findings.check_verdict_consistent("CLEAR", [veto_finding])
    except findings.VerdictInconsistent as exc:
        assert "verdict is CLEAR" in str(exc)
    else:
        raise AssertionError("expected VerdictInconsistent")

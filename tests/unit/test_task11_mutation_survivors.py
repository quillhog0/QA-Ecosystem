"""Task 11 - mutation: kill the surviving mutants the seeded sample surfaced.

Adding code to Lexi, the orchestrator and the environment classifier shifted the
positions of every source file, so the seeded 30-mutant draw now includes sites
that were never exercised before. None of the survivors are in the Task 11 logic
itself (that code is covered by the dedicated Task 11 tests); they are pre-existing
gaps in modules around it. Each test drives one surviving mutant to the opposite
verdict, so the mutant is killed without touching any production code.
"""

from __future__ import annotations

import ast
from pathlib import Path

from agents.lexi import _Mention, _external_reference
from agents.riley import _unittest_assertion_present
from core.environment import product_failure_count
from core.mutation import run_mutation_testing
from core.report import _failures_section
from policies.sable import _isolation_contains_secrets


# --- lexi.py:443  _Mention.forbidden: rule-heading prohibition -> return None --


def test_forbidden_rule_heading_returns_true():
    # A non-negated mention under a short prohibitive heading ("Never use") is
    # forbidden via the heading rule on the final line of forbidden(). Mutating
    # that `return <expr>` to `return None` loses the True.
    mention = _Mention(
        "The handler `parse_thing` loads data.",
        "Never use",
        "",
        False,
        False,
        "",
        "",
        "",
        "",
        (),
    )
    assert mention.forbidden("parse_thing") is True


# --- lexi.py:730  _external_reference: dotted name qualified in docs -> False ---


def test_qualified_dotted_name_is_external():
    assert (
        _external_reference("foo", "see a.b.foo for details", project_symbols=set(), lowered_symbols=set())
        is True
    )


# --- riley.py:184  _unittest_assertion_present: self.fail attr -> != ----------


def test_self_fail_counts_as_unittest_assertion():
    tree = ast.parse("def test_x(self):\n    self.fail('nope')\n")
    assert _unittest_assertion_present(tree.body[0]) is True


# --- environment.py:793  product_failure_count: unclassified-but-listed -------


def test_product_failure_count_uses_raw_failed_when_unannotated():
    execution = {"failed": 2, "failures": [{"node": "tests/test_a.py::t"}]}
    assert product_failure_count(execution) == 2


# --- sable.py:112  _isolation_contains_secrets: missing root -> None ----------


def test_secret_scan_of_missing_root_is_empty_list(tmp_path: Path):
    assert _isolation_contains_secrets(tmp_path / "does-not-exist") == []


# --- mutation.py:381  run_mutation_testing skipped branch: enabled -> False ----


def test_run_mutation_skipped_branch_stays_enabled(tmp_path: Path):
    root = tmp_path / "isolated"
    root.mkdir()
    (root / "mod.py").write_text("def f(x, y):\n    return x > 0 and y + 1\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    result = run_mutation_testing(root, work_dir=work, config={"mutation": {"max_mutants": 0}})
    assert result["status"] == "skipped"
    assert result["enabled"] is True


# --- report.py:202  _failures_section: environment explainer placed wrongly ---


def test_environment_explainer_follows_environment_heading():
    execution = {
        "failed": 2,
        "errors": 0,
        "product_failed": 1,
        "environment_failed": 1,
        "failures": [
            {"node": "tests/test_a.py::test_one", "classification": "product", "message": "boom", "kind": "failure"},
            {
                "node": "tests/test_b.py::test_two",
                "classification": "environment",
                "message": "no node",
                "kind": "failure",
                "environment_kind": "missing_tool",
            },
        ],
    }
    lines = _failures_section(execution)
    env_idx = lines.index("### Environment failures")
    explainer_idx = next(i for i, line in enumerate(lines) if line.startswith("These tests did not judge"))
    # The caveat belongs after the environment group, not the product group.
    assert explainer_idx > env_idx

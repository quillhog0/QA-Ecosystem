from __future__ import annotations

from agents.riley import review_plan, review_results
from core.packets import Scenario, TestPlan, Verdict


def test_riley_vetoes_plan_without_negative_scenario():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=["collected > 0"],
                negative=False,
            )
        ]
    )
    review = review_plan(plan, config={"llm": {"provider": "none"}, "dry_run": True})
    assert review.verdict == Verdict.VETO.value
    assert review.root_cause


def test_riley_vetoes_plan_without_acceptance_criteria():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=[],
                negative=False,
            ),
            Scenario(
                id="S2",
                title="invalid input",
                steps=["send bad data"],
                acceptance=[],
                negative=True,
            ),
        ]
    )
    review = review_plan(plan, config={"llm": {"provider": "none"}, "dry_run": True})
    assert review.verdict == Verdict.VETO.value
    assert review.root_cause


def test_riley_collection_error_missing_dependency():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=["collected > 0"],
                negative=False,
            ),
            Scenario(
                id="S2",
                title="negative path",
                steps=["run tests"],
                acceptance=["fail on error"],
                negative=True,
            ),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 0,
        "failed": 0,
        "errors": 1,
        "exit_code": 2,
        "stdout": (
            "ERROR collecting tests/test_calculator.py\n"
            "ModuleNotFoundError: No module named 'qa_nonexistent_pkg_xyz'"
        ),
        "stderr": "",
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}
    )
    assert review.verdict == Verdict.VETO.value
    assert "missing dependency: qa_nonexistent_pkg_xyz" in review.root_cause


def test_riley_records_gap_for_uncovered_claim():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="C1",
                title="Never burns tokens",
                steps=["verify"],
                acceptance=["pass"],
                negative=False,
                covered_by=[],
            ),
            Scenario(
                id="S1",
                title="negative path",
                steps=["run tests"],
                acceptance=["fail on error"],
                negative=True,
            ),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "stdout": "1 passed",
        "stderr": "",
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}
    )
    assert review.verdict == Verdict.CLEAR.value
    assert "claim not covered by tests: C1" in review.gaps


def test_riley_monotonic_veto_llm_cannot_override_to_clear(monkeypatch):
    import agents.riley
    monkeypatch.setattr(
        agents.riley,
        "complete_with_status",
        lambda **kwargs: ("verdict: CLEAR\nroot_cause: ''\nnotes: looks fine to me", "ok"),
    )
    plan = TestPlan(
        scenarios=[
            Scenario(id="S1", title="happy", steps=["run"], acceptance=["ok"], negative=False),
            Scenario(id="S2", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 0,
        "failed": 1,
        "errors": 0,
        "exit_code": 1,
        "stdout": "FAILED test_x.py::test_fail",
        "stderr": "",
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "mock"}, "dry_run": False}
    )
    assert review.verdict == Verdict.VETO.value
    assert review.root_cause


def test_riley_vetoes_on_uncovered_claims_when_configured():
    plan = TestPlan(
        scenarios=[
            Scenario(id="C1", title="claim without test", steps=["verify"], acceptance=["pass"], negative=False, covered_by=[]),
            Scenario(id="S1", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "stdout": "1 passed",
        "stderr": "",
    }
    # When false (default): CLEAR
    review_clear = review_results(
        plan, execution, config={"riley": {"veto_on_uncovered_claims": False}, "llm": {"provider": "none"}, "dry_run": True}
    )
    assert review_clear.verdict == Verdict.CLEAR.value

    # When true: VETO
    review_veto = review_results(
        plan, execution, config={"riley": {"veto_on_uncovered_claims": True}, "llm": {"provider": "none"}, "dry_run": True}
    )
    assert review_veto.verdict == Verdict.VETO.value
    assert "uncovered claims" in review_veto.root_cause


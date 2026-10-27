from __future__ import annotations

from agents.riley import review_plan
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

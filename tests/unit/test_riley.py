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


def test_riley_ast_detects_weak_tests_and_vetoes_uncovered_claim(tmp_path):
    from agents.riley import detect_weak_tests
    from pathlib import Path

    proj = tmp_path / "proj"
    proj.mkdir()
    test_file = proj / "test_weak.py"
    test_file.write_text(
        "def test_empty():\n"
        "    x = 1\n"
        "def test_tautology_none():\n"
        "    assert x is not None\n"
        "def test_tautology_true():\n"
        "    assert True\n"
        "def test_tautology_callable():\n"
        "    assert callable(x)\n"
        "def test_strong_assertion():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    weak = detect_weak_tests(proj)
    assert "test_empty" in weak and weak["test_empty"] == "no assertions"
    assert "test_tautology_none" in weak and weak["test_tautology_none"] == "tautology assertion"
    assert "test_tautology_true" in weak and weak["test_tautology_true"] == "tautology assertion"
    assert "test_tautology_callable" in weak and weak["test_tautology_callable"] == "tautology assertion"
    assert "test_strong_assertion" not in weak

    plan = TestPlan(
        scenarios=[
            Scenario(id="C1", title="claim", steps=["verify"], acceptance=["pass"], negative=False, covered_by=["test_weak.py::test_tautology_none"]),
            Scenario(id="S1", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    plan_rev = review_plan(plan, config={"llm": {"provider": "none"}, "dry_run": True}, project_root=proj)
    # Claim coverage is discounted: covered_by cleared to []
    assert plan.scenarios[0].covered_by == []
    assert any("claim C1 is covered only by weak test" in g for g in plan_rev.gaps)

    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "stdout": "1 passed",
        "stderr": "",
    }
    res_rev = review_results(plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}, project_root=proj)
    assert res_rev.verdict == Verdict.VETO.value
    assert "weak test detected" in res_rev.root_cause


def test_riley_vetoes_when_pip_install_failed():
    plan = TestPlan(
        scenarios=[
            Scenario(id="S1", title="happy", steps=["run"], acceptance=["ok"], negative=False),
            Scenario(id="S2", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "install_status": "pip install failed: unsafe requirement: git+https://example.com/repo.git",
        "stdout": "1 passed",
        "stderr": "",
    }
    rev = review_results(plan, execution, config={"llm": {"provider": "none"}, "dry_run": True})
    assert rev is not None
    assert rev.verdict == Verdict.VETO.value
    assert "dependency installation failed" in rev.root_cause




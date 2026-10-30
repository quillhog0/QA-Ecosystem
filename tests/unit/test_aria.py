from __future__ import annotations

from pathlib import Path

from agents.aria import _extract_body_tokens, _find_covering_tests, _stems, design
from tests.support import mini_project


def test_aria_creates_scenarios_from_claims(tmp_path: Path):
    root = mini_project(tmp_path / "proj")
    claims = ["The API must never return null.", "All data is always consistent."]
    plan = design(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
        claims=claims,
    )
    assert len(plan.scenarios) >= len(claims)
    assert any(s.id == "C1" and "The API must never return null." in s.title for s in plan.scenarios)
    assert any(s.id == "C2" and "All data is always consistent." in s.title for s in plan.scenarios)


def test_aria_creates_no_claim_scenarios_without_claims(tmp_path: Path):
    root = mini_project(tmp_path / "proj")
    plan = design(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert not any(s.id.startswith("C") for s in plan.scenarios)
    s2 = next(s for s in plan.scenarios if s.id == "S2")
    assert s2.negative is False


def test_aria_from_llm_preserves_scenarios_and_assumptions():
    from agents.aria import _from_llm
    from core.packets import Scenario, TestPlan

    fallback = TestPlan(
        scenarios=[
            Scenario(
                id="C1",
                title="Fallback claim",
                steps=["s"],
                acceptance=["a"],
                negative=False,
                covered_by=["tests/test_x.py::test_a"],
                score=6,
            )
        ],
        coverage_notes="fallback notes",
        assumptions=["fallback assumption"],
    )
    llm_data = {
        "scenarios": [
            {
                "id": "C1",
                "title": "LLM claim",
                "steps": ["step 1"],
                "acceptance": ["acc 1"],
                "negative": True,
            }
        ],
        "coverage_notes": "llm notes",
        "assumptions": ["llm custom assumption"],
    }
    merged = _from_llm(llm_data, fallback)
    assert len(merged.scenarios) == 1
    assert merged.scenarios[0].title == "LLM claim"
    assert merged.scenarios[0].negative is True
    assert merged.scenarios[0].covered_by == ["tests/test_x.py::test_a"]
    assert merged.scenarios[0].score == 6
    assert merged.assumptions == ["llm custom assumption"]


def test_aria_maps_claim_to_covering_test(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        code={"tests/test_core.py": "def test_no_burn():\n    assert True\n"},
        docs="# Docs\n",
    )
    plan = design(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
        claims=["Never burns tokens"],
    )
    claim_scenario = next(s for s in plan.scenarios if s.id == "C1")
    assert any("test_no_burn" in cb for cb in claim_scenario.covered_by)


def test_aria_find_covering_tests_mtu_safety():
    claim = "MTU Safety: payload fits 1232 bytes"
    tests = [
        (
            "test_mtu_batch_size_cap",
            "test_mtu_batch_size_cap",
            "common_helper(1232, payload=True)\nassert True",
        ),
        (
            "test_dest_equals_owner_invariant",
            "test_dest_equals_owner_invariant",
            "common_helper(1232, payload=True)\nassert True",
        ),
    ]
    covered = _find_covering_tests(claim, tests)
    assert covered == ["test_mtu_batch_size_cap"]


def test_aria_find_covering_tests_no_name_match():
    claim = "Completely unmatched claim without any name correspondence"
    tests = [
        (
            "test_mtu_batch_size_cap",
            "test_mtu_batch_size_cap",
            "common_helper(1232, payload=True)\nassert True",
        ),
        (
            "test_dest_equals_owner_invariant",
            "test_dest_equals_owner_invariant",
            "common_helper(1232, payload=True)\nassert True",
        ),
    ]
    covered = _find_covering_tests(claim, tests)
    assert covered == []


def test_aria_scored_covering_tests_integration(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        code={
            "tests/test_core.py": (
                "def test_mtu_batch_size_cap():\n"
                "    common_helper(1232, payload=True)\n"
                "    assert True\n\n"
                "def test_dest_equals_owner_invariant():\n"
                "    common_helper(1232, payload=True)\n"
                "    assert True\n"
            )
        },
        docs="# Docs\n",
    )
    plan = design(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
        claims=[
            "MTU Safety: payload fits 1232 bytes",
            "Completely unmatched claim without any name correspondence",
        ],
    )
    c1 = next(s for s in plan.scenarios if s.id == "C1")
    c2 = next(s for s in plan.scenarios if s.id == "C2")
    assert c1.covered_by == ["tests/test_core.py::test_mtu_batch_size_cap"]
    assert getattr(c1, "score", 0) >= 3
    assert c2.covered_by == []
    assert getattr(c2, "score", 0) < 3


def test_claim_coverage_markdown_has_score_column():
    from core.packets import FinalReport
    from core.report import render_markdown

    report = FinalReport(
        run_id="run-1",
        verdict="CLEAR",
        dry_run=True,
        project_root="/proj",
        docs_root="/proj/docs",
        isolated_root="/tmp/iso",
        started_at="now",
        finished_at="now",
        plan={
            "scenarios": [
                {
                    "id": "C1",
                    "title": "MTU Safety",
                    "covered_by": ["tests/test_core.py::test_mtu_batch_size_cap"],
                    "score": 3,
                },
                {
                    "id": "C2",
                    "title": "Unmatched Claim",
                    "covered_by": [],
                    "score": 0,
                },
            ]
        },
    )
    md = render_markdown(report)
    assert "| claim | covered_by | score |" in md
    assert "| MTU Safety | tests/test_core.py::test_mtu_batch_size_cap | 3 |" in md
    assert "| Unmatched Claim | NONE | 0 |" in md


def test_stems_only_splits_plural_words_longer_than_three():
    """aria.py:37 - the 's' stem applies only when len(word) > 3, not >= 3."""
    assert _stems("bus") == {"bus"}
    assert _stems("here") == {"here"}
    assert "box" in _stems("boxes")


def test_extract_body_tokens_underscore_parts_honour_length_and_stopwords():
    """aria.py:75 - underscore parts are kept iff len >= 3 and not a stop word."""
    assert "add" in _extract_body_tokens("add_")
    assert "xy" not in _extract_body_tokens("xy_z")
    assert "z" not in _extract_body_tokens("xy_z")
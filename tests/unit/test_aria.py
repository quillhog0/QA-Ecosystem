from __future__ import annotations

from pathlib import Path

from agents.aria import design
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
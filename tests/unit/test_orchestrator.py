from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from agents.riley import review_plan
from core.orchestrator import ESCALATE_DOMAIN, Vesper
from core.packets import RedTeamReview, TestPlan, Verdict
from tests.support import mini_project


def test_orchestrator_escalates_after_two_riley_plan_vetoes(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    reports = tmp_path / "reports"
    calls = {"n": 0}

    def always_veto(plan: TestPlan, *, config: dict, **_kwargs) -> RedTeamReview:
        calls["n"] += 1
        return RedTeamReview(
            verdict=Verdict.VETO.value,
            gaps=["forced gap"],
            required_changes=["add negative path"],
            root_cause="forced veto for iteration cap",
            notes="forced",
        )

    config = {
        "dry_run": True,
        "allow_network": False,
        "max_iterations": 2,
        "reports_dir": str(reports),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
    }
    with patch("agents.riley.review_plan", side_effect=always_veto):
        report = Vesper(config).run(project, project / "docs")

    assert report.verdict == Verdict.ESCALATE.value
    assert report.escalation == ESCALATE_DOMAIN
    riley_plan = [
        r
        for r in report.role_results
        if r.role == "riley" and r.stage == "adversarial_plan"
    ]
    assert len(riley_plan) == 2
    assert calls["n"] == 2
    assert max(r.iteration for r in riley_plan) == 2
    assert review_plan is not None


def test_orchestrator_records_llm_fallback_on_exception(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    reports = tmp_path / "reports"
    config = {
        "dry_run": False,
        "allow_network": True,
        "reports_dir": str(reports),
        "llm": {"provider": "local", "base_url": "http://127.0.0.1:11434/v1", "call_in_dry_run": True},
        "isolation_method": "copy",
    }
    with patch("agents.adapters.complete", side_effect=RuntimeError("Simulated LLM network error")):
        report = Vesper(config).run(project, project / "docs")

    md_text = ""
    for p in reports.rglob("*.md"):
        if report.run_id in p.name:
            md_text = p.read_text(encoding="utf-8")
            break

    assert "- llm: fallback" in md_text
    assert any("fallback" in r.llm for r in report.role_results)


def test_project_config_cannot_override_effective_config(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    (project / "config.yaml").write_text(
        "allow_network: true\ndry_run: false\nkeep_isolated_copy: true\n",
        encoding="utf-8",
    )
    reports = tmp_path / "reports"
    system_config = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(reports),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "keep_isolated_copy": False,
    }
    import agents.nova as nova
    import policies.sable as sable

    with patch("policies.sable.evaluate_preflight", wraps=sable.evaluate_preflight) as mock_sable, \
         patch("agents.nova.execute", wraps=nova.execute) as mock_nova:
        report = Vesper(system_config).run(project, project / "docs")

    assert report.dry_run is True
    assert mock_sable.called
    assert mock_sable.call_args.kwargs["allow_network"] is False
    assert mock_nova.called
    assert mock_nova.call_args.kwargs["allow_network"] is False
    assert mock_nova.call_args.kwargs["config"]["allow_network"] is False
    assert mock_nova.call_args.kwargs["config"]["dry_run"] is True


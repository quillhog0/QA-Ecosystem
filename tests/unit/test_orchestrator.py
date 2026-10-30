from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
    assert all(r.verdict == Verdict.VETO.value for r in riley_plan)


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


def test_vesper_default_dry_run_is_true_and_loop_returns_none_on_veto(tmp_path: Path):
    from core.isolation import IsolationHandle
    from core.packets import ExecutionResult, FinalReport, HandoffPacket

    assert Vesper({}).dry_run is True

    project = mini_project(tmp_path / "proj")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    vesper = Vesper({"reports_dir": str(tmp_path / "reports"), "isolation_method": "copy"})
    handle = IsolationHandle(
        live_root=project,
        isolated_root=project,
        method="copy",
        read_only=False,
        skipped_secrets=[],
    )
    packet = HandoffPacket(
        run_id="r1",
        stage="init",
        from_role="vesper",
        to_role="lexi",
        project_root=str(project.parent),
        docs_root=str(project / "docs"),
        isolated_root=str(project),
    )
    report = FinalReport(run_id="r1", verdict=Verdict.PENDING.value)
    failed_exec = ExecutionResult(collected=1, passed=0, failed=1, exit_code=1, sandbox_status="off")
    with patch("agents.nova.execute", return_value=failed_exec):
        loop_ret = vesper._loop(packet, handle, work_dir, report)
    assert loop_ret is None
    assert report.verdict == Verdict.VETO.value


def test_adapters_complete_and_status_network_and_provider_guards(monkeypatch):
    from unittest.mock import MagicMock
    from agents import adapters

    monkeypatch.setenv("OPENROUTER_API_KEY", "test_key_val")
    openrouter_no_net_cfg = {
        "dry_run": False,
        "allow_network": False,
        "llm": {"provider": "openrouter", "call_in_dry_run": True},
    }
    assert adapters.complete(
        system_prompt="sys", user_payload={"a": 1}, config=openrouter_no_net_cfg
    ) is None

    with patch("agents.adapters.complete", side_effect=AssertionError("complete must not be called")):
        raw, status = adapters.complete_with_status(
            system_prompt="sys", user_payload={"a": 1}, config=openrouter_no_net_cfg
        )
    assert raw is None
    assert status == "none"

    local_no_net_cfg = {
        "dry_run": False,
        "allow_network": False,
        "llm": {
            "provider": "local",
            "base_url": "http://127.0.0.1:11434/v1",
            "call_in_dry_run": True,
        },
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"choices": [{"message": {"content": "verdict: CLEAR"}}]}
    mock_client = MagicMock()
    mock_client.__enter__.return_value.post.return_value = mock_resp
    with patch("httpx.Client", return_value=mock_client):
        res = adapters.complete(
            system_prompt="sys", user_payload={"a": 1}, config=local_no_net_cfg
        )
    assert res == "verdict: CLEAR"




def test_venv_python_matches_prepare_venv_location(tmp_path: Path):
    from core.deps import venv_dir, venv_python

    assert venv_dir(tmp_path) == tmp_path / "venv"
    assert venv_python(tmp_path).parent.parent == tmp_path / "venv"


def test_mutation_audit_uses_project_venv_python(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    fake_python = tmp_path / "fake-python"
    fake_python.write_text("", encoding="utf-8")
    config = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "reports"),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "mutation": {"enabled": True},
    }
    captured: dict = {}

    def fake_audit(isolated, **kwargs):
        captured.update(kwargs)
        return {"status": "completed", "verdict": Verdict.CLEAR.value, "score": 1.0}

    with patch("core.orchestrator.venv_python", return_value=fake_python), \
         patch("agents.riley.audit_mutations", side_effect=fake_audit):
        Vesper(config).run(project, project / "docs")

    assert captured.get("python") == fake_python


def test_sable_execute_summary_when_no_blocked_reasons(tmp_path: Path):
    """Post-execution Sable summary falls back to 'collected > 0' (guards `... or \"collected > 0\"`)."""
    project = mini_project(tmp_path / "proj")
    config = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "reports"),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "sandbox": {"mode": "off"},
    }
    report = Vesper(config).run(project, project / "docs")

    sable_execute = [
        r for r in report.role_results if r.role == "sable" and r.stage == "execute"
    ]
    assert sable_execute, "expected a Sable post-execution result"
    assert sable_execute[-1].verdict == Verdict.CLEAR.value
    assert sable_execute[-1].summary == "collected > 0"


def test_orchestrator_invalid_sandbox_mode_fails_before_isolation(tmp_path: Path):
    import pytest

    project = mini_project(tmp_path / "proj")
    reports = tmp_path / "reports"
    config = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(reports),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "sandbox": {"mode": "bogus"},
    }
    with patch("core.orchestrator.create_isolation") as create:
        with pytest.raises(ValueError, match="Unknown sandbox mode"):
            Vesper(config).run(project, project / "docs")
    create.assert_not_called()
    assert not reports.exists()

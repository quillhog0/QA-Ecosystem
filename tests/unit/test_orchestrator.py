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
    # Task 9 fix H split the network switches: a remote provider (openrouter) is
    # refused because *LLM* network is off, and the status now names that reason
    # rather than the old generic "none".
    assert status == "none: llm network disabled"

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


def test_orchestrator_suppresses_live_git_branch_in_drift(tmp_path: Path):
    """Case B end-to-end: a branch known only to the live ``.git`` is not flagged,
    even though the isolated copy the drift check reads never carries ``.git``.

    A genuinely absent term is still reported, and under strict mode that keeps
    the whole run a VETO, so the pipeline returns before Nova and the assertion
    is about the drift findings alone.
    """
    project = mini_project(tmp_path / "proj")
    git = project / ".git"
    (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "refs" / "heads" / "release").write_text(
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4\n", encoding="utf-8"
    )
    (project / "docs" / "overview.md").write_text(
        "# Workflow\n\nThe `release` branch is protected; `absent_flower` is unknown.\n",
        encoding="utf-8",
    )

    reports = tmp_path / "reports"
    config = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(reports),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "lexi": {"veto_on_missing_code": True},
    }
    report = Vesper(config).run(project, project / "docs")

    missing = {
        item["path"]
        for item in report.drift.get("findings", [])
        if item.get("kind") == "missing_code"
    }
    assert "release" not in missing
    assert "absent_flower" in missing
    assert report.verdict == Verdict.VETO.value


def test_install_timing_is_execute_window_minus_pytest(tmp_path: Path):
    """The install phase is only the part of the execute window that pytest did
    not consume, so a long pytest run with no installs yields a zero install."""
    from core.isolation import IsolationHandle
    from core.packets import ExecutionResult, FinalReport, HandoffPacket

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
    slow_exec = ExecutionResult(
        collected=1,
        passed=0,
        failed=1,
        exit_code=1,
        sandbox_status="off",
        duration_seconds=5.0,
    )
    with patch("agents.nova.execute", return_value=slow_exec):
        vesper._loop(packet, handle, work_dir, report)

    assert report.timings["pytest"] == 5.0
    assert report.timings["install"] == 0.0


def _relaxation_loop_inputs(tmp_path: Path):
    """A dangerous mini project plus the packet/handle/report _loop needs."""
    from core.isolation import IsolationHandle
    from core.packets import FinalReport, HandoffPacket

    project = mini_project(tmp_path / "proj", dangerous=True)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
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
    # Preflight decided the sandbox was active, so dangerous ops became WARN.
    report.sandbox = "bwrap"
    vesper = Vesper({"reports_dir": str(tmp_path / "reports"), "isolation_method": "copy"})
    return vesper, packet, handle, work_dir, report


def test_sandbox_lost_after_preflight_relaxations_vetoes_without_executing(tmp_path: Path):
    """TOCTOU: relaxations were granted because bwrap was active at pre-flight.

    If the sandbox is no longer usable when execution would start, the cycle must
    VETO instead of running the isolated copy unsandboxed under relaxed rules.
    """
    from core.packets import ExecutionResult

    vesper, packet, handle, work_dir, report = _relaxation_loop_inputs(tmp_path)

    def never_run(*args, **kwargs) -> ExecutionResult:
        raise AssertionError("nova.execute must not run unsandboxed after relaxations")

    with patch("agents.nova.execute", side_effect=never_run), patch(
        "core.sandbox.get_sandbox_status", return_value=(False, "unavailable", None)
    ):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.VETO.value
    assert report.sandbox == "unavailable"
    refusals = [
        item
        for item in vesper.role_results
        if item.role == "sable" and "refusing to run" in item.summary
    ]
    assert len(refusals) == 1
    assert refusals[0].verdict == Verdict.BLOCK.value
    assert refusals[0].details["sandbox"] == "unavailable"


def test_sandbox_still_active_keeps_relaxed_preflight(tmp_path: Path):
    """The same relaxation proceeds to execution when the sandbox is still there."""
    from core.packets import ExecutionResult

    vesper, packet, handle, work_dir, report = _relaxation_loop_inputs(tmp_path)
    ok_exec = ExecutionResult(
        collected=1, passed=1, failed=0, exit_code=0, sandbox_status="bwrap"
    )
    with patch("agents.nova.execute", return_value=ok_exec), patch(
        "core.sandbox.get_sandbox_status", return_value=(True, "bwrap", None)
    ):
        vesper._loop(packet, handle, work_dir, report)

    assert not [item for item in vesper.role_results if "refusing to run" in item.summary]
    assert any(item.role == "nova" for item in vesper.role_results)


def test_relaxations_granted_reads_warnings_and_sandbox_allows():
    from core.orchestrator import _relaxations_granted
    from policies import sable

    class Decision:
        def __init__(self, warnings=None, allowed=None):
            self.warnings = warnings if warnings is not None else []
            self.allowed = allowed if allowed is not None else []

    assert _relaxations_granted(Decision()) is False
    assert _relaxations_granted(Decision(warnings=["p.py: call eval [in source]"])) is True
    assert (
        _relaxations_granted(
            Decision(
                allowed=[
                    {
                        "path": "p.py",
                        "call": "shutil.rmtree",
                        "reason": sable.SANDBOX_DELETE_REASON,
                    }
                ]
            )
        )
        is True
    )
    assert (
        _relaxations_granted(
            Decision(allowed=[{"path": "p.py", "call": "os.unlink", "reason": "operator allow-list"}])
        )
        is False
    )


# --- environment-blocked aggregation ----------------------------------------
def _env_loop_inputs(tmp_path: Path, *, dangerous: bool = False):
    """A mini project plus the packet/handle/report ``_loop`` needs."""
    from core.isolation import IsolationHandle
    from core.packets import FinalReport, HandoffPacket

    project = mini_project(tmp_path / "proj", dangerous=dangerous)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
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
    # No sandbox: Sable evaluates strictly, so a dangerous project is BLOCKed.
    report.sandbox = "off"
    vesper = Vesper({"reports_dir": str(tmp_path / "reports"), "isolation_method": "copy"})
    return vesper, packet, handle, work_dir, report


def _blocked_execution(**overrides):
    from core.packets import ExecutionResult

    data = {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "errors": 1,
        "exit_code": 2,
        "stderr": "E   ModuleNotFoundError: No module named 'qa_absent_dep'",
        "command": ["pytest"],
        "install_status": "no dependency manifest",
        "sandbox_status": "off",
    }
    data.update(overrides)
    return ExecutionResult(**data)


def _role(results, role: str, stage: str):
    return [item for item in results if item.role == role and item.stage == stage]


def test_a_missing_dependency_blocks_instead_of_vetoing(tmp_path: Path):
    from core.report import exit_code_for

    vesper, packet, handle, work_dir, report = _env_loop_inputs(tmp_path)
    with patch("agents.nova.execute", return_value=_blocked_execution()):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.BLOCKED_ENV.value
    assert exit_code_for(report.verdict) == 2
    assert report.post_audit["root_cause"].startswith("missing dependency: qa_absent_dep")
    assert report.mutation == {
        "status": "skipped",
        "reason": "environment blocked: the tests could not run",
    }
    # Riley softens its technical block, Nova keeps the raw execution signal.
    assert _role(vesper.role_results, "riley", "post_audit")[0].verdict == Verdict.BLOCKED_ENV.value
    assert _role(vesper.role_results, "nova", "execute")[0].verdict == Verdict.VETO.value
    # Lexi's zero-collection contradiction stays below the veto line.
    lexi_post = _role(vesper.role_results, "lexi", "post_audit")[0]
    assert lexi_post.verdict == Verdict.CLEAR.value
    contradiction = [f for f in report.post_audit["lexi"]["findings"] if f["kind"] == "contradiction"]
    assert contradiction[0]["severity"] == "medium"
    assert "environment blocked: missing dependency: qa_absent_dep" in contradiction[0]["message"]


def test_a_partial_collection_with_a_missing_dependency_also_blocks(tmp_path: Path):
    """collected > 0 keeps Sable's post check CLEAR, so the non-zero pytest exit
    is the only technical block - and it is environmental."""
    vesper, packet, handle, work_dir, report = _env_loop_inputs(tmp_path)
    execution = _blocked_execution(collected=66, passed=64, errors=2)
    with patch("agents.nova.execute", return_value=execution):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.BLOCKED_ENV.value
    assert report.execution["collected"] == 66
    assert _role(vesper.role_results, "sable", "execute")[0].verdict == Verdict.CLEAR.value
    assert report.mutation["reason"] == "environment blocked: the tests could not run"


def test_a_failing_test_keeps_the_veto_despite_a_missing_dependency(tmp_path: Path):
    from core.report import exit_code_for

    vesper, packet, handle, work_dir, report = _env_loop_inputs(tmp_path)
    execution = _blocked_execution(
        collected=2,
        passed=1,
        failed=1,
        errors=1,
        exit_code=1,
        stdout="FAILED tests/test_calculator.py::test_add - assert 5 == 0",
    )
    with patch("agents.nova.execute", return_value=execution):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.VETO.value
    assert exit_code_for(report.verdict) == 1
    assert report.post_audit["root_cause"].startswith("pytest failures: test_add")
    assert _role(vesper.role_results, "riley", "post_audit")[0].verdict == Verdict.VETO.value


def test_dangerous_code_plus_a_missing_dependency_is_a_veto(tmp_path: Path):
    """Sable's pre-flight BLOCK outranks any environmental explanation, and the
    suite is never executed."""
    from core.report import exit_code_for

    vesper, packet, handle, work_dir, report = _env_loop_inputs(tmp_path, dangerous=True)

    def never_run(*args, **kwargs):
        raise AssertionError("nova.execute must not run after a pre-flight BLOCK")

    with patch("agents.nova.execute", side_effect=never_run):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.VETO.value
    assert exit_code_for(report.verdict) == 1
    assert _role(vesper.role_results, "sable", "preflight")[0].verdict == Verdict.BLOCK.value
    assert not _role(vesper.role_results, "nova", "execute")


def test_a_non_pytest_command_keeps_the_veto_when_environment_blocked(tmp_path: Path):
    """The downgrade only applies to Sable's "collected 0" block. A command that
    is not pytest-only is a policy violation, not an environment problem."""
    vesper, packet, handle, work_dir, report = _env_loop_inputs(tmp_path)
    execution = _blocked_execution(command=["bash", "-c", "pytest"])
    with patch("agents.nova.execute", return_value=execution):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.VETO.value
    post = _role(vesper.role_results, "sable", "execute")[0]
    assert post.verdict == Verdict.BLOCK.value
    assert post.details["checks"]["pytest_only"] is False
    # A policy block is not an environment stop: the skip reason stays the baseline.
    assert report.mutation["reason"] == "baseline tests did not pass"


def test_an_unexplained_empty_collection_still_vetoes(tmp_path: Path):
    vesper, packet, handle, work_dir, report = _env_loop_inputs(tmp_path)
    execution = _blocked_execution(
        errors=0, exit_code=5, stderr="", stdout="no tests ran in 0.01s"
    )
    with patch("agents.nova.execute", return_value=execution):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.VETO.value
    lexi_post = _role(vesper.role_results, "lexi", "post_audit")[0]
    assert lexi_post.verdict == Verdict.VETO.value


def test_a_healthy_run_stays_clear(tmp_path: Path):
    from core.packets import ExecutionResult
    from core.report import exit_code_for

    vesper, packet, handle, work_dir, report = _env_loop_inputs(tmp_path)
    ok = ExecutionResult(
        collected=3, passed=3, exit_code=0, command=["pytest"], sandbox_status="off"
    )
    with patch("agents.nova.execute", return_value=ok):
        vesper._loop(packet, handle, work_dir, report)

    assert report.verdict == Verdict.CLEAR.value
    assert exit_code_for(report.verdict) == 0
    assert report.mutation == {"status": "skipped", "reason": "disabled"}


def _required_sandbox_config(tmp_path: Path) -> dict:
    return {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "reports"),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "sandbox": {"mode": "required"},
    }


def test_a_required_sandbox_that_cannot_start_blocks_and_releases_the_copy(tmp_path: Path):
    from core.report import exit_code_for

    project = mini_project(tmp_path / "proj")

    def never_run(*args, **kwargs):
        raise AssertionError("nova.execute must not run without the required sandbox")

    with patch("core.sandbox.get_sandbox_status", return_value=(False, "unavailable", "bwrap not found")), \
         patch("agents.nova.execute", side_effect=never_run):
        report = Vesper(_required_sandbox_config(tmp_path)).run(project, project / "docs")

    assert report.verdict == Verdict.BLOCKED_ENV.value
    assert exit_code_for(report.verdict) == 2
    assert report.sandbox == "unavailable"
    assert report.post_audit["root_cause"] == "sandbox required but bwrap unavailable: bwrap not found"
    assert report.mutation == {
        "status": "skipped",
        "reason": "environment blocked before execution",
    }
    # The design stages still ran: only execution was skipped.
    assert report.plan and report.drift
    assert not Path(report.isolated_root).exists()
    md = next(path for path in (tmp_path / "reports").rglob("*.md") if report.run_id in path.name)
    assert "environment blocked" in md.read_text(encoding="utf-8")


def test_a_required_sandbox_that_cannot_start_still_vetoes_dangerous_code(tmp_path: Path):
    """Precedence is monotonic: VETO > BLOCKED_ENV > CLEAR."""
    project = mini_project(tmp_path / "proj", dangerous=True)
    with patch("core.sandbox.get_sandbox_status", return_value=(False, "unavailable", "bwrap not found")), \
         patch("agents.nova.execute", side_effect=AssertionError("must not execute")):
        report = Vesper(_required_sandbox_config(tmp_path)).run(project, project / "docs")

    assert report.verdict == Verdict.VETO.value
    assert not Path(report.isolated_root).exists()
    preflight = [item for item in report.role_results if item.role == "sable" and item.stage == "preflight"]
    assert preflight[0].verdict == Verdict.BLOCK.value


def test_profile_and_fail_on_validation_and_verdict_override(tmp_path: Path, monkeypatch):
    import pytest
    from core.orchestrator import apply_profile
    import main as main_mod

    with pytest.raises(ValueError, match="invalid profile"):
        apply_profile({"profile": "unknown"})
    with pytest.raises(ValueError, match="invalid fail_on"):
        apply_profile({"fail_on": "P9"})

    factory_cfg = apply_profile({"profile": "factory"})
    assert factory_cfg["profile"] == "factory"
    assert factory_cfg["fail_on"] == "P2"
    assert factory_cfg["identity_strict"] is True
    assert factory_cfg["mutation"]["enabled"] is True

    # Explicit overrides preserved under factory profile
    override_cfg = apply_profile({"profile": "factory", "fail_on": "p0", "identity_strict": False, "mutation": {"enabled": False}})
    assert override_cfg["fail_on"] == "P0"
    assert override_cfg["identity_strict"] is False
    assert override_cfg["mutation"]["enabled"] is False

    # pull_request_target security guard in main.py
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    assert main_mod.main(["--self-test"]) == 2
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)

    # CLI flags parsing and wiring
    project = mini_project(tmp_path / "proj")
    reports_dir = tmp_path / "reports_cli"
    rc = main_mod.main([
        "--project", str(project),
        "--docs", str(project / "docs"),
        "--reports-dir", str(reports_dir),
        "--profile", "default",
        "--fail-on", "p2",
        "--fix-brief", "false",
    ])
    assert rc in (0, 1)
    assert not (reports_dir / "latest" / "fix-brief.md").exists()
    assert (reports_dir / "latest" / "pr-comment.md").is_file()

    # fail_on="P0" downgrades a P1 drift VETO to CLEAR when tests pass
    (project / "docs" / "extra.md").write_text("# Extra\n\nCall `missing_func_xyz()` now.\n", encoding="utf-8")
    rep_p0 = Vesper({
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "rep_p0"),
        "isolation_method": "copy",
        "fail_on": "P0",
        "lexi": {"veto_on_missing_code": True},
    }).run(project, project / "docs")
    assert rep_p0.verdict == Verdict.CLEAR.value

    # fail_on="P2" upgrades a run that had only a P2 drift finding (veto_on_missing_code=False) from CLEAR to VETO
    rep_p2 = Vesper({
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "rep_p2"),
        "isolation_method": "copy",
        "fail_on": "P2",
        "lexi": {"veto_on_missing_code": False},
    }).run(project, project / "docs")
    assert rep_p2.verdict == Verdict.VETO.value



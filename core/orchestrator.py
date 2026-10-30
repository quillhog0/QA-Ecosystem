"""Vesper — orchestrator. Owns the loop, iteration caps, and escalation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents import aria, lexi, nova, riley
from core.isolation import (
    IsolationHandle,
    archive_note,
    create_isolation,
    isolation_destination,
    release_isolation,
    _make_sources_readonly,
)
from core.deps import venv_python
from core.packets import (
    FinalReport,
    HandoffPacket,
    Role,
    RoleResult,
    Stage,
    Verdict,
    new_run_id,
    utc_now,
)
from core.report import append_result, write_reports
from core.runner import sanitized_env
from policies import sable


ESCALATE_DOMAIN = "ESCALATE: DOMAIN_EXPERT"


class Vesper:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.max_stage_iterations = int(config.get("max_iterations") or 2)
        self.allow_network = bool(config.get("allow_network", False))
        self.dry_run = bool(config.get("dry_run", True))
        self.timeout = int((config.get("pytest") or {}).get("timeout_seconds") or 120)
        self.role_results: list[RoleResult] = []

    def run(self, project: Path, docs: Path) -> FinalReport:
        from core.sandbox import get_sandbox_status, resolve_sandbox_mode

        # Validate operator config before any isolated copy exists, so an
        # invalid sandbox mode cannot leak a copy under the temp directory.
        resolve_sandbox_mode(self.config)
        started = utc_now()
        run_id = new_run_id()
        base = Path(self.config.get("reports_dir") or "reports").resolve()
        run_dir = base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        isolated_dest = isolation_destination(
            live_root=project.resolve(),
            reports_dir=base,
            run_id=run_id,
        )

        isolation = create_isolation(
            project,
            isolated_dest,
            method=str(self.config.get("isolation_method") or "auto"),
            read_only=False,
        )
        archive_note(isolation, run_dir)
        work_dir = run_dir / "nova_work"
        work_dir.mkdir(parents=True, exist_ok=True)

        packet = HandoffPacket(
            run_id=run_id,
            stage=Stage.INIT.value,
            from_role=Role.VESPER.value,
            to_role=Role.LEXI.value,
            project_root=str(project.resolve()),
            docs_root=str(docs.resolve()),
            isolated_root=str(isolation.isolated_root),
            dry_run=self.dry_run,
            allow_network=self.allow_network,
        )
        append_result(
            self.role_results,
            RoleResult(
                role=Role.VESPER.value,
                stage=Stage.INIT.value,
                verdict=Verdict.CLEAR.value,
                summary=f"isolation via {isolation.method}; live tree untouched; tested_tree: {isolation.tested_tree}",
                details=isolation.to_dict(),
            ),
        )

        _, sandbox_status, failure_reason = get_sandbox_status(self.config)
        report = FinalReport(
            run_id=run_id,
            verdict=Verdict.PENDING.value,
            project_root=str(project.resolve()),
            docs_root=str(docs.resolve()),
            isolated_root=str(isolation.isolated_root),
            dry_run=self.dry_run,
            sandbox=sandbox_status,
            started_at=started,
            tested_tree=isolation.tested_tree,
        )

        if failure_reason:
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.VESPER.value,
                    stage=Stage.INIT.value,
                    verdict=Verdict.VETO.value,
                    summary=f"sandbox required but bwrap unavailable: {failure_reason}",
                    details={"sandbox": sandbox_status, "error": failure_reason},
                ),
            )
            report.verdict = Verdict.VETO.value
            report.role_results = list(self.role_results)
            report.finished_at = utc_now()
            write_reports(report, base)
            return report

        try:
            try:
                self._loop(packet, isolation, work_dir, report)
            finally:
                report.role_results = list(self.role_results)
                report.finished_at = utc_now()
                write_reports(report, base)
        finally:
            release_isolation(isolation, keep=bool(self.config.get("keep_isolated_copy", False)))
        return report

    def _loop(
        self,
        packet: HandoffPacket,
        isolation: IsolationHandle,
        work_dir: Path,
        report: FinalReport,
    ) -> None:
        isolated = isolation.isolated_root
        docs = Path(packet.docs_root)

        # 1. Lexi drift check
        drift = None
        for attempt in range(1, self.max_stage_iterations + 1):
            packet = packet.next(
                stage=Stage.DRIFT_CHECK.value,
                from_role=Role.VESPER.value,
                to_role=Role.LEXI.value,
            )
            drift = lexi.inspect(
                docs_root=docs,
                project_root=isolated,
                config=self.config,
            )
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.LEXI.value,
                    stage=Stage.DRIFT_CHECK.value,
                    verdict=drift.verdict,
                    summary=drift.notes,
                    details=drift.to_dict(),
                    iteration=attempt,
                    llm=drift.llm_status,
                ),
            )
            report.drift = drift.to_dict()
            if drift.verdict != Verdict.VETO.value:
                break
            # Drift is not something this loop can patch; stop after first veto.
            report.verdict = Verdict.VETO.value
            report.escalation = ""
            return

        assert drift is not None

        # 2–3. Aria design with Riley review (max iterations)
        plan = None
        review = None
        gaps: list[str] = []
        for attempt in range(1, self.max_stage_iterations + 1):
            packet = packet.next(
                stage=Stage.DESIGN.value,
                from_role=Role.VESPER.value,
                to_role=Role.ARIA.value,
                payload={"gaps": gaps, "claims": drift.claims},
            )
            plan = aria.design(
                docs_root=docs,
                project_root=isolated,
                config=self.config,
                gaps=gaps,
                claims=drift.claims,
            )
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.ARIA.value,
                    stage=Stage.DESIGN.value,
                    verdict=Verdict.CLEAR.value,
                    summary=f"{len(plan.scenarios)} scenarios",
                    details=plan.to_dict(),
                    iteration=attempt,
                    llm=plan.llm_status,
                ),
            )
            report.plan = plan.to_dict()

            packet = packet.next(
                stage=Stage.ADVERSARIAL_PLAN.value,
                from_role=Role.ARIA.value,
                to_role=Role.RILEY.value,
                payload=plan.to_dict(),
            )
            review = riley.review_plan(plan, config=self.config, project_root=isolated)
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.RILEY.value,
                    stage=Stage.ADVERSARIAL_PLAN.value,
                    verdict=review.verdict,
                    summary=review.notes,
                    details=review.to_dict(),
                    iteration=attempt,
                    llm=review.llm_status,
                ),
            )
            report.plan = plan.to_dict()
            if review.verdict != Verdict.VETO.value:
                break
            gaps = review.gaps + review.required_changes
            if attempt >= self.max_stage_iterations:
                report.verdict = Verdict.ESCALATE.value
                report.escalation = ESCALATE_DOMAIN
                return

        assert plan is not None and review is not None

        # 4. Sable pre-flight
        command = sable.default_command(isolated, work_dir)
        env = sanitized_env(allow_network=self.allow_network)
        env["HOME"] = str(work_dir / "home")
        packet = packet.next(
            stage=Stage.PREFLIGHT.value,
            from_role=Role.VESPER.value,
            to_role=Role.SABLE.value,
            payload={"command": command},
        )
        policy = sable.evaluate_preflight(
            command=command,
            isolated_root=isolated,
            live_root=Path(packet.project_root),
            allow_network=self.allow_network,
            env=env,
            cwd=isolated,
            allow_in_tests=(self.config.get("sable") or {}).get("allow_in_tests"),
            sandboxed=report.sandbox == "bwrap",
        )
        allowed_note = ""
        if policy.allowed:
            allowed_note = "; ALLOWED: " + "; ".join(
                f"{item.get('path')} {item.get('call')} ({item.get('reason')})"
                for item in policy.allowed
            )
        append_result(
            self.role_results,
            RoleResult(
                role=Role.SABLE.value,
                stage=Stage.PREFLIGHT.value,
                verdict=policy.verdict,
                summary=(
                    "; ".join(policy.blocked_reasons)
                    or "pre-flight checks passed"
                )
                + allowed_note,
                details=policy.to_dict(),
            ),
        )
        report.policy = policy.to_dict()
        if policy.verdict == Verdict.BLOCK.value:
            report.verdict = Verdict.VETO.value
            return

        sable.install_runtime_guards(work_dir)
        _make_sources_readonly(isolated)
        isolation.read_only = True

        # 5. Nova execute
        packet = packet.next(
            stage=Stage.EXECUTE.value,
            from_role=Role.SABLE.value,
            to_role=Role.NOVA.value,
            payload={"command": command},
        )
        execution = nova.execute(
            isolated,
            work_dir=work_dir,
            allow_network=self.allow_network,
            timeout=self.timeout,
            config=self.config,
        )
        nova_failed = execution.exit_code != 0 or execution.failed > 0
        nova_verdict = (
            Verdict.VETO.value
            if execution.collected <= 0 or nova_failed
            else Verdict.CLEAR.value
        )
        append_result(
            self.role_results,
            RoleResult(
                role=Role.NOVA.value,
                stage=Stage.EXECUTE.value,
                verdict=nova_verdict,
                summary=(
                    f"collected={execution.collected} passed={execution.passed} "
                    f"failed={execution.failed} exit={execution.exit_code} "
                    f"duration={execution.duration_seconds}s install={execution.install_status} "
                    + (
                        "sandbox: unavailable (ran unsandboxed)"
                        if execution.sandbox_status == "unavailable"
                        else f"sandbox: {execution.sandbox_status} (ran unsandboxed)"
                        if str(execution.sandbox_status).startswith("unavailable")
                        else f"sandbox={execution.sandbox_status}"
                    )
                ),
                details={
                    "collected": execution.collected,
                    "passed": execution.passed,
                    "failed": execution.failed,
                    "skipped": execution.skipped,
                    "errors": execution.errors,
                    "exit_code": execution.exit_code,
                    "duration_seconds": execution.duration_seconds,
                    "command": execution.command,
                    "install_status": execution.install_status,
                    "sandbox_status": execution.sandbox_status,
                },
            ),
        )
        report.execution = execution.to_dict()
        report.sandbox = execution.sandbox_status

        post_policy = sable.evaluate_post_execution(
            collected=execution.collected, command=execution.command
        )
        append_result(
            self.role_results,
            RoleResult(
                role=Role.SABLE.value,
                stage=Stage.EXECUTE.value,
                verdict=post_policy.verdict,
                summary="; ".join(post_policy.blocked_reasons) or "collected > 0",
                details=post_policy.to_dict(),
            ),
        )
        report.policy = {
            "preflight": policy.to_dict(),
            "post": post_policy.to_dict(),
        }

        # 6. Riley + Lexi post-audit
        packet = packet.next(
            stage=Stage.POST_AUDIT.value,
            from_role=Role.NOVA.value,
            to_role=Role.RILEY.value,
            payload=execution.to_dict(),
        )
        result_review = riley.review_results(plan, execution.to_dict(), config=self.config, project_root=isolated)
        append_result(
            self.role_results,
            RoleResult(
                role=Role.RILEY.value,
                stage=Stage.POST_AUDIT.value,
                verdict=result_review.verdict,
                summary=result_review.notes,
                details=result_review.to_dict(),
                llm=result_review.llm_status,
            ),
        )
        post_drift = lexi.inspect(
            docs_root=docs,
            project_root=isolated,
            config=self.config,
            execution=execution.to_dict(),
        )
        append_result(
            self.role_results,
            RoleResult(
                role=Role.LEXI.value,
                stage=Stage.POST_AUDIT.value,
                verdict=post_drift.verdict,
                summary=post_drift.notes,
                details=post_drift.to_dict(),
                llm=post_drift.llm_status,
            ),
        )
        root_cause = result_review.root_cause
        if root_cause == "Nova collected zero tests, so results cannot witness the plan":
            specific_cause = riley.parse_failure_root_cause(
                str(execution.stdout or ""),
                str(execution.stderr or ""),
            )
            if specific_cause:
                root_cause = specific_cause

        report.post_audit = {
            "riley": result_review.to_dict(),
            "lexi": post_drift.to_dict(),
            "root_cause": root_cause,
        }

        # Veto hierarchy: Sable > Riley > Lexi (Sable applied first)
        if post_policy.verdict == Verdict.BLOCK.value:
            report.mutation = {"status": "skipped", "reason": "baseline tests did not pass"}
            report.verdict = Verdict.VETO.value
            return
        if nova_failed or result_review.verdict == Verdict.VETO.value:
            report.mutation = {"status": "skipped", "reason": "baseline tests did not pass"}
            report.verdict = Verdict.VETO.value
            return
        if post_drift.verdict == Verdict.VETO.value:
            report.mutation = {"status": "skipped", "reason": "baseline tests did not pass"}
            report.verdict = Verdict.VETO.value
            return

        mutation_cfg = dict(self.config.get("mutation") or {})
        mutation_enabled = bool(mutation_cfg.get("enabled", False))

        if not mutation_enabled:
            report.mutation = {"status": "skipped", "reason": "disabled"}
        else:
            py_bin = None
            venv_py = venv_python(work_dir)
            if venv_py.is_file():
                py_bin = venv_py

            mut_res = riley.audit_mutations(
                isolated,
                work_dir=work_dir,
                config=self.config,
                allow_network=self.allow_network,
                python=py_bin,
            )
            report.mutation = mut_res
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.RILEY.value,
                    stage="mutation_audit",
                    verdict=mut_res.get("verdict", Verdict.CLEAR.value),
                    summary=mut_res.get("reason", "mutation testing complete"),
                    details=mut_res,
                ),
            )
            if mut_res.get("verdict") == Verdict.VETO.value:
                report.verdict = Verdict.VETO.value
                return

        append_result(
            self.role_results,
            RoleResult(
                role=Role.VESPER.value,
                stage=Stage.CLOSE.value,
                verdict=Verdict.CLEAR.value,
                summary="cycle complete; no patch applied; live tree unchanged",
            ),
        )
        report.verdict = Verdict.CLEAR.value

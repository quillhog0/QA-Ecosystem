"""Vesper — orchestrator. Owns the loop, iteration caps, and escalation."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agents import aria, lexi, nova, riley
from core.isolation import (
    IsolationHandle,
    archive_note,
    create_isolation,
    isolation_destination,
    release_isolation,
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
from core.findings import write_findings
from core.report import append_result, write_reports
from core.runner import sanitized_env
from policies import sable


ESCALATE_DOMAIN = "ESCALATE: DOMAIN_EXPERT"
VALID_PROFILES = ("default", "factory")
VALID_FAIL_ON = ("P0", "P1", "P2", "P3")


def apply_profile(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and apply ``profile`` and ``fail_on`` defaults on ``config``."""
    profile = str(config.get("profile") or "default").strip().lower()
    if profile not in VALID_PROFILES:
        raise ValueError(f"invalid profile: {profile}")
    if profile == "factory":
        config.setdefault("fail_on", "P2")
        config.setdefault("identity_strict", not bool())
        mut = dict(config.get("mutation") or {})
        mut.setdefault("enabled", not bool())
        config["mutation"] = mut
    fail_on = str(config.get("fail_on") or "P1").strip().upper()
    if fail_on not in VALID_FAIL_ON:
        raise ValueError(f"invalid fail_on: {fail_on}")
    config["profile"] = profile
    config["fail_on"] = fail_on
    return config


def _relaxations_granted(policy: Any) -> bool:
    """True when the Sable pre-flight verdict was softened by the sandbox.

    Either dangerous operations were downgraded to WARN, or file deletions were
    auto-allowed because the sandbox confines them. Both are promises about the
    sandbox, so they may only be kept while the sandbox is really active.
    """
    if getattr(policy, "warnings", None):
        return True
    return any(
        str(item.get("reason") or "") == sable.SANDBOX_DELETE_REASON
        for item in (policy.allowed or [])
    )


class Vesper:
    def __init__(self, config: dict[str, Any]):
        self.config = apply_profile(config)
        self.max_stage_iterations = int(config.get("max_iterations") or 2)
        self.allow_network = bool(config.get("allow_network", False))
        self.dry_run = bool(config.get("dry_run", True))
        self.timeout = int((config.get("pytest") or {}).get("timeout_seconds") or 120)
        self.role_results: list[RoleResult] = []
        self._scope: Any = None
        self._diff_fallback: str = ""

    def _compute_diff_scope(self, project: Path, docs: Path) -> Any:
        """Return a DiffScope for ``--diff``, or ``None`` for a full run.

        A diff mode that cannot compute its scope (no git, bad ref, too many
        files) never silently skips: it falls back to a full run and remembers
        why, so the report can say so.
        """
        diff_cfg = self.config.get("diff") or {}
        base_ref = str(diff_cfg.get("base_ref") or "").strip()
        self._diff_fallback = ""
        if not base_ref:
            return None
        from core.diffscope import DiffScopeError, compute_diff_scope

        try:
            return compute_diff_scope(
                project.resolve(),
                base_ref,
                docs.resolve(),
                max_files=int(diff_cfg.get("max_files") or 200),
            )
        except DiffScopeError as exc:
            self._diff_fallback = (
                f"diff scope unavailable ({exc}); running the full cycle"
            )
            return None

    def run(self, project: Path, docs: Path, exceptions: list[Any] | None = None) -> FinalReport:
        from core.sandbox import get_sandbox_status, resolve_sandbox_mode

        # Validate operator config before any isolated copy exists, so an
        # invalid sandbox mode cannot leak a copy under the temp directory.
        resolve_sandbox_mode(self.config)
        self.exceptions = list(exceptions or [])
        from core.exceptions import load_qa_ignore
        qa_entries, qa_warns = load_qa_ignore(project)
        existing_matches = {getattr(e, "match", "") for e in self.exceptions}
        for qe in qa_entries:
            if qe.match not in existing_matches:
                self.exceptions.append(qe)
        self._scope = self._compute_diff_scope(project, docs)
        if self._scope is not None:
            self.config["_diff_changed"] = list(self._scope.changed)
        _run_started = time.monotonic()
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

        _isolation_started = time.monotonic()
        isolation = create_isolation(
            project,
            isolated_dest,
            method=str(self.config.get("isolation_method") or "auto"),
            read_only=False,
        )
        _isolation_seconds = time.monotonic() - _isolation_started
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
        report.timings = {"isolation": round(_isolation_seconds, 3)}
        for w in qa_warns:
            report.warnings.append(w)
        warn_skipped = [rec for rec in isolation.skipped if not rec["reason"].startswith("secret")]
        if warn_skipped:
            # A path the copy could not carry is never a silent drop: the
            # operator must see which files are missing from the isolated tree.
            report.warnings.append(
                "isolation skipped "
                + str(len(warn_skipped))
                + " path(s): "
                + "; ".join(f"{rec['path']} [{rec['reason']}]" for rec in warn_skipped)
            )

        blocked_reason = ""
        if self._scope is not None:
            report.diff = {"mode": "diff", **self._scope.to_dict()}
        elif self.config.get("diff") and (self.config.get("diff") or {}).get("base_ref"):
            report.diff = {
                "mode": "full",
                "reason": self._diff_fallback,
                "base_ref": str((self.config.get("diff") or {}).get("base_ref")),
            }
            report.warnings.append(self._diff_fallback)
        if self._scope is not None:
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.VESPER.value,
                    stage=Stage.INIT.value,
                    verdict=Verdict.CLEAR.value,
                    summary=(
                        f"diff mode vs {self._scope.base_ref}: "
                        f"{len(self._scope.changed)} changed file(s)"
                    ),
                    details=self._scope.to_dict(),
                ),
            )
        if failure_reason:
            # A required sandbox that cannot start is an environment stop, not an
            # accusation against the project. The cycle still runs Lexi, Aria,
            # Riley and the Sable pre-flight (strictly, because sandboxed=False),
            # so dangerous code is still VETOed; only execution is skipped.
            # Staying on the normal path also releases the isolated copy, which
            # the previous early return leaked.
            blocked_reason = f"sandbox required but bwrap unavailable: {failure_reason}"
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.VESPER.value,
                    stage=Stage.INIT.value,
                    verdict=Verdict.BLOCKED_ENV.value,
                    summary=blocked_reason,
                    details={"sandbox": sandbox_status, "error": failure_reason},
                ),
            )

        try:
            try:
                self._loop(packet, isolation, work_dir, report, blocked_reason=blocked_reason)
            finally:
                report.timings["total"] = round(time.monotonic() - _run_started, 3)
                report.role_results = list(self.role_results)
                report.finished_at = utc_now()
                fail_on = str(self.config.get("fail_on") or "P1").upper()
                write_brief = bool(self.config.get("fix_brief", not bool()))
                baseline_fps = self.config.get("baseline_fingerprints")
                if any((baseline_fps, fail_on != "P1")):
                    from core.findings import build_document, build_findings

                    raw_f = build_findings(report.to_dict())
                    doc_after = build_document(
                        report.to_dict(),
                        exceptions=getattr(self, "exceptions", None),
                        baseline=baseline_fps,
                        fail_on=fail_on,
                    )
                    rem_veto = [
                        f
                        for f in (doc_after.get("findings") or [])
                        if f.get("verdict_effect") == "VETO"
                    ]
                    exec_collected = int((report.execution or {}).get("collected") or 0)
                    if rem_veto:
                        report.verdict = Verdict.VETO.value
                    elif all((raw_f, not rem_veto, bool(exec_collected))):
                        report.verdict = Verdict.CLEAR.value
                write_reports(report, base)
                findings_base = Path(self.config.get("findings_dir") or base).resolve()
                write_findings(
                    report,
                    findings_base,
                    exceptions=getattr(self, "exceptions", None),
                    baseline=baseline_fps,
                    fail_on=fail_on,
                    write_brief=write_brief,
                    feedback_export=bool(self.config.get("feedback_export", bool())),
                )
        finally:
            release_isolation(isolation, keep=bool(self.config.get("keep_isolated_copy", False)))
        return report

    def _loop(
        self,
        packet: HandoffPacket,
        isolation: IsolationHandle,
        work_dir: Path,
        report: FinalReport,
        blocked_reason: str = "",
    ) -> None:
        isolated = isolation.isolated_root
        docs = Path(packet.docs_root)

        # Diff mode threads a per-role scope through every collector; a full run
        # leaves all of these None so behaviour is byte-for-byte unchanged.
        scope = getattr(self, "_scope", None)
        docs_allowlist = set(scope.lexi_docs_allowlist) if scope is not None else None
        changed_code = set(scope.changed_code) if scope is not None else None
        only_paths = set(scope.riley_sable_paths) if scope is not None else None
        nova_targets = list(scope.nova_test_targets) if scope is not None else None

        # 1. Lexi drift check
        drift = None
        drift_veto = False
        _lexi_started = time.monotonic()
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
                live_root=isolation.live_root,
                docs_allowlist=docs_allowlist,
                changed_code=changed_code,
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
            # A drift veto is an accusation, not a reason to stop looking. Lexi
            # cannot patch documentation in this loop, so it vetoes once and the
            # pipeline keeps going: the operator still gets Riley, Sable and Nova,
            # and the report shows what the tests actually did. The veto itself is
            # applied in the final hierarchy below, where it outranks an
            # environmental stop and a clean run.
            drift_veto = True
            break

        report.timings["lexi"] = round(time.monotonic() - _lexi_started, 3)
        assert drift is not None

        # 2–3. Aria design with Riley review (max iterations)
        plan = None
        review = None
        gaps: list[str] = []
        _design_started = time.monotonic()
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
            review = riley.review_plan(plan, config=self.config, project_root=isolated, only_paths=only_paths)
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
                if drift_veto:
                    # The answer is already a veto from the drift stage; an
                    # escalation would replace it with a different one. Keep the
                    # accusation intact and let execution still be reported.
                    break
                report.verdict = Verdict.ESCALATE.value
                report.escalation = ESCALATE_DOMAIN
                return

        report.timings["design"] = round(time.monotonic() - _design_started, 3)
        assert plan is not None and review is not None

        # 4. Sable pre-flight
        _preflight_started = time.monotonic()
        command = sable.default_command(isolated, work_dir, test_targets=nova_targets)
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
            only_paths=only_paths,
        )
        report.timings["preflight"] = round(time.monotonic() - _preflight_started, 3)
        allowed_note = ""
        if policy.allowed:
            allowed_note = "; ALLOWED: " + "; ".join(
                f"{item.get('path')} {item.get('call')} ({item.get('reason')})"
                for item in policy.allowed
            )
        warn_note = ""
        if policy.warnings:
            warn_note = (
                f"; WARN ({len(policy.warnings)} dangerous ops allowed under sandbox): "
                + "; ".join(policy.warnings[:8])
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
                + allowed_note
                + warn_note,
                details=policy.to_dict(),
            ),
        )
        report.policy = policy.to_dict()
        if policy.verdict == Verdict.BLOCK.value:
            report.verdict = Verdict.VETO.value
            return

        # TOCTOU guard: pre-flight was evaluated against the sandbox status
        # known at init, and the runner probes again when it builds the command.
        # If the sandbox disappeared in between, the relaxed rules must not be
        # executed unsandboxed - re-evaluate strictly by refusing the run.
        if _relaxations_granted(policy):
            from core.sandbox import get_sandbox_status

            sandbox_ready, sandbox_label, sandbox_error = get_sandbox_status(self.config)
            if not sandbox_ready or sandbox_label != "bwrap":
                append_result(
                    self.role_results,
                    RoleResult(
                        role=Role.SABLE.value,
                        stage=Stage.PREFLIGHT.value,
                        verdict=Verdict.BLOCK.value,
                        summary=(
                            "sandbox is no longer active before execution ("
                            f"{sandbox_error or sandbox_label}) - refusing to run "
                            f"{len(policy.warnings)} sandbox-relaxed dangerous "
                            "operations unsandboxed"
                        ),
                        details={
                            "sandbox": sandbox_label,
                            "warnings": list(policy.warnings[:8]),
                        },
                    ),
                )
                report.sandbox = sandbox_label
                report.verdict = Verdict.VETO.value
                return

        node_engine_blocked = bool()
        from core.toolchain import check_node_engine, detect_languages, ensure_ts_tooling
        from core.workspace import check_workspace_engines, discover_workspaces

        workspaces = discover_workspaces(isolated)
        if not blocked_reason and "ts" in detect_languages(isolated):
            node_ok, node_msg = check_node_engine(isolated)
            if not node_ok:
                blocked_reason = f"node engine precondition: {node_msg}"
                node_engine_blocked = not bool()
            else:
                ws_ok, ws_msg = check_workspace_engines(isolated, workspaces)
                if not ws_ok:
                    blocked_reason = f"node engine precondition: {ws_msg}"
                    node_engine_blocked = not bool()
                elif not ensure_ts_tooling():
                    blocked_reason = "missing TypeScript tooling (pinned typescript.js not found in QA cache)"
                    node_engine_blocked = not bool()

        if blocked_reason:
            # Nothing may execute without the required sandbox. Every stage above
            # already ran, so a dangerous project has been VETOed by now and an
            # environment stop is only reported for a project that passed them.
            # A drift veto held from stage 1 still outranks the environment stop:
            # the accusation was already made, and losing it here would make the
            # hierarchy depend on whether a sandbox happened to be available.
            report.post_audit = {"root_cause": blocked_reason}
            report.mutation = {
                "status": "skipped",
                "reason": (
                    "drift veto: docs and code disagree"
                    if (drift_veto and not node_engine_blocked)
                    else "environment blocked before execution"
                ),
            }
            report.verdict = (
                Verdict.VETO.value if (drift_veto and not node_engine_blocked) else Verdict.BLOCKED_ENV.value
            )
            return

        sable.install_runtime_guards(work_dir)
        # The isolated copy is a standalone checkout that the tested process may
        # write into (output directories, a local sqlite database, generated
        # fixtures), exactly like a normal working tree. We deliberately do NOT
        # strip its write bits: a read-only copy turns legitimate in-tree writes
        # into PermissionError and mislabels them as product defects. The live
        # tree is still never written - it is a different directory, mounted
        # read-only (or not mounted at all) inside the sandbox, so the isolation
        # guarantee is preserved by structure, not by hardening the copy against
        # its own tests.

        # 5. Nova execute
        packet = packet.next(
            stage=Stage.EXECUTE.value,
            from_role=Role.SABLE.value,
            to_role=Role.NOVA.value,
            payload={"command": command},
        )
        _execute_started = time.monotonic()
        execution = nova.execute(
            isolated,
            work_dir=work_dir,
            allow_network=self.allow_network,
            timeout=self.timeout,
            config=self.config,
            test_targets=nova_targets,
        )
        _execute_seconds = time.monotonic() - _execute_started
        report.timings["execute"] = round(_execute_seconds, 3)
        report.timings["pytest"] = round(execution.duration_seconds, 3)
        # install_deps time is the part of the execute window not spent running
        # pytest; it is 0 when dependency installation is disabled.
        report.timings["install"] = round(
            max(_execute_seconds - execution.duration_seconds, 0.0), 3
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
                        f"flaky={execution.flaky} status={execution.status} "
                        if execution.flaky
                        else ""
                    )
                    + (
                        f"product_failed={execution.product_failed} "
                        f"environment_failed={execution.environment_failed} "
                        if execution.failures
                        else ""
                    )
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
                    "product_failed": execution.product_failed,
                    "environment_failed": execution.environment_failed,
                    "flaky": execution.flaky,
                    "flaky_tests": execution.flaky_tests,
                    "status": execution.status,
                    "failures": execution.to_dict().get("failures") or [],
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
        result_review = riley.review_results(plan, execution.to_dict(), config=self.config, project_root=isolated, only_paths=only_paths)
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
            live_root=isolation.live_root,
            docs_allowlist=docs_allowlist,
            changed_code=changed_code,
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

        # Check identity leaks
        from core.identity import scan_identity_leaks
        _ident_started = time.monotonic()
        diff_only_paths = set(self._scope.changed) if self._scope is not None else None
        identity_leaks = scan_identity_leaks(isolated, self.config, only_paths=diff_only_paths)
        report.timings["identity_scan"] = round(time.monotonic() - _ident_started, 3)

        live_project = Path(packet.project_root)

        # Check deleted and weakened tests in diff mode
        deleted_tests: list[str] = []
        weakened_tests: list[str] = []
        if all((self._scope is not None, getattr(self._scope, "base_ref", None))):
            from core.diffscope import detect_deleted_tests, detect_weakened_tests

            deleted_tests = detect_deleted_tests(live_project, self._scope.base_ref)
            weakened_tests = detect_weakened_tests(live_project, self._scope.base_ref)

        # Check clean install if requested
        clean_install_res: dict[str, Any] | None = None
        if self.config.get("clean_install", bool()):
            from core.clean_install import run_clean_install
            clean_install_res = run_clean_install(live_project, work_dir)

        report.post_audit = {
            "riley": result_review.to_dict(),
            "lexi": post_drift.to_dict(),
            "root_cause": root_cause,
            "identity_leaks": identity_leaks,
            "deleted_tests": deleted_tests,
            "weakened_tests": weakened_tests,
            "clean_install": clean_install_res,
            "workspaces": workspaces,
        }

        p1_leaks = [leak for leak in identity_leaks if leak.get("priority") == "P1"]
        if p1_leaks:
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.SABLE.value,
                    stage="identity_audit",
                    verdict=Verdict.BLOCK.value,
                    summary=f"{len(p1_leaks)} critical secret leak(s) detected in shipped tree",
                    details={"leaks": identity_leaks},
                ),
            )
            report.mutation = {"status": "skipped", "reason": "identity leak veto"}
            report.verdict = Verdict.VETO.value
            return
        elif identity_leaks:
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.SABLE.value,
                    stage="identity_audit",
                    verdict=Verdict.CLEAR.value,
                    summary=f"{len(identity_leaks)} identity warning(s) (P2) detected in shipped tree",
                    details={"leaks": identity_leaks},
                ),
            )

        if any((deleted_tests, weakened_tests)):
            reason_label = "deleted tests in diff mode" if deleted_tests else "weakened tests in diff mode"
            summary_label = (
                f"{len(deleted_tests)} test(s) deleted in diff mode"
                if deleted_tests
                else f"{len(weakened_tests)} test(s) weakened in diff mode"
            )
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.RILEY.value,
                    stage="diff_tests_audit",
                    verdict=Verdict.VETO.value,
                    summary=summary_label,
                    details={"deleted_tests": deleted_tests, "weakened_tests": weakened_tests},
                ),
            )
            report.mutation = {"status": "skipped", "reason": reason_label}
            report.verdict = Verdict.VETO.value
            return

        if clean_install_res and clean_install_res.get("failed"):
            append_result(
                self.role_results,
                RoleResult(
                    role=Role.NOVA.value,
                    stage="clean_install_audit",
                    verdict=Verdict.VETO.value,
                    summary="README installation instructions failed in clean environment",
                    details=clean_install_res,
                ),
            )
            report.mutation = {"status": "skipped", "reason": "clean install failure"}
            report.verdict = Verdict.VETO.value
            return

        # Veto hierarchy: Sable > Riley > Lexi (Sable applied first). An
        # environmental stop may only soften the technical blocks that say "the
        # tests did not run"; it never softens a real accusation, and Riley
        # returns BLOCKED_ENV only when no test failed.
        env_blocked = result_review.verdict == Verdict.BLOCKED_ENV.value
        if post_policy.verdict == Verdict.BLOCK.value:
            # Sable's post-execution block only says the baseline did not pass, so
            # an environmental cause can soften it - except a command that is not
            # pytest-only, which is a policy violation on its own.
            blocked_by_env = env_blocked and post_policy.checks.get("pytest_only", False)
            report.mutation = {
                "status": "skipped",
                "reason": (
                    "environment blocked: the tests could not run"
                    if blocked_by_env
                    else "baseline tests did not pass"
                ),
            }
            report.verdict = (
                Verdict.BLOCKED_ENV.value if blocked_by_env else Verdict.VETO.value
            )
            return
        if (nova_failed and not env_blocked) or result_review.verdict == Verdict.VETO.value:
            report.mutation = {"status": "skipped", "reason": "baseline tests did not pass"}
            report.verdict = Verdict.VETO.value
            return
        if post_drift.verdict == Verdict.VETO.value:
            report.mutation = {"status": "skipped", "reason": "baseline tests did not pass"}
            report.verdict = Verdict.VETO.value
            return
        if drift_veto:
            # The stage-1 drift veto: same rank as the post-audit one, applied
            # after Sable and Riley so a stronger accusation is never hidden, and
            # before the environmental softening so a veto is never downgraded.
            report.mutation = {
                "status": "skipped",
                "reason": "drift veto: docs and code disagree",
            }
            report.verdict = Verdict.VETO.value
            return
        if env_blocked:
            report.mutation = {
                "status": "skipped",
                "reason": "environment blocked: the tests could not run",
            }
            report.verdict = Verdict.BLOCKED_ENV.value
            return

        mutation_cfg = dict(self.config.get("mutation") or {})
        mutation_enabled = bool(mutation_cfg.get("enabled", bool()))

        if not mutation_enabled:
            skip_reason = "diff mode" if self._scope is not None else "disabled"
            report.mutation = {"status": "skipped", "reason": skip_reason}
        else:
            run_cfg = dict(self.config)
            if self._scope is not None:
                run_cfg["mutation"] = {
                    **mutation_cfg,
                    "allowed_lines": dict(getattr(self._scope, "changed_lines", {})),
                }
            py_bin = None
            venv_py = venv_python(work_dir)
            if venv_py.is_file():
                py_bin = venv_py

            _mutation_started = time.monotonic()
            mut_res = riley.audit_mutations(
                isolated,
                work_dir=work_dir,
                config=run_cfg,
                allow_network=self.allow_network,
                python=py_bin,
            )
            if self._scope is not None:
                mut_res["diff_scoped"] = not bool()
            report.timings["mutation"] = round(time.monotonic() - _mutation_started, 3)
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

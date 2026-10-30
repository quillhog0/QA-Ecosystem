"""Write Markdown + YAML reports. Archive logs only; never delete the live tree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from core.packets import FinalReport, RoleResult, Verdict


def render_markdown(report: FinalReport) -> str:
    lines = [
        f"# QA run `{report.run_id}`",
        "",
        f"- **verdict:** {report.verdict}",
        f"- **dry_run:** {report.dry_run}",
        f"- **sandbox:** {report.sandbox}",
        f"- **project:** `{report.project_root}`",
        f"- **docs:** `{report.docs_root}`",
        f"- **isolated copy:** `{report.isolated_root}`",
        f"- **started:** {report.started_at}",
        f"- **finished:** {report.finished_at}",
    ]
    if report.sandbox == "unavailable":
        lines.append("- **warning:** sandbox: unavailable (ran unsandboxed)")
    elif str(report.sandbox).startswith("unavailable"):
        lines.append(f"- **warning:** sandbox: {report.sandbox} (ran unsandboxed)")
    if report.tested_tree:
        lines.append(f"- **tested_tree:** {report.tested_tree}")
    if report.escalation:
        lines.append(f"- **escalation:** `{report.escalation}`")
    lines += ["", "## Role results", ""]
    for item in report.role_results:
        lines.append(f"### {item.role} / {item.stage} (iter {item.iteration})")
        lines.append(f"- verdict: **{item.verdict}**")
        lines.append(f"- llm: {item.llm}")
        lines.append(f"- {item.summary}")
        lines.append("")
    lines += ["## Drift (Lexi)", "", "```yaml", _yaml(report.drift), "```", ""]
    lines += ["## Plan (Aria)", "", "```yaml", _yaml(report.plan), "```", ""]
    claim_scenarios = [
        s for s in (report.plan.get("scenarios") or [])
        if str(s.get("id") or "").startswith("C")
    ]
    if claim_scenarios:
        lines += ["## Claim coverage", "", "| claim | covered_by | score |", "|---|---|---|"]
        for s in claim_scenarios:
            title = str(s.get("title") or "").replace("|", "\\|")
            covered = s.get("covered_by") or []
            covered_str = ", ".join(covered) if covered else "NONE"
            score = s.get("score") if s.get("score") is not None else 0
            lines.append(f"| {title} | {covered_str} | {score} |")
        lines.append("")
    lines += ["## Policy (Sable)", "", "```yaml", _yaml(report.policy), "```", ""]
    exec_view = dict(report.execution)
    if "stdout" in exec_view:
        exec_view["stdout"] = _short(str(exec_view.get("stdout") or ""), 4000)
    if "stderr" in exec_view:
        exec_view["stderr"] = _short(str(exec_view.get("stderr") or ""), 2000)
    lines += ["## Execution (Nova)", "", "```yaml", _yaml(exec_view), "```", ""]
    lines += ["## Post-audit", "", "```yaml", _yaml(report.post_audit), "```", ""]
    if report.mutation:
        lines += ["## Mutation testing", ""]
        status = report.mutation.get("status", "")
        if status == "skipped":
            lines.append(f"- **status:** skipped ({report.mutation.get('reason', 'disabled')})")
        elif status == "completed":
            score = float(report.mutation.get("score", 0.0))
            killed = report.mutation.get("killed", 0)
            survived = report.mutation.get("survived", 0)
            timeout = report.mutation.get("timeout", 0)
            error = report.mutation.get("error", 0)
            min_score = report.mutation.get("min_score", 0.0)
            lines.append("- **status:** completed")
            lines.append(f"- **score:** {score:.1%} ({killed} killed, {survived} survived, {timeout} timeout, {error} error)")
            lines.append(f"- **min_score:** {min_score}")
            lines.append("")
            survived_list = report.mutation.get("survived_mutants") or []
            if survived_list:
                lines += ["### Surviving mutants", ""]
                for s in survived_list:
                    loc = s.get("location", "")
                    orig = str(s.get("original", "")).replace("`", "")
                    mut = str(s.get("mutated", "")).replace("`", "")
                    lines.append(f"- `{loc}`: `{orig}` → `{mut}`")
            else:
                lines.append("No mutants survived.")
        lines.append("")
    if report.timings:
        lines += ["## Phase timings (seconds)", ""]
        for name in ("isolation", "install", "pytest", "execute", "mutation"):
            if name in report.timings:
                lines.append(f"- {name}: {report.timings[name]:.3f}")
        lines.append("")
    lines += [
        "## Bounds",
        "",
        "- This run does not patch code, does not target production, and does not claim coverage.",
        "- Secret-looking files were excluded from the isolated copy.",
        "",
    ]
    return "\n".join(lines)


def write_reports(report: FinalReport, reports_dir: Path) -> tuple[Path, Path]:
    reports_dir.mkdir(parents=True, exist_ok=True)
    md_path = reports_dir / f"{report.run_id}.md"
    yml_path = reports_dir / f"{report.run_id}.yaml"
    md_path.write_text(render_markdown(report), encoding="utf-8")
    yml_path.write_text(_yaml(report.to_dict()), encoding="utf-8")
    return md_path, yml_path


def exit_code_for(verdict: str) -> int:
    return 0 if verdict == Verdict.CLEAR.value else 1


def _yaml(data: Any) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()


def _short(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def append_result(results: list[RoleResult], result: RoleResult) -> RoleResult:
    results.append(result)
    return result

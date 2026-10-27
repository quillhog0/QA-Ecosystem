"""Riley — red team. Technical VETO with required root cause."""

from __future__ import annotations

import re
from typing import Any

from agents.adapters import complete, load_prompt, parse_yaml_block
from core.packets import RedTeamReview, TestPlan, Verdict

FAILED_LINE_RE = re.compile(
    r"^FAILED\s+(?P<node>.+?)(?:\s+-\s+(?P<assertion>.*))?$",
    re.MULTILINE,
)


def review_plan(plan: TestPlan, *, config: dict[str, Any]) -> RedTeamReview:
    gaps: list[str] = []
    if not plan.scenarios:
        gaps.append("plan has zero scenarios")
    untitled = [s.id for s in plan.scenarios if not s.title.strip()]
    if untitled:
        gaps.append(f"scenarios missing titles: {untitled}")
    no_accept = [s.id for s in plan.scenarios if not s.acceptance]
    if no_accept:
        gaps.append(f"scenarios missing acceptance criteria: {no_accept}")
    if not any(s.negative for s in plan.scenarios):
        gaps.append("no negative-path scenario")
    if any("network" in " ".join(s.steps).lower() for s in plan.scenarios):
        gaps.append("plan steps appear to require network")

    veto = bool(gaps) and (
        not plan.scenarios
        or no_accept
        or not any(s.negative for s in plan.scenarios)
    )
    if veto:
        root = "; ".join(gaps)
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=_changes_from_gaps(gaps),
                root_cause=root,
                notes="plan is not trustworthy until gaps are closed",
            ),
            {"phase": "plan", "plan": plan.to_dict()},
            config,
        )
    return _maybe_llm(
        RedTeamReview(
            verdict=Verdict.CLEAR.value,
            gaps=gaps,
            required_changes=[],
            root_cause="",
            notes="plan has scenarios, acceptance criteria, and a negative path",
        ),
        {"phase": "plan", "plan": plan.to_dict()},
        config,
    )


def parse_failure_root_cause(stdout: str, stderr: str = "") -> str:
    """Build a root cause naming the failing node and assertion."""
    text = f"{stdout}\n{stderr}"
    parts: list[str] = []
    for match in FAILED_LINE_RE.finditer(text):
        node = (match.group("node") or "").strip()
        assertion = (match.group("assertion") or "").strip()
        name = node.split("::")[-1] if node else "unknown test"
        if assertion:
            parts.append(f"{name} ({node}): {assertion}")
        else:
            parts.append(f"{name} ({node})")
    if parts:
        return "pytest failures: " + "; ".join(parts)

    if "ModuleNotFoundError: No module named" in text or "ImportError: No module named" in text:
        match = re.search(r"No module named '([a-zA-Z0-9_.]+)'", text)
        module_name = match.group(1) if match else "unknown"
        return f"missing dependency: {module_name} (not installable or not declared in requirements)"

    return ""


def review_results(
    plan: TestPlan,
    execution: dict[str, Any],
    *,
    config: dict[str, Any],
) -> RedTeamReview:
    gaps: list[str] = []
    collected = int(execution.get("collected") or 0)
    failed = int(execution.get("failed") or 0)
    exit_code = int(execution.get("exit_code") if execution.get("exit_code") is not None else -1)
    errors = int(execution.get("errors") or 0)
    install_status = str(execution.get("install_status") or "skipped")

    if collected <= 0:
        gaps.append("collected tests == 0")
    if exit_code != 0:
        gaps.append(f"pytest exit code {exit_code}")
    if failed > 0:
        gaps.append(f"failed={failed}")
    if errors:
        gaps.append(f"pytest collection/execution errors={errors}")
    if not plan.scenarios:
        gaps.append("no plan to compare against results")

    failure_cause = parse_failure_root_cause(
        str(execution.get("stdout") or ""),
        str(execution.get("stderr") or ""),
    )

    if collected <= 0:
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=[],
                root_cause="Nova collected zero tests, so results cannot witness the plan",
                notes="empty collection is a technical veto",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )
    if errors:
        root = failure_cause or (
            "pytest reported collection or runtime errors; results are not a clean verdict"
        )
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=[],
                root_cause=root,
                notes="errors must be explained before CLEAR",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )
    if install_status.startswith("pip install failed"):
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=[],
                root_cause=f"dependency installation failed: {install_status}",
                notes="dependency installation failed",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )
    if exit_code != 0 or failed > 0:
        root = failure_cause or (
            f"pytest exit {exit_code}, failed={failed}; assertion details were not parsed"
        )
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=[],
                root_cause=root,
                notes="failing tests are a technical veto",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )
    return _maybe_llm(
        RedTeamReview(
            verdict=Verdict.CLEAR.value,
            gaps=gaps,
            required_changes=[],
            root_cause="",
            notes="results can be interpreted against the plan",
        ),
        {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
        config,
    )


def _changes_from_gaps(gaps: list[str]) -> list[str]:
    changes = []
    for gap in gaps:
        if "negative" in gap:
            changes.append("add one negative-path scenario with acceptance criteria")
        elif "zero scenarios" in gap:
            changes.append("add at least one scenario mapped to pytest")
        elif "acceptance" in gap:
            changes.append("add measurable acceptance criteria to every scenario")
        else:
            changes.append(f"resolve: {gap}")
    return changes


def _small_exec(execution: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "collected",
        "passed",
        "failed",
        "skipped",
        "errors",
        "exit_code",
        "duration_seconds",
        "command",
    )
    return {k: execution.get(k) for k in keys}


def _maybe_llm(
    review: RedTeamReview, payload: dict[str, Any], config: dict[str, Any]
) -> RedTeamReview:
    try:
        raw = complete(
            system_prompt=load_prompt("riley.md"),
            user_payload={**payload, "heuristic": review.to_dict()},
            config=config,
        )
    except Exception:
        return review
    if not raw:
        return review
    parsed = parse_yaml_block(raw)
    if not parsed:
        return review
    verdict = str(parsed.get("verdict") or review.verdict).upper()
    if verdict not in {Verdict.CLEAR.value, Verdict.VETO.value}:
        verdict = review.verdict
    root = str(parsed.get("root_cause") or "")
    if verdict == Verdict.VETO.value and not root:
        root = review.root_cause or "unspecified technical defect"
    return RedTeamReview(
        verdict=verdict,
        gaps=[str(g) for g in (parsed.get("gaps") or review.gaps)],
        required_changes=[str(c) for c in (parsed.get("required_changes") or review.required_changes)],
        root_cause=root if verdict == Verdict.VETO.value else "",
        notes=str(parsed.get("notes") or review.notes),
    )

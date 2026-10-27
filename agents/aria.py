"""Aria — designer. Scenarios and acceptance criteria from docs + inventory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.adapters import complete, load_prompt, parse_yaml_block
from agents.inventory import doc_tokens, list_python, public_names, split_code_and_tests
from core.packets import Scenario, TestPlan


def _heuristic_plan(
    *,
    docs_root: Path,
    project_root: Path,
    gaps: list[str],
) -> TestPlan:
    py_files = list_python(project_root)
    code_files, test_files = split_code_and_tests(py_files, project_root)
    tokens, excerpt = doc_tokens(docs_root)
    scenarios: list[Scenario] = []

    publics: list[str] = []
    for path in code_files:
        publics.extend(public_names(path))

    if test_files:
        scenarios.append(
            Scenario(
                id="S1",
                title="Existing pytest suite completes",
                steps=[
                    "Run the isolated pytest suite against the project copy",
                    "Record collected count, exit code, and failures",
                ],
                acceptance=[
                    "pytest collects more than 0 tests",
                    "exit code is 0 when no assertion failures are expected by docs",
                ],
                negative=False,
            )
        )
    if publics:
        names = ", ".join(publics[:8])
        scenarios.append(
            Scenario(
                id="S2",
                title="Public functions remain callable via tests",
                steps=[
                    f"Identify public names: {names}",
                    "Confirm tests import or call at least one public name",
                ],
                acceptance=[
                    "At least one existing test references a public name from source",
                ],
                negative=False,
            )
        )

    error_talk = any(
        word in excerpt.lower()
        for word in ("error", "invalid", "reject", "raise", "must not")
    )
    if error_talk or any("negative" in g.lower() for g in gaps):
        scenarios.append(
            Scenario(
                id="S3",
                title="Invalid input is rejected",
                steps=[
                    "Exercise documented invalid or empty input through existing tests",
                    "Observe an error, False return, or failed assertion path",
                ],
                acceptance=[
                    "A test covers a failure or validation path (negative case)",
                ],
                negative=True,
            )
        )
    elif test_files:
        scenarios.append(
            Scenario(
                id="S3",
                title="Suite reports failures instead of silent collection errors",
                steps=[
                    "If a test fails, pytest must report failed > 0 rather than hiding the error",
                ],
                acceptance=[
                    "errors count is 0 unless import of the project under test is broken",
                ],
                negative=True,
            )
        )

    if not scenarios:
        scenarios.append(
            Scenario(
                id="S1",
                title="Establish a runnable pytest surface",
                steps=["Discover tests in the isolated copy"],
                acceptance=["collected > 0"],
                negative=False,
            )
        )

    notes = "Scenarios map to the existing pytest surface; no new files are required."
    if tokens:
        notes += " Docs headings considered: " + ", ".join(tokens[:8]) + "."
    assumptions = [
        "Default language is Python + pytest",
        "Execution happens only in the isolated copy",
        "This plan does not claim coverage percentage",
    ]
    if gaps:
        assumptions.append("Previous red-team gaps were considered: " + "; ".join(gaps[:6]))
    return TestPlan(scenarios=scenarios, coverage_notes=notes, assumptions=assumptions)


def _from_llm(data: dict[str, Any], fallback: TestPlan) -> TestPlan:
    scenarios: list[Scenario] = []
    for idx, item in enumerate(data.get("scenarios") or [], start=1):
        if not isinstance(item, dict):
            continue
        scenarios.append(
            Scenario(
                id=str(item.get("id") or f"S{idx}"),
                title=str(item.get("title") or f"Scenario {idx}"),
                steps=[str(s) for s in (item.get("steps") or [])],
                acceptance=[str(s) for s in (item.get("acceptance") or [])],
                negative=bool(item.get("negative")),
            )
        )
    if not scenarios:
        scenarios = fallback.scenarios
    return TestPlan(
        scenarios=scenarios,
        coverage_notes=str(data.get("coverage_notes") or fallback.coverage_notes),
        assumptions=[str(a) for a in (data.get("assumptions") or fallback.assumptions)],
    )


def design(
    *,
    docs_root: Path,
    project_root: Path,
    config: dict[str, Any],
    gaps: list[str] | None = None,
) -> TestPlan:
    gaps = gaps or []
    plan = _heuristic_plan(docs_root=docs_root, project_root=project_root, gaps=gaps)
    payload = {"heuristic_plan": plan.to_dict(), "red_team_gaps": gaps}
    try:
        raw = complete(
            system_prompt=load_prompt("aria.md"),
            user_payload=payload,
            config=config,
        )
    except Exception:
        return plan
    if not raw:
        return plan
    parsed = parse_yaml_block(raw)
    if not parsed:
        return plan
    return _from_llm(parsed, plan)

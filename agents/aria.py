"""Aria — designer. Scenarios and acceptance criteria from docs + inventory."""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re
from typing import Any

from agents.adapters import complete_with_status, format_llm_error, load_prompt, parse_yaml_block
from agents.inventory import doc_tokens, list_python, list_sources, public_names, split_code_and_tests
from core.packets import Scenario, TestPlan


_DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}


def locate_claim_in_docs(docs_root: Path, claim: str) -> tuple[str | None, int | None]:
    """Return ``(relative_path, line)`` of the first doc line that carries *claim*."""
    needle = (claim or "").strip()
    if not needle or not docs_root.exists():
        return None, None
    stripped = needle.lstrip("-* ").strip()
    paths: list[Path] = []
    if docs_root.is_file():
        paths = [docs_root]
        root = docs_root.parent
    else:
        root = docs_root
        paths = sorted(
            path for path in docs_root.rglob("*")
            if path.is_file() and path.suffix.lower() in _DOC_SUFFIXES
        )
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for index, line in enumerate(lines, start=1):
            if needle in line or (stripped and stripped in line):
                try:
                    rel = str(path.relative_to(root)).replace("\\", "/")
                except ValueError:
                    rel = path.name
                return rel, index
    return None, None


STOP_WORDS = {
    "the", "and", "for", "all", "not", "are", "can", "any", "has", "had",
    "per", "out", "its", "our", "did", "was", "may", "use", "let", "set",
    "this", "that", "with", "from", "have", "been", "only", "also", "into",
    "more", "some", "such", "than", "them", "then", "there", "these", "they",
    "will", "would", "about", "above", "after", "again", "against", "cannot",
    "does", "doing", "down", "during", "each", "further", "here", "itself",
    "just", "most", "other", "over", "same", "should", "very", "what", "when",
    "where", "which", "while", "true", "claim", "verify", "tento", "tato", "toto",
    "jsou", "bude", "nebo", "jako", "pres", "podle"
}


def _stems(word: str) -> set[str]:
    stems = {word}
    if word.endswith("ies") and len(word) > 4:
        stems.add(word[:-3] + "y")
    if word.endswith("es") and len(word) > 3:
        stems.add(word[:-1])
        stems.add(word[:-2])
    if word.endswith("s") and len(word) > 3:
        stems.add(word[:-1])
    return stems


def _extract_claim_keywords(claim: str) -> list[str]:
    tokens = []
    raw = re.findall(r"[A-Za-z0-9_]+", claim)
    for w in raw:
        w_low = w.lower()
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z][a-z0-9]|\b)", w)
        if len(parts) > 1:
            for p in parts:
                p_low = p.lower()
                if len(p_low) >= 3 and p_low not in STOP_WORDS:
                    tokens.append(p_low)
        if "_" in w_low:
            for p in w_low.split("_"):
                if len(p) >= 3 and p not in STOP_WORDS:
                    tokens.append(p)
        if len(w_low) >= 3 and w_low not in STOP_WORDS:
            tokens.append(w_low)
    return list(dict.fromkeys(tokens))


def _extract_body_tokens(body: str) -> set[str]:
    res = set()
    raw = re.findall(r"[A-Za-z0-9_]+", body)
    for w in raw:
        w_low = w.lower()
        parts = re.findall(r"[A-Z]?[a-z0-9]+|[A-Z]+(?=[A-Z][a-z0-9]|\b)", w)
        if len(parts) > 1:
            for p in parts:
                p_low = p.lower()
                if len(p_low) >= 3 and p_low not in STOP_WORDS:
                    res.update(_stems(p_low))
        if "_" in w_low:
            for p in w_low.split("_"):
                if len(p) >= 3 and p not in STOP_WORDS:
                    res.update(_stems(p))
        if len(w_low) >= 3 and w_low not in STOP_WORDS:
            res.update(_stems(w_low))
    return res


def _compute_common_body_words(tests: list[tuple[str, str, str]]) -> set[str]:
    N = len(tests)
    if N < 2:
        return set()
    word_counts: Counter[str] = Counter()
    for _, _, body in tests:
        tokens = _extract_body_tokens(body)
        for t in tokens:
            word_counts[t] += 1
    return {w for w, c in word_counts.items() if c >= 0.8 * N}


def _collect_test_functions(test_files: list[Path], project_root: Path) -> list[tuple[str, str, str]]:
    """Return list of (test_id, func_name, body_text)."""
    tests = []
    for path in test_files:
        try:
            rel = str(path.relative_to(project_root)).replace("\\", "/")
        except ValueError:
            rel = path.name
        suffix = path.suffix.lower()
        if suffix in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
                lines = text.splitlines()
            except OSError:
                continue
            test_decl_re = re.compile(
                r"""(?:^|\s)(?:it|test)(?:\.(?:only|skip|concurrent|each[^\)]*\)))?\s*\(\s*(?P<quote>["'`])(?P<title>.*?)(?P=quote)""",
                re.MULTILINE,
            )
            for m in test_decl_re.finditer(text):
                title = m.group("title").strip()
                if not title:
                    continue
                start_line = text[:m.start()].count("\n")
                end_line = min(start_line + 40, len(lines))
                body_text = "\n".join(lines[start_line:end_line])
                test_id = f"{rel}::{title}"
                tests.append((test_id, title, body_text))
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            tree = ast.parse(text)
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_") or "test" in node.name.lower():
                    if node.lineno < (node.end_lineno or node.lineno):
                        start = node.lineno
                        end = node.end_lineno if hasattr(node, "end_lineno") and node.end_lineno else start + 40
                        body_text = "\n".join(lines[start:end])
                    else:
                        header_and_body = lines[node.lineno - 1] if node.lineno - 1 < len(lines) else ""
                        body_text = header_and_body.split(":", 1)[1] if ":" in header_and_body else header_and_body
                    test_id = f"{rel}::{node.name}"
                    tests.append((test_id, node.name, body_text))
    return tests


def _find_covering_tests_scored(
    claim: str,
    tests: list[tuple[str, str, str]],
    common_body_words: set[str] | None = None,
) -> tuple[list[str], int]:
    keywords = _extract_claim_keywords(claim)
    if not keywords:
        return [], 0

    if common_body_words is None:
        common_body_words = _compute_common_body_words(tests)

    scored = []
    for test_id, func_name, body in tests:
        name_parts = [p.lower() for p in re.split(r"[_\s]+", func_name) if p.lower() not in {"test", "it"}]
        name_stems = set()
        for p in name_parts:
            if len(p) >= 3 and p not in STOP_WORDS:
                name_stems.update(_stems(p))

        body_tokens = _extract_body_tokens(body)
        score = 0
        for kw in keywords:
            kw_s = _stems(kw)
            if kw_s & name_stems:
                score += 3
            if (kw_s & body_tokens) - common_body_words:
                score += 1
        scored.append((test_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    top_score = scored[0][1] if scored else 0
    covered = [tid for tid, s in scored if s >= 3][:2]
    return covered, top_score


def _find_covering_tests(
    claim: str,
    tests: list[tuple[str, str, str]],
    common_body_words: set[str] | None = None,
) -> list[str]:
    covered, _ = _find_covering_tests_scored(claim, tests, common_body_words)
    return covered


def _heuristic_plan(
    *,
    docs_root: Path,
    project_root: Path,
    gaps: list[str],
    claims: list[str],
) -> TestPlan:
    sources = list_sources(project_root)
    code_files, test_files = split_code_and_tests(sources, project_root)
    tokens, excerpt = doc_tokens(docs_root)
    scenarios: list[Scenario] = []
    tests = _collect_test_functions(test_files, project_root)
    common_body_words = _compute_common_body_words(tests)

    has_ts = any(p.suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"} for p in sources)
    has_py = any(p.suffix.lower() == ".py" for p in sources)
    is_pure_ts = has_ts and not has_py

    for i, claim in enumerate(claims, start=1):
        covered, top_score = _find_covering_tests_scored(claim, tests, common_body_words)
        doc_file, doc_line = locate_claim_in_docs(docs_root, claim)
        scenario = Scenario(
            id=f"C{i}",
            title=claim,
            steps=["Verify this claim is true"],
            acceptance=["Claim is true"],
            negative=False,
            covered_by=covered,
            docs_file=doc_file,
            docs_line=doc_line,
        )
        scenario.score = top_score
        scenarios.append(scenario)

    publics: list[str] = []
    for path in code_files:
        publics.extend(public_names(path))

    if test_files:
        runner_title = "Existing test suite completes" if is_pure_ts else "Existing pytest suite completes"
        suite_step = "Run the isolated test suite against the project copy" if is_pure_ts else "Run the isolated pytest suite against the project copy"
        collect_acc = "test runner collects more than 0 tests" if is_pure_ts else "pytest collects more than 0 tests"
        scenarios.append(
            Scenario(
                id="S1",
                title=runner_title,
                steps=[
                    suite_step,
                    "Record collected count, exit code, and failures",
                ],
                acceptance=[
                    collect_acc,
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
        err_step = (
            "If a test fails, the test suite must report failures rather than hiding the error"
            if is_pure_ts
            else "If a test fails, pytest must report failed > 0 rather than hiding the error"
        )
        scenarios.append(
            Scenario(
                id="S3",
                title="Suite reports failures instead of silent collection errors",
                steps=[
                    err_step,
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
                title="Establish a runnable test surface",
                steps=["Discover tests in the isolated copy"],
                acceptance=["collected > 0"],
                negative=False,
            )
        )

    notes = (
        "Scenarios map to the existing test surface; no new files are required."
        if is_pure_ts
        else "Scenarios map to the existing pytest surface; no new files are required."
    )
    if tokens:
        notes += " Docs headings considered: " + ", ".join(tokens[:8]) + "."
    assumptions = [
        "Default language is TypeScript / JavaScript" if is_pure_ts else "Default language is Python + pytest",
        "Execution happens only in the isolated copy",
        "This plan does not claim coverage percentage",
    ]
    if gaps:
        assumptions.append("Previous red-team gaps were considered: " + "; ".join(gaps[:6]))
    return TestPlan(scenarios=scenarios, coverage_notes=notes, assumptions=assumptions)


def _from_llm(data: dict[str, Any], fallback: TestPlan) -> TestPlan:
    scenarios: list[Scenario] = []
    fallback_map = {s.id: s.covered_by for s in fallback.scenarios if s.covered_by}
    fallback_score = {s.id: getattr(s, "score", 0) for s in fallback.scenarios}
    fallback_loc = {
        s.id: (s.docs_file, s.docs_line) for s in fallback.scenarios if s.docs_file
    }
    for idx, item in enumerate(data.get("scenarios") or [], start=1):
        if not isinstance(item, dict):
            continue
        sc_id = str(item.get("id") or f"S{idx}")
        covered = [str(c) for c in (item.get("covered_by") or [])]
        if not covered and sc_id in fallback_map:
            covered = fallback_map[sc_id]
        loc = fallback_loc.get(sc_id, (None, None))
        docs_file = item.get("docs_file") or loc[0]
        docs_line = item.get("docs_line") if item.get("docs_line") is not None else loc[1]
        sc = Scenario(
            id=sc_id,
            title=str(item.get("title") or f"Scenario {idx}"),
            steps=[str(s) for s in (item.get("steps") or [])],
            acceptance=[str(s) for s in (item.get("acceptance") or [])],
            negative=bool(item.get("negative")),
            covered_by=covered,
            docs_file=str(docs_file) if docs_file else None,
            docs_line=int(docs_line) if docs_line else None,
        )
        sc.score = int(item.get("score") or fallback_score.get(sc_id, 0))
        scenarios.append(sc)
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
    claims: list[str] | None = None,
) -> TestPlan:
    gaps = gaps or []
    claims = claims or []
    plan = _heuristic_plan(docs_root=docs_root, project_root=project_root, gaps=gaps, claims=claims)
    payload = {"heuristic_plan": plan.to_dict(), "red_team_gaps": gaps, "claims": claims}
    raw = None
    llm_status = "none"
    try:
        raw, llm_status = complete_with_status(
            system_prompt=load_prompt("aria.md"),
            user_payload=payload,
            config=config,
        )
    except Exception as exc:
        llm_status = format_llm_error(exc)
        raw = None

    plan.llm_status = llm_status
    # Do not replace the heuristic test plan with an LLM plan: that is not
    # deterministic. Status is kept for the report.
    if raw:
        parsed = parse_yaml_block(raw)
        if not parsed:
            plan.llm_status = "fallback: invalid YAML"
        else:
            plan.llm_status = "ok"
    return plan

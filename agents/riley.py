import ast
from pathlib import Path
import re
from typing import Any

from agents.adapters import complete, complete_with_status, format_llm_error, load_prompt, parse_yaml_block
from core.packets import RedTeamReview, TestPlan, Verdict

FAILED_LINE_RE = re.compile(
    r"^FAILED\s+(?P<node>.+?)(?:\s+-\s+(?P<assertion>.*))?$",
    re.MULTILINE,
)


def is_tautology_assert(stmt: ast.Assert) -> bool:
    test_node = stmt.test
    if isinstance(test_node, ast.Constant):
        if bool(test_node.value):
            return True
    if isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not):
        if isinstance(test_node.operand, ast.Constant) and not bool(test_node.operand.value):
            return True
    if isinstance(test_node, ast.Compare):
        if len(test_node.ops) == 1 and isinstance(test_node.ops[0], ast.IsNot):
            if len(test_node.comparators) == 1 and isinstance(test_node.comparators[0], ast.Constant) and test_node.comparators[0].value is None:
                return True
            if isinstance(test_node.left, ast.Constant) and test_node.left.value is None:
                return True
    if isinstance(test_node, ast.Call):
        func = test_node.func
        if isinstance(func, ast.Name) and func.id == "callable":
            return True
    return False


SKIP_NAMES = {"skip", "skipif", "xfail", "skipTest", "skipIf", "skipUnless", "expectedFailure"}


def _call_name(node: ast.AST) -> str | None:
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return None


def is_skipped_test(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the test is skipped or expected to fail by decorator or by an unconditional skip call."""
    for dec in fn_node.decorator_list:
        if _call_name(dec) in SKIP_NAMES:
            return True
    for stmt in fn_node.body:
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            if _call_name(stmt.value) in {"skip", "skipTest", "xfail"}:
                return True
    return False


def _handler_swallows(handler: ast.ExceptHandler) -> bool:
    return all(isinstance(b, (ast.Pass, ast.Continue)) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)) for b in handler.body)


def has_swallowed_assertions(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when every assertion sits inside a try block whose handler silently swallows the failure."""
    asserts = [n for n in ast.walk(fn_node) if isinstance(n, ast.Assert)]
    if not asserts:
        return False
    guarded: set[int] = set()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Try) and any(_handler_swallows(h) for h in node.handlers):
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    if isinstance(inner, ast.Assert):
                        guarded.add(id(inner))
    return all(id(a) in guarded for a in asserts)


def is_trivial_assert(stmt: ast.Assert) -> bool:
    """Assertions that almost any value satisfies: x >= 0 on sizes, isinstance checks, x == x."""
    if is_tautology_assert(stmt):
        return True
    test_node = stmt.test
    if isinstance(test_node, ast.Call) and _call_name(test_node) in {"isinstance", "hasattr", "bool"}:
        return True
    if isinstance(test_node, ast.Compare) and len(test_node.ops) == 1:
        op = test_node.ops[0]
        right = test_node.comparators[0]
        if ast.dump(test_node.left) == ast.dump(right) and isinstance(op, (ast.Eq, ast.GtE, ast.LtE, ast.Is)):
            return True
        if isinstance(op, ast.GtE) and isinstance(right, ast.Constant) and right.value == 0:
            if isinstance(test_node.left, ast.Call) and _call_name(test_node.left) == "len":
                return True
    return False


def is_weak_test_node(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[bool, str]:
    if is_skipped_test(fn_node):
        return True, "skipped test"

    with_raises = any(
        isinstance(n, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "attr", getattr(item.context_expr.func, "id", None)) == "raises"
            for item in n.items
        )
        for n in ast.walk(fn_node)
    )
    if with_raises:
        return False, "ok"

    asserts = [n for n in ast.walk(fn_node) if isinstance(n, ast.Assert)]
    if not asserts:
        return True, "no assertions"
    if all(is_tautology_assert(a) for a in asserts):
        return True, "tautology assertion"
    if has_swallowed_assertions(fn_node):
        return True, "assertion failures swallowed by try/except"
    if all(is_trivial_assert(a) for a in asserts):
        return True, "only trivial assertions"
    return False, "ok"


def detect_weak_tests(project_root: Path | None) -> dict[str, str]:
    weak_tests: dict[str, str] = {}
    if not project_root or not project_root.is_dir():
        return weak_tests

    for path in project_root.rglob("*.py"):
        if any(part in {".venv", "__pycache__", ".git"} for part in path.parts):
            continue
        rel = str(path.relative_to(project_root)).replace("\\", "/")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("test_") or "test" in node.name.lower():
                    is_weak, reason = is_weak_test_node(node)
                    if is_weak:
                        test_id = f"{rel}::{node.name}"
                        weak_tests[test_id] = reason
                        weak_tests[node.name] = reason
    return weak_tests


def review_plan(
    plan: TestPlan,
    *,
    config: dict[str, Any],
    project_root: Path | None = None,
) -> RedTeamReview:
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

    weak_tests = detect_weak_tests(project_root)
    claims_covered_by_weak: list[str] = []
    for s in plan.scenarios:
        if s.id.startswith("C") and s.covered_by:
            strong = [t for t in s.covered_by if t not in weak_tests and t.split("::")[-1] not in weak_tests]
            if not strong:
                claims_covered_by_weak.append(s.id)
                s.covered_by_weak = list(s.covered_by)
                s.covered_by = []

    for cid in claims_covered_by_weak:
        gaps.append(f"claim {cid} is covered only by weak test (not counted as covered)")

    for tid, reason in weak_tests.items():
        if "::" in tid:
            gaps.append(f"weak test: {tid} ({reason})")

    veto_uncovered = bool((config.get("riley") or {}).get("veto_on_uncovered_claims", False))
    uncovered_claims = [s.id for s in plan.scenarios if s.id.startswith("C") and not s.covered_by]
    if veto_uncovered and uncovered_claims:
        gaps.append(f"uncovered claims: {uncovered_claims}")

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
    project_root: Path | None = None,
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

    weak_tests = detect_weak_tests(project_root)
    claims_covered_by_weak: list[str] = []
    for s in plan.scenarios:
        if s.id.startswith("C"):
            weak_cov = getattr(s, "covered_by_weak", None)
            if weak_cov:
                claims_covered_by_weak.append(s.id)
            elif s.covered_by:
                strong = [t for t in s.covered_by if t not in weak_tests and t.split("::")[-1] not in weak_tests]
                if not strong:
                    claims_covered_by_weak.append(s.id)
                    s.covered_by_weak = list(s.covered_by)
                    s.covered_by = []

    for cid in claims_covered_by_weak:
        gaps.append(f"claim {cid} is covered only by weak test (not counted as covered)")

    for tid, reason in weak_tests.items():
        if "::" in tid:
            gaps.append(f"weak test: {tid} ({reason})")

    veto_uncovered = bool((config.get("riley") or {}).get("veto_on_uncovered_claims", False))
    uncovered_claims = [s.id for s in plan.scenarios if s.id.startswith("C") and not s.covered_by]
    for s in plan.scenarios:
        if s.id.startswith("C") and not s.covered_by:
            gaps.append(f"claim not covered by tests: {s.id}")

    if claims_covered_by_weak:
        weak_list = ", ".join(k for k in weak_tests if "::" in k)
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=[f"replace weak tests ({weak_list}) with meaningful assertions"],
                root_cause=f"weak test detected: {weak_list} (claim {', '.join(claims_covered_by_weak)} is not properly tested)",
                notes="tests without meaningful assertions cannot verify claims",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )

    if veto_uncovered and uncovered_claims:
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=[f"add test coverage for claims: {', '.join(uncovered_claims)}"],
                root_cause=f"uncovered claims: {', '.join(uncovered_claims)}",
                notes="strict claims coverage enabled: all claims must have covering tests",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )

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
    raw = None
    llm_status = "none"
    try:
        raw, llm_status = complete_with_status(
            system_prompt=load_prompt("riley.md"),
            user_payload={**payload, "heuristic": review.to_dict()},
            config=config,
        )
    except Exception as exc:
        llm_status = format_llm_error(exc)
        raw = None

    review.llm_status = llm_status
    if not raw:
        return review
    parsed = parse_yaml_block(raw)
    if not parsed:
        review.llm_status = "fallback: invalid YAML"
        return review
    verdict = str(parsed.get("verdict") or review.verdict).upper()
    if verdict not in {Verdict.CLEAR.value, Verdict.VETO.value}:
        verdict = review.verdict
    if review.verdict == Verdict.VETO.value:
        verdict = Verdict.VETO.value
    root = str(parsed.get("root_cause") or "")
    if verdict == Verdict.VETO.value and not root:
        root = review.root_cause or "unspecified technical defect"
    elif verdict == Verdict.VETO.value and review.root_cause and root and root != review.root_cause:
        root = f"{review.root_cause}; {root}"

    gaps = list(review.gaps)
    for g in parsed.get("gaps") or []:
        sg = str(g)
        if sg not in gaps:
            gaps.append(sg)
    required_changes = list(review.required_changes)
    for c in parsed.get("required_changes") or []:
        sc = str(c)
        if sc not in required_changes:
            required_changes.append(sc)

    return RedTeamReview(
        verdict=verdict,
        gaps=gaps,
        required_changes=required_changes,
        root_cause=root if verdict == Verdict.VETO.value else "",
        notes=str(parsed.get("notes") or review.notes),
        llm_status="ok",
    )


def audit_mutations(
    isolated_root: Path,
    *,
    work_dir: Path,
    config: dict[str, Any],
    allow_network: bool = False,
    python: Path | None = None,
) -> dict[str, Any]:
    """Execute mutation testing audit on the isolated copy."""
    from core.mutation import run_mutation_testing

    return run_mutation_testing(
        isolated_root,
        work_dir=work_dir,
        config=config,
        allow_network=allow_network,
        python=python,
    )


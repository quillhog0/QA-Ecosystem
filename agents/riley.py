import ast
import builtins
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

from agents.adapters import complete_with_status, format_llm_error, load_prompt, parse_yaml_block
from core.environment import EnvCause, classify_execution, product_failure_count
from core.packets import RedTeamReview, TestPlan, Verdict

FAILED_LINE_RE = re.compile(
    r"^FAILED\s+(?P<node>.+?)(?:\s+-\s+(?P<assertion>.*))?$",
    re.MULTILINE,
)
ERROR_LINE_RE = re.compile(
    r"^ERROR\s+(?P<node>.+?)(?:\s+-\s+(?P<assertion>.*))?$",
    re.MULTILINE,
)
# One line per failure in the report is the promise; a suite with thousands of
# failures still needs a bound on how deep it goes.
MAX_REPORTED_FAILURE_LINES = 40

# pytest default collection: files named test_*.py or *_test.py.
_PYTEST_COLLECTED_FILE_RE = re.compile(r"^(?:test_.*|.*_test)\.py$")


# Hollow tests that can never fail, plus a leftover ``.only`` that hides the
# rest of the suite. These match the TypeScript analyzer's P1 reasons so
# Python and TS use the same ladder. Other weak patterns (skip, trivial
# assertions, snapshots) stay P2 unless they uniquely guard a security,
# privacy or payment claim.
_P1_WEAK_FRAGMENTS = (
    "assertion failures swallowed",
    "tautology assertion",
    "no assertions",
    "committed .only focus flag",
)

_SECURITY_PRIVACY_PAYMENT_TERMS = (
    "payment", "refund", "charge", "payout", "withdraw", "deposit",
    "fee", "coupon", "invoice", "billing", "price", "discount",
    "currency", "ledger", "money",
    "secret", "credential", "password", "api key", "private key", "token",
    "sandbox", "escape", "data loss", "corrupt", "wipe", "exfil",
    "privacy", "plaintext", "pii", "gdpr", "email", "personal data",
    "confidential", "authentication", "authorization",
)


def _normalize_term_text(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9]+", " ", (text or "").lower()) + " "


def mentions_security_privacy_payment(text: str) -> bool:
    """True when *text* (test id, body, or claim) is about security, privacy or payment."""
    blob = _normalize_term_text(text)
    return any(f" {term} " in blob for term in _SECURITY_PRIVACY_PAYMENT_TERMS)


def weak_priority_for(reason: str, *, text: str = "") -> str:
    low = (reason or "").lower()
    if any(fragment in low for fragment in _P1_WEAK_FRAGMENTS) or "[p1]" in low:
        return "P1"
    if mentions_security_privacy_payment(f"{reason} {text}"):
        return "P1"
    return "P2"


@dataclass
class WeakTestHit:
    reason: str
    file: str = ""
    line: int | None = None
    priority: str = "P2"

    def tagged_reason(self) -> str:
        if self.priority == "P1" and "[p1]" not in self.reason.lower():
            return f"[{self.priority}] {self.reason}"
        return self.reason


def format_weak_gap(test_id: str, hit: WeakTestHit) -> str:
    loc = f" @ {hit.file}:{hit.line}" if hit.file and hit.line else ""
    return f"weak test: {test_id} ({hit.tagged_reason()}){loc}"


def upgrade_hits_for_plan(hits: dict[str, WeakTestHit], plan: TestPlan) -> None:
    """Promote a P2 weak test to P1 when it is the coverage for a security/privacy/payment claim.

    Skipped, trivial, and snapshot-only tests stay P2 — a skip does not
    guard a claim, even if the docs mention sandbox or payment.
    """
    stay_p2 = ("skipped test", "only trivial assertions", "snapshot-only test")
    claim_blobs: dict[str, list[str]] = {}
    for scenario in plan.scenarios:
        blob = " ".join(
            [scenario.title, scenario.id]
            + list(scenario.acceptance or [])
            + list(scenario.steps or [])
        )
        for tid in list(scenario.covered_by or []) + list(scenario.covered_by_weak or []):
            claim_blobs.setdefault(tid, []).append(blob)
            claim_blobs.setdefault(tid.split("::")[-1], []).append(blob)
    for tid, hit in hits.items():
        if hit.priority == "P1":
            continue
        if hit.reason in stay_p2 or "skipped" in hit.reason.lower():
            continue
        extra = " ".join(claim_blobs.get(tid, []))
        name = tid.split("::")[-1]
        if mentions_security_privacy_payment(f"{name} {hit.reason} {extra}"):
            hit.priority = "P1"


def _observes_code(fn_node: ast.AST | None) -> bool:
    """True when the test body calls something that can hand back a value.

    ``assert x is not None`` is a real assertion whenever the test produced ``x``
    by calling something: the call can return None and the check can fail. It is
    vacuous when the body calls nothing at all - then the name can only be an
    imported symbol or a literal, and the check merely proves an import worked.
    Builtins and assert-style helpers do not count as producers, so
    ``assert len(x) is not None`` or ``assert self.value is not None`` in a test
    that never exercises the code is still vacuous.
    """
    if fn_node is None:
        return True
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Attribute):
            if callee.attr.startswith("assert"):
                continue
            return True
        if isinstance(callee, ast.Name):
            if callee.id.startswith("assert") or hasattr(builtins, callee.id):
                continue
            return True
        # A computed callee (``f()()``, a subscript, a lambda) is real work.
        return True
    return False


def is_tautology_assert(stmt: ast.Assert, *, fn_node: ast.AST | None = None) -> bool:
    test_node = stmt.test
    if isinstance(test_node, ast.Constant):
        if bool(test_node.value):
            return True
    if isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not):
        if isinstance(test_node.operand, ast.Constant) and not bool(test_node.operand.value):
            return True
    if isinstance(test_node, ast.Compare):
        if len(test_node.ops) == 1 and isinstance(test_node.ops[0], ast.IsNot):
            # A None-check only vacuums when nothing in the test could have
            # produced the value; against a call result it is a live assertion.
            if not _observes_code(fn_node):
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


def is_trivial_assert(stmt: ast.Assert, *, fn_node: ast.AST | None = None) -> bool:
    """Assertions that almost any value satisfies: x >= 0 on sizes, isinstance checks, x == x."""
    if is_tautology_assert(stmt, fn_node=fn_node):
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


def _raises_context_present(fn_node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and getattr(item.context_expr.func, "attr", getattr(item.context_expr.func, "id", None)) == "raises"
            for item in n.items
        )
        for n in ast.walk(fn_node)
    )


# Mock/spy assertion methods. They fail the test when the expected interaction
# did not happen, so they carry the same weight as ``self.assertEqual``: a test
# that only checks a double's calls is asserting on behaviour, not on nothing.
_MOCK_ASSERTION_PREFIXES = (
    "assert_called",
    "assert_awaited",
    "assert_any_call",
    "assert_any_await",
    "assert_has_calls",
    "assert_not_called",
)


def _mock_assertion_present(fn_node: ast.AST) -> bool:
    """``mock.send.assert_called_once_with(...)`` and the async/awaited variants."""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr.startswith(_MOCK_ASSERTION_PREFIXES)
        for n in ast.walk(fn_node)
    )


def _unittest_assertion_present(fn_node: ast.AST) -> bool:
    """unittest-style assertions (self.assertEqual, self.assertRaises, self.fail)."""
    return any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "self"
        and (n.func.attr.startswith("assert") or n.func.attr == "fail")
        for n in ast.walk(fn_node)
    )


def _assertion_call_present(fn_node: ast.AST) -> bool:
    """Any call that fails the test when the expectation is wrong."""
    return _unittest_assertion_present(fn_node) or _mock_assertion_present(fn_node)


def has_meaningful_assertion(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when this function performs an assertion a wrong value can fail.

    Uses the same checks the test scan applies to tests: a bare ``assert`` counts
    only when it is not tautological or trivial (``assert True``, ``x == x``,
    ``isinstance(...)``) and not swallowed by a silent ``try/except``. A
    ``self.assert*`` / ``self.fail`` call or a ``raises`` context counts, because
    both fail the test when the expectation is wrong. Without the meaningfulness
    filter, a helper body of ``assert True`` would lend its credibility to every
    assertion-free test that calls it.
    """
    if _assertion_call_present(fn_node) or _raises_context_present(fn_node):
        return True
    asserts = [n for n in ast.walk(fn_node) if isinstance(n, ast.Assert)]
    if not asserts or has_swallowed_assertions(fn_node):
        return False
    return any(not is_trivial_assert(a, fn_node=fn_node) for a in asserts)


def _module_assertion_helpers(tree: ast.Module) -> set[str]:
    """Names of module-level functions, and methods of module-level classes, that
    assert meaningfully themselves (``assert_equal`` in ``test/common.py``)."""
    names: set[str] = set()
    for stmt in tree.body:
        defs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(stmt)
        elif isinstance(stmt, ast.ClassDef):
            defs.extend(
                sub
                for sub in stmt.body
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
        for node in defs:
            if has_meaningful_assertion(node):
                names.add(node.name)
    return names


def _helper_source_files(path: Path, project_root: Path) -> list[Path]:
    """Modules a test file may import assertion helpers from: the modules living
    next to it (its test package) and ``conftest.py`` in every ancestor directory
    up to the project root. Nothing outside the test tree is consulted."""
    files: list[Path] = []
    try:
        siblings = sorted(path.parent.glob("*.py"))
    except OSError:
        siblings = []
    files.extend(other for other in siblings if other != path)
    directory = path.parent
    while True:
        conftest = directory / "conftest.py"
        if conftest.is_file():
            files.append(conftest)
        if directory == project_root or directory == directory.parent:
            break
        directory = directory.parent
    return files


def _ancestor_dirs(path: Path, project_root: Path) -> list[Path]:
    """Directories an unqualified import can resolve from - the test file's own
    directory and each ancestor up to the project root, which is the search path
    pytest builds itself. Stops at the root, so nothing outside the tree binds."""
    directories: list[Path] = []
    directory = path.parent
    while True:
        directories.append(directory)
        if directory == project_root or directory == directory.parent:
            break
        directory = directory.parent
    return directories


def _module_file_in_tree(directory: Path, parts: tuple[str, ...]) -> Path | None:
    """Resolve a dotted module path under ``directory`` as ``<name>.py`` or a
    package ``<name>/__init__.py``; None when neither exists."""
    if not parts:
        return None
    base = directory.joinpath(*parts)
    as_file = base.with_suffix(".py")
    if as_file.is_file():
        return as_file
    as_package = base / "__init__.py"
    if as_package.is_file():
        return as_package
    return None


def _imported_module_files(
    tree: ast.Module, origin: Path, project_root: Path
) -> dict[str, Path]:
    """Binding name -> module file, for imports that resolve inside the test tree.

    Covers ``import common``, ``import common as tools``, ``import pkg.mod``
    (bound as ``pkg``), ``from . import common`` and ``from pkg import mod``.
    Function-level imports are included because suites do use them. Anything that
    does not map onto a file under the project root - the standard library,
    third-party packages, fixtures - is left out, so only a real test-tree module
    can qualify an attribute call.
    """
    bindings: dict[str, Path] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import a.b` binds a, `import a.b as c` binds the submodule c.
                parts = (
                    tuple(alias.name.split("."))
                    if alias.asname
                    else (alias.name.split(".")[0],)
                )
                for directory in _ancestor_dirs(origin, project_root):
                    found = _module_file_in_tree(directory, parts)
                    if found is not None:
                        bindings[alias.asname or parts[0]] = found
                        break
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                base = origin.parent
                for _ in range(node.level - 1):
                    base = base.parent
                directories = [base]
            else:
                directories = _ancestor_dirs(origin, project_root)
            mod_parts = tuple(node.module.split(".")) if node.module else ()
            for alias in node.names:
                for directory in directories:
                    found = _module_file_in_tree(
                        directory.joinpath(*mod_parts), (alias.name,)
                    )
                    if found is not None:
                        bindings[alias.asname or alias.name] = found
                        break
    return {
        name: module
        for name, module in bindings.items()
        if module.is_relative_to(project_root)
    }


def _module_helpers(path: Path, cache: dict[Path, frozenset[str]]) -> frozenset[str]:
    """Assertion-helper names defined in one file, parsed at most once per scan."""
    cached = cache.get(path)
    if cached is not None:
        return cached
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, SyntaxError):
        cached = frozenset()
    else:
        cached = frozenset(_module_assertion_helpers(tree))
    cache[path] = cached
    return cached


def _assertion_helpers(
    path: Path,
    tree: ast.Module,
    project_root: Path,
    cache: dict[Path, frozenset[str]],
) -> tuple[frozenset[str], dict[str, frozenset[str]]]:
    """What one test module can assert through: helper names visible in its test
    tree, plus the helper names exported by each test-tree module it imports."""
    names = set(_module_assertion_helpers(tree))
    for extra in _helper_source_files(path, project_root):
        names |= _module_helpers(extra, cache)
    modules: dict[str, frozenset[str]] = {}
    for binding, module in _imported_module_files(tree, path, project_root).items():
        modules[binding] = _module_helpers(module, cache)
    return frozenset(names), modules


def _calls_assertion_helper(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
    helper_names: set[str] | frozenset[str],
    module_helpers: dict[str, frozenset[str]] | None = None,
) -> bool:
    """True when the body calls one of the test-tree helpers: plainly
    (``assert_equal(...)``) or through a module the test file imported
    (``common.assert_equal(...)``). Attribute calls on anything else are not
    resolved - a fixture, mock or handle that merely has a method sharing a
    helper's name is not an assertion."""
    for node in ast.walk(fn_node):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            if func.id != fn_node.name and func.id in helper_names:
                return True
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            exported = (module_helpers or {}).get(func.value.id)
            if exported and func.attr != fn_node.name and func.attr in exported:
                return True
    return False


def is_weak_test_node(
    fn_node: ast.FunctionDef | ast.AsyncFunctionDef,
    helper_names: frozenset[str] = frozenset(),
    module_helpers: dict[str, frozenset[str]] | None = None,
) -> tuple[bool, str]:
    if is_skipped_test(fn_node):
        return True, "skipped test"

    if _raises_context_present(fn_node):
        return False, "ok"

    asserts = [n for n in ast.walk(fn_node) if isinstance(n, ast.Assert)]
    if not asserts:
        if _assertion_call_present(fn_node):
            return False, "ok"
        # Assertion made through a helper defined in the test tree is still an
        # assertion; without this, suites that assert via assert_equal-style
        # helpers look like they have no assertions at all.
        if (helper_names or module_helpers) and _calls_assertion_helper(
            fn_node, helper_names, module_helpers
        ):
            return False, "ok"
        return True, "no assertions"
    if all(is_tautology_assert(a, fn_node=fn_node) for a in asserts):
        return True, "tautology assertion"
    if has_swallowed_assertions(fn_node):
        return True, "assertion failures swallowed by try/except"
    if all(is_trivial_assert(a, fn_node=fn_node) for a in asserts):
        return True, "only trivial assertions"
    return False, "ok"


def _detect_ts_weak_tests(
    project_root: Path | None,
    only_paths: set[str] | None = None,
) -> dict[str, WeakTestHit]:
    if not project_root or not project_root.is_dir():
        return {}
    analyzer_js = Path(__file__).resolve().parent / "ts_analyzer.js"
    if not analyzer_js.is_file():
        return {}
    node_bin = shutil.which("node") or "/tmp/node/node"
    if not shutil.which(node_bin) and not Path(node_bin).is_file():
        return {}

    from core.toolchain import ensure_ts_tooling
    tooling_dir = ensure_ts_tooling()
    if not tooling_dir or not (tooling_dir / "typescript.js").is_file():
        return {}

    cmd = [str(node_bin), str(analyzer_js), "--project", str(project_root)]
    if only_paths:
        cmd.extend(["--only-paths", ",".join(only_paths)])

    env = dict(os.environ)
    env["QA_TOOLING_DIR"] = str(tooling_dir)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False, env=env)
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            results: dict[str, WeakTestHit] = {}
            for item in data.get("weak_tests") or []:
                tid = str(item.get("test_id") or "")
                name = str(item.get("name") or "")
                reason = str(item.get("reason") or "weak test")
                file_name = str(item.get("file") or "")
                line = item.get("line")
                try:
                    line_no = int(line) if line is not None else None
                except (TypeError, ValueError):
                    line_no = None
                priority = str(item.get("priority") or weak_priority_for(reason, text=f"{tid} {name}"))
                if priority != "P1" and mentions_security_privacy_payment(f"{name} {reason}"):
                    priority = "P1"
                hit = WeakTestHit(reason=reason, file=file_name, line=line_no, priority=priority)
                if tid:
                    results[tid] = hit
                if name:
                    results[name] = hit
            return results
    except Exception:
        pass
    return {}


def detect_weak_test_hits(
    project_root: Path | None,
    only_paths: set[str] | None = None,
) -> dict[str, WeakTestHit]:
    hits: dict[str, WeakTestHit] = {}
    if not project_root or not project_root.is_dir():
        return hits

    # TS / JS tests
    hits.update(_detect_ts_weak_tests(project_root, only_paths=only_paths))

    helper_cache: dict[Path, frozenset[str]] = {}
    for path in project_root.rglob("*.py"):
        if any(part in {".venv", "__pycache__", ".git"} for part in path.parts):
            continue
        try:
            rel_parts = path.relative_to(project_root).parts[:-1]
        except ValueError:
            rel_parts = path.parts[:-1]
        if any(part in {"fixtures", "test_fixtures", "benchmark", "bench"} for part in rel_parts):
            continue
        # Only scan files pytest would actually collect (default python_files
        # = test_*.py / *_test.py). This skips conftest.py, benchmark scripts,
        # helpers and runner modules that merely happen to mention "test".
        if not _PYTEST_COLLECTED_FILE_RE.match(path.name):
            continue
        rel = str(path.relative_to(project_root)).replace("\\", "/")
        if only_paths is not None and rel not in only_paths:
            # Diff mode: review only the tests that changed.
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            continue
        helper_names, module_helpers = _assertion_helpers(
            path, tree, project_root, helper_cache
        )
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Default pytest python_functions = test* — a name merely
                # containing "test" (pytest_timeout_x, script_has_no_test_steps)
                # is not collected, so it is not a weak test.
                if node.name.startswith("test"):
                    is_weak, reason = is_weak_test_node(node, helper_names, module_helpers)
                    if is_weak:
                        test_id = f"{rel}::{node.name}"
                        # Term matching uses the function name only — a path
                        # like tests/unit/test_sandbox.py must not promote a
                        # skipped test to P1 just because the file name has
                        # a security word.
                        priority = weak_priority_for(reason, text=node.name)
                        hit = WeakTestHit(
                            reason=reason,
                            file=rel,
                            line=int(node.lineno) if getattr(node, "lineno", None) else None,
                            priority=priority,
                        )
                        hits[test_id] = hit
                        hits[node.name] = hit
    return hits


def detect_weak_tests(
    project_root: Path | None,
    only_paths: set[str] | None = None,
) -> dict[str, str]:
    """Map of test id / function name to the raw weak-test reason (no priority tag)."""
    return {key: hit.reason for key, hit in detect_weak_test_hits(project_root, only_paths=only_paths).items()}


def review_plan(
    plan: TestPlan,
    *,
    config: dict[str, Any],
    project_root: Path | None = None,
    only_paths: set[str] | None = None,
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

    hits = detect_weak_test_hits(project_root, only_paths=only_paths)
    upgrade_hits_for_plan(hits, plan)
    claims_covered_by_weak: list[str] = []
    for s in plan.scenarios:
        if s.id.startswith("C") and s.covered_by:
            strong = [t for t in s.covered_by if t not in hits and t.split("::")[-1] not in hits]
            if not strong:
                claims_covered_by_weak.append(s.id)
                s.covered_by_weak = list(s.covered_by)
                s.covered_by = []

    for cid in claims_covered_by_weak:
        gaps.append(f"claim {cid} is covered only by weak test (not counted as covered)")

    for tid in sorted(hits):
        if "::" in tid:
            gaps.append(format_weak_gap(tid, hits[tid]))

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

    if "PackageNotFoundError: No package metadata was found for" in text:
        match = re.search(
            r"No package metadata was found for ([A-Za-z0-9_.\-]+)", text
        )
        return f"missing package metadata: {match.group(1) if match else 'unknown'}"

    # Nothing failed with an assertion, but the run may still have named the
    # collection or teardown problems that stopped it.
    for match in ERROR_LINE_RE.finditer(text):
        node = (match.group("node") or "").strip()
        assertion = (match.group("assertion") or "").strip()
        parts.append(f"error ({node}): {assertion}" if assertion else f"error ({node})")
    if parts:
        return "pytest errors: " + "; ".join(parts)

    return ""


def _one_line(text: str, limit: int = 300) -> str:
    """Collapse a pytest message to one readable line, keeping its tail."""
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    return "..." + flat[-limit:]


def failure_lines(execution: dict[str, Any]) -> list[str]:
    """One line per failed test: what it was, and the message that explains it.

    The structured failures from pytest's JUnit XML are the authority here; the
    console summary is only a fallback for a run whose XML could not be read. This
    is what makes "every failed test appears in the report" true even when the log
    is far longer than any clip.
    """
    lines: list[str] = []
    for failure in execution.get("failures") or []:
        if not isinstance(failure, dict):
            continue
        node = str(failure.get("node") or "unknown test")
        kind = "error" if str(failure.get("kind") or "") == "error" else "failure"
        message = _one_line(failure.get("message") or "")
        note = " [environment]" if str(failure.get("classification") or "") == "environment" else ""
        lines.append(f"{kind} in {node}: {message}{note}" if message else f"{kind} in {node}{note}")
    if lines:
        return lines
    text = f"{execution.get('stdout') or ''}\n{execution.get('stderr') or ''}"
    for match in FAILED_LINE_RE.finditer(text):
        node = (match.group("node") or "").strip() or "unknown test"
        assertion = _one_line(match.group("assertion") or "")
        lines.append(f"failure in {node}: {assertion}" if assertion else f"failure in {node}")
    for match in ERROR_LINE_RE.finditer(text):
        node = (match.group("node") or "").strip() or "unknown collection error"
        assertion = _one_line(match.group("assertion") or "")
        lines.append(f"error in {node}: {assertion}" if assertion else f"error in {node}")
    return lines


def environment_failure_cause(execution: dict[str, Any]) -> EnvCause | None:
    """An EnvCause for a run whose only failures are environmental, else ``None``.

    Every failure has already been proved one by one by ``annotate_failures``; this
    turns that proof into the sentence the report shows. A run with even one
    product failure gets ``None``, so the accusation stands.
    """
    failed = int(execution.get("failed") or 0)
    product = product_failure_count(execution)
    environment = int(execution.get("environment_failed") or 0)
    if failed <= 0 or product > 0 or environment <= 0:
        return None
    names = [
        str(failure.get("node") or "unknown test")
        for failure in execution.get("failures") or []
        if isinstance(failure, dict)
        and str(failure.get("classification") or "") == "environment"
    ][:MAX_REPORTED_FAILURE_LINES]
    kinds = {
        str(failure.get("environment_kind") or "")
        for failure in execution.get("failures") or []
        if isinstance(failure, dict)
        and str(failure.get("classification") or "") == "environment"
    }
    detail = ", ".join(kinds) or "environment"
    listed = "; ".join(names) if names else f"{environment} tests"
    return EnvCause(
        "environment_failures",
        f"environment-blocked test failures ({detail}): {listed} "
        f"({environment} of {failed} failures; they did not judge the project)",
    )


def review_results(
    plan: TestPlan,
    execution: dict[str, Any],
    *,
    config: dict[str, Any],
    project_root: Path | None = None,
    only_paths: set[str] | None = None,
) -> RedTeamReview:
    gaps: list[str] = []
    collected = int(execution.get("collected") or 0)
    failed = int(execution.get("failed") or 0)
    exit_code = int(execution.get("exit_code") if execution.get("exit_code") is not None else -1)
    errors = int(execution.get("errors") or 0)
    install_status = str(execution.get("install_status") or "skipped")
    # Two kinds of failure: the ones that accuse the project, and the ones the
    # isolated environment caused by itself. Only the first are counted as
    # product failures; both are listed, each with its own name and message.
    product_failed = product_failure_count(execution)
    environment_failed = int(execution.get("environment_failed") or 0)
    detail_lines = failure_lines(execution)

    if collected <= 0:
        gaps.append("collected tests == 0")
    if exit_code != 0:
        gaps.append(f"pytest exit code {exit_code}")
    if failed > 0:
        gaps.append(f"failed={failed}")
    if environment_failed:
        gaps.append(
            f"environment-blocked failures={environment_failed} "
            f"(product failures={product_failed})"
        )
    for line in detail_lines[:MAX_REPORTED_FAILURE_LINES]:
        gaps.append(f"test detail: {line}")
    if errors:
        gaps.append(f"pytest collection/execution errors={errors}")
    if not plan.scenarios:
        gaps.append("no plan to compare against results")

    hits = detect_weak_test_hits(project_root, only_paths=only_paths)
    upgrade_hits_for_plan(hits, plan)
    veto_weak = bool((config.get("riley") or {}).get("veto_on_weak_tests", False))
    if veto_weak:
        for hit in hits.values():
            hit.priority = "P1"
    claims_covered_by_weak: list[str] = []
    for s in plan.scenarios:
        if s.id.startswith("C"):
            weak_cov = getattr(s, "covered_by_weak", None)
            if weak_cov:
                claims_covered_by_weak.append(s.id)
            elif s.covered_by:
                strong = [
                    t for t in s.covered_by
                    if t not in hits and (t.split("::")[-1] not in hits if "::" not in t else True)
                ]
                if not strong:
                    claims_covered_by_weak.append(s.id)
                    s.covered_by_weak = list(s.covered_by)
                    s.covered_by = []

    for cid in claims_covered_by_weak:
        gaps.append(f"claim {cid} is covered only by weak test (not counted as covered)")

    for tid in sorted(hits):
        if "::" in tid:
            gaps.append(format_weak_gap(tid, hits[tid]))

    veto_uncovered = bool((config.get("riley") or {}).get("veto_on_uncovered_claims", False))
    uncovered_claims = [s.id for s in plan.scenarios if s.id.startswith("C") and not s.covered_by]
    for s in plan.scenarios:
        if s.id.startswith("C") and not s.covered_by:
            gaps.append(f"claim not covered by tests: {s.id}")

    # An environmental stop is not an accusation: do not let a hollow
    # test outrank a run that never got a real chance to witness the plan.
    # A product failure keeps env_cause as None (classify_execution).
    env_cause = classify_execution(execution, project_root=project_root)
    if env_cause is None:
        env_cause = environment_failure_cause(execution)

    # Verdict follows findings: only a P1 weak test (hollow or
    # security/privacy/payment) can VETO, and only when the suite actually
    # ran. Uncovered or weak-only claims stay in the gaps as P2 warnings
    # and never set VETO on their own.
    flagged_weak = [k for k, hit in hits.items() if "::" in k and hit.priority == "P1"]
    if flagged_weak and env_cause is None:
        weak_list = ", ".join(flagged_weak)
        return _maybe_llm(
            RedTeamReview(
                verdict=Verdict.VETO.value,
                gaps=gaps,
                required_changes=[f"replace weak tests ({weak_list}) with meaningful assertions"],
                root_cause=f"weak test detected: {weak_list}",
                notes="tests without meaningful assertions cannot verify claims",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )

    failure_cause = parse_failure_root_cause(
        str(execution.get("stdout") or ""),
        str(execution.get("stderr") or ""),
    )
    if not failure_cause and detail_lines:
        # The console summary may be clipped away in a long run; the structured
        # failures are not, so they still name every failed test.
        failure_cause = "pytest failures: " + "; ".join(
            detail_lines[:MAX_REPORTED_FAILURE_LINES]
        )
    # env_cause was already classified above (before any weak-test VETO).
    technical_verdict = Verdict.BLOCKED_ENV.value if env_cause else Verdict.VETO.value
    blocked_note = "environment blocked: the tests could not run"

    if collected <= 0:
        return _maybe_llm(
            RedTeamReview(
                verdict=technical_verdict,
                gaps=gaps,
                required_changes=[],
                root_cause=(
                    env_cause.cause
                    if env_cause
                    else "Nova collected zero tests, so results cannot witness the plan"
                ),
                notes=blocked_note if env_cause else "empty collection is a technical veto",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )
    if errors:
        root = (env_cause.cause if env_cause else failure_cause) or (
            "pytest reported collection or runtime errors; results are not a clean verdict"
        )
        return _maybe_llm(
            RedTeamReview(
                verdict=technical_verdict,
                gaps=gaps,
                required_changes=[],
                root_cause=root,
                notes=blocked_note if env_cause else "errors must be explained before CLEAR",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )
    if install_status.startswith("pip install failed"):
        return _maybe_llm(
            RedTeamReview(
                verdict=technical_verdict,
                gaps=gaps,
                required_changes=[],
                root_cause=(
                    env_cause.cause
                    if env_cause
                    else f"dependency installation failed: {install_status}"
                ),
                notes="dependency installation failed",
            ),
            {"phase": "results", "plan": plan.to_dict(), "execution": _small_exec(execution)},
            config,
        )
    if exit_code != 0 or failed > 0:
        root = (env_cause.cause if env_cause else failure_cause) or (
            f"pytest exit {exit_code}, failed={failed}; no failure detail was "
            "recorded - see pytest.log in the run directory"
        )
        return _maybe_llm(
            RedTeamReview(
                verdict=technical_verdict,
                gaps=gaps,
                required_changes=[],
                root_cause=root,
                notes=blocked_note if env_cause else "failing tests are a technical veto",
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
    # Do not add gaps or upgrade a CLEAR to VETO from the LLM. Ambiguous
    # findings are triaged later (downgrade or annotate only).
    if raw:
        parsed = parse_yaml_block(raw)
        if not parsed:
            review.llm_status = "fallback: invalid YAML"
        else:
            review.llm_status = "ok"
    return review


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


"""Mutation testing engine for QA Ecosystem.

Identifies mutable AST locations in tested source code, generates targeted
mutants, executes them in isolated bwrap/guard sandboxes, and calculates
the mutation score (killed / (killed + survived)).
"""

from __future__ import annotations

import ast
import copy
import os
import queue
import random
import shutil
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.packets import ExecutionResult, Verdict
from core.runner import run_pytest


@dataclass
class Mutant:
    id: str
    file_path: str  # Relative to project root
    lineno: int
    col_offset: int
    mutation_type: str  # comparison, arithmetic, bool_constant, return_value, bool_op
    original: str
    mutated: str
    node_index: int
    status: str = "pending"  # killed, survived, timeout, error
    details: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


CMP_MUTATIONS: dict[type[ast.cmpop], tuple[type[ast.cmpop], str, str]] = {
    ast.Lt: (ast.LtE, "<", "<="),
    ast.LtE: (ast.Lt, "<=", "<"),
    ast.Gt: (ast.GtE, ">", ">="),
    ast.GtE: (ast.Gt, ">=", ">"),
    ast.Eq: (ast.NotEq, "==", "!="),
    ast.NotEq: (ast.Eq, "!=", "=="),
}

BINOP_MUTATIONS: dict[type[ast.operator], tuple[type[ast.operator], str, str]] = {
    ast.Add: (ast.Sub, "+", "-"),
    ast.Sub: (ast.Add, "-", "+"),
}

BOOLOP_MUTATIONS: dict[type[ast.boolop], tuple[type[ast.boolop], str, str]] = {
    ast.And: (ast.Or, "and", "or"),
    ast.Or: (ast.And, "or", "and"),
}

SKIP_DIR_NAMES = {
    "tests",
    "test",
    "fixtures",
    "seeded",
    "benchmark",
    "examples",
    "reports",
    ".venv",
    "venv",
    "env",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
}


def is_mutable_file(rel_path: Path) -> bool:
    """Return True if rel_path is candidate source code (not tests or test infra)."""
    parts = set(rel_path.parts)
    if parts.intersection(SKIP_DIR_NAMES):
        return False
    name = rel_path.name.lower()
    if name.startswith("test_") or name.endswith("_test.py"):
        return False
    if name == "conftest.py" or name.startswith("conftest"):
        return False
    if not name.endswith(".py"):
        return False
    return True


class MutationCollector(ast.NodeVisitor):
    """Scan an AST to locate mutable points."""

    def __init__(self, rel_path: str) -> None:
        self.rel_path = rel_path
        self.mutants: list[Mutant] = []
        self._counter = 0

    def visit_Compare(self, node: ast.Compare) -> None:
        for idx, op in enumerate(node.ops):
            op_type = type(op)
            if op_type in CMP_MUTATIONS:
                _, orig_str, mut_str = CMP_MUTATIONS[op_type]
                self.mutants.append(
                    Mutant(
                        id=f"m-{len(self.mutants) + 1}",
                        file_path=self.rel_path,
                        lineno=getattr(node, "lineno", 1),
                        col_offset=getattr(node, "col_offset", 0),
                        mutation_type="comparison",
                        original=orig_str,
                        mutated=mut_str,
                        node_index=self._counter,
                    )
                )
                self._counter += 1
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        op_type = type(node.op)
        if op_type in BINOP_MUTATIONS:
            _, orig_str, mut_str = BINOP_MUTATIONS[op_type]
            self.mutants.append(
                Mutant(
                    id=f"m-{len(self.mutants) + 1}",
                    file_path=self.rel_path,
                    lineno=getattr(node, "lineno", 1),
                    col_offset=getattr(node, "col_offset", 0),
                    mutation_type="arithmetic",
                    original=orig_str,
                    mutated=mut_str,
                    node_index=self._counter,
                )
            )
            self._counter += 1
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if type(node.value) is bool:
            orig_str = "True" if node.value else "False"
            mut_str = "False" if node.value else "True"
            self.mutants.append(
                Mutant(
                    id=f"m-{len(self.mutants) + 1}",
                    file_path=self.rel_path,
                    lineno=getattr(node, "lineno", 1),
                    col_offset=getattr(node, "col_offset", 0),
                    mutation_type="bool_constant",
                    original=orig_str,
                    mutated=mut_str,
                    node_index=self._counter,
                )
            )
            self._counter += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        op_type = type(node.op)
        if op_type in BOOLOP_MUTATIONS:
            _, orig_str, mut_str = BOOLOP_MUTATIONS[op_type]
            self.mutants.append(
                Mutant(
                    id=f"m-{len(self.mutants) + 1}",
                    file_path=self.rel_path,
                    lineno=getattr(node, "lineno", 1),
                    col_offset=getattr(node, "col_offset", 0),
                    mutation_type="bool_op",
                    original=orig_str,
                    mutated=mut_str,
                    node_index=self._counter,
                )
            )
            self._counter += 1
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        is_none = (
            node.value is None
            or (isinstance(node.value, ast.Constant) and node.value.value is None)
        )
        if is_none:
            orig_str = "return None" if node.value is not None else "return"
            mut_str = "return True"
        else:
            orig_str = "return <expr>"
            mut_str = "return None"

        self.mutants.append(
            Mutant(
                id=f"m-{len(self.mutants) + 1}",
                file_path=self.rel_path,
                lineno=getattr(node, "lineno", 1),
                col_offset=getattr(node, "col_offset", 0),
                mutation_type="return_value",
                original=orig_str,
                mutated=mut_str,
                node_index=self._counter,
            )
        )
        self._counter += 1
        self.generic_visit(node)


class MutationApplier(ast.NodeTransformer):
    """Replace exactly one mutable node identified by its node_index."""

    def __init__(self, target_node_index: int) -> None:
        self.target_node_index = target_node_index
        self._counter = 0

    def visit_Compare(self, node: ast.Compare) -> ast.AST:
        new_ops = []
        for op in node.ops:
            op_type = type(op)
            if op_type in CMP_MUTATIONS:
                if self._counter == self.target_node_index:
                    new_op_cls = CMP_MUTATIONS[op_type][0]
                    new_ops.append(new_op_cls())
                else:
                    new_ops.append(op)
                self._counter += 1
            else:
                new_ops.append(op)
        node.ops = new_ops
        return self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        op_type = type(node.op)
        if op_type in BINOP_MUTATIONS:
            if self._counter == self.target_node_index:
                new_op_cls = BINOP_MUTATIONS[op_type][0]
                node.op = new_op_cls()
            self._counter += 1
        return self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        if type(node.value) is bool:
            if self._counter == self.target_node_index:
                node.value = not node.value
            self._counter += 1
        return self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> ast.AST:
        op_type = type(node.op)
        if op_type in BOOLOP_MUTATIONS:
            if self._counter == self.target_node_index:
                new_op_cls = BOOLOP_MUTATIONS[op_type][0]
                node.op = new_op_cls()
            self._counter += 1
        return self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> ast.AST:
        if self._counter == self.target_node_index:
            is_none = (
                node.value is None
                or (isinstance(node.value, ast.Constant) and node.value.value is None)
            )
            if is_none:
                node.value = ast.Constant(value=True)
            else:
                node.value = ast.Constant(value=None)
        self._counter += 1
        return self.generic_visit(node)


def collect_file_mutants(file_path: Path, rel_path: str) -> list[Mutant]:
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return []
    collector = MutationCollector(rel_path)
    collector.visit(tree)
    return collector.mutants


def apply_mutation_to_source(source: str, target_node_index: int) -> str:
    tree = ast.parse(source)
    applier = MutationApplier(target_node_index)
    mutated_tree = applier.visit(tree)
    ast.fix_missing_locations(mutated_tree)
    return ast.unparse(mutated_tree)


def find_all_mutants(project_root: Path) -> list[Mutant]:
    mutants: list[Mutant] = []
    for path in sorted(project_root.rglob("*.py")):
        rel = path.relative_to(project_root)
        if not is_mutable_file(rel):
            continue
        file_mutants = collect_file_mutants(path, rel.as_posix())
        mutants.extend(file_mutants)

    # Deterministic sorting
    mutants.sort(key=lambda m: (m.file_path, m.lineno, m.col_offset, m.mutation_type, m.node_index))
    for idx, m in enumerate(mutants, start=1):
        m.id = f"m-{idx:03d}"
    return mutants


def select_mutants(mutants: list[Mutant], max_mutants: int, seed: int) -> list[Mutant]:
    if max_mutants <= 0:
        return []
    if len(mutants) <= max_mutants:
        return list(mutants)
    rng = random.Random(seed)
    chosen_indices = sorted(rng.sample(range(len(mutants)), max_mutants))
    return [mutants[i] for i in chosen_indices]


def make_tree_writable(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        try:
            if path.is_file():
                os.chmod(path, path.stat().st_mode | stat.S_IWUSR | stat.S_IRUSR)
            elif path.is_dir():
                os.chmod(path, path.stat().st_mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
        except OSError:
            pass
    try:
        os.chmod(root, root.stat().st_mode | stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
    except OSError:
        pass


MAX_MUTATION_WORKERS = 16


def _resolve_mutation_workers(mutation_cfg: dict[str, Any]) -> int:
    """Resolve the mutation worker count from operator config only.

    Default is min(4, cpu_count); the value is always clamped to [1, 16]. A
    malformed or out-of-range entry degrades to the guarded default rather than
    an unbounded spawn, mirroring how the other mutation knobs are read.
    """
    default_workers = min(4, os.cpu_count() or 1)
    raw = mutation_cfg.get("workers")
    if raw is None:
        workers = default_workers
    else:
        try:
            workers = int(raw)
        except (TypeError, ValueError):
            workers = default_workers
    if workers < 1:
        workers = 1
    if workers > MAX_MUTATION_WORKERS:
        workers = MAX_MUTATION_WORKERS
    return workers


def run_mutation_testing(
    isolated_root: Path,
    *,
    work_dir: Path,
    config: dict[str, Any],
    allow_network: bool = False,
    python: Path | None = None,
) -> dict[str, Any]:
    """Execute mutation testing against isolated tree."""
    mutation_cfg = dict(config.get("mutation") or {})
    max_mutants = int(mutation_cfg.get("max_mutants", 30))
    timeout_per_mutant = int(mutation_cfg.get("timeout_per_mutant", 30))
    seed = int(mutation_cfg.get("seed", 42))
    min_score = float(mutation_cfg.get("min_score", 0.0))

    all_mutants = find_all_mutants(isolated_root)
    if max_mutants <= 0 and all_mutants:
        return {
            "status": "skipped",
            "enabled": True,
            "verdict": Verdict.CLEAR.value,
            "min_score": min_score,
            "total_candidates": len(all_mutants),
            "evaluated": 0,
            "reason": f"max_mutants is {max_mutants}; no mutants evaluated, score not computed",
            "survived_mutants": [],
            "mutants": [],
        }
    selected = select_mutants(all_mutants, max_mutants=max_mutants, seed=seed)

    if not selected:
        return {
            "status": "completed",
            "enabled": True,
            "verdict": Verdict.CLEAR.value,
            "score": 1.0,
            "min_score": min_score,
            "total_candidates": len(all_mutants),
            "evaluated": 0,
            "killed": 0,
            "survived": 0,
            "timeout": 0,
            "error": 0,
            "reason": "no mutable code points found",
            "survived_mutants": [],
            "mutants": [],
        }

    # Create writable mutant sandbox copy
    workers = _resolve_mutation_workers(mutation_cfg)
    sable_guard = work_dir / "_sable_guard.py"

    # Outcomes are stored by selection index so the aggregated counters and the
    # survivor list stay byte-for-byte identical to a sequential run, no matter
    # which worker finishes a given mutant first.
    outcomes: list[tuple[str, dict[str, Any] | None]] = [("", None)] * len(selected)

    def _make_tree(name: str) -> Path:
        tree = work_dir / name
        if tree.exists():
            shutil.rmtree(tree, ignore_errors=True)

        work_resolved = work_dir.resolve()

        def _ignore_work_dir(src, names):
            # Never recurse the workspace back into a tree copy: the mutant
            # trees and per-run dirs live under work_dir, which may itself sit
            # inside isolated_root (unit fixtures).
            skipped = set()
            for n in names:
                try:
                    if (Path(src) / n).resolve() == work_resolved:
                        skipped.add(n)
                except OSError:
                    pass
            return skipped

        shutil.copytree(isolated_root, tree, symlinks=True, ignore=_ignore_work_dir)
        make_tree_writable(tree)
        return tree

    def _evaluate(mutant: Mutant, tree: Path) -> tuple[str, dict[str, Any] | None]:
        target_file = tree / mutant.file_path
        if not target_file.is_file():
            mutant.status = "error"
            mutant.details = "target file missing in mutant tree"
            return ("error", None)

        original_code = target_file.read_text(encoding="utf-8", errors="replace")
        try:
            mutated_code = apply_mutation_to_source(original_code, mutant.node_index)
        except Exception as exc:
            mutant.status = "error"
            mutant.details = f"failed to apply mutation: {exc}"
            return ("error", None)

        target_file.write_text(mutated_code, encoding="utf-8")

        m_work = work_dir / f"m_run_{mutant.id}"
        m_work.mkdir(parents=True, exist_ok=True)
        if sable_guard.is_file():
            try:
                shutil.copy2(sable_guard, m_work / "_sable_guard.py")
            except OSError:
                pass

        try:
            res: ExecutionResult = run_pytest(
                tree,
                work_dir=m_work,
                allow_network=allow_network,
                timeout=timeout_per_mutant,
                python=python,
                config=config,
                extra_args=["-x", "-q"],
            )

            active_count = res.collected or (res.passed + res.failed + res.skipped + res.errors)
            if res.exit_code == 124 or "TIMEOUT" in str(res.stderr or ""):
                mutant.status = "timeout"
                mutant.details = f"timed out after {timeout_per_mutant}s"
                return ("timeout", None)
            elif res.exit_code == 0 and res.failed == 0 and (active_count > 0 or res.passed > 0):
                mutant.status = "survived"
                mutant.details = f"passed ({res.passed} passed)"
                survivor = {
                    "id": mutant.id,
                    "location": f"{mutant.file_path}:{mutant.lineno}",
                    "type": mutant.mutation_type,
                    "original": mutant.original,
                    "mutated": mutant.mutated,
                }
                return ("survived", survivor)
            elif res.failed > 0 or res.errors > 0 or (res.exit_code != 0 and active_count > 0):
                mutant.status = "killed"
                mutant.details = f"killed (exit={res.exit_code}, failed={res.failed})"
                return ("killed", None)
            else:
                mutant.status = "error"
                mutant.details = f"error running tests (exit={res.exit_code}, collected={res.collected})"
                return ("error", None)
        finally:
            target_file.write_text(original_code, encoding="utf-8")
            shutil.rmtree(m_work, ignore_errors=True)

    if workers == 1:
        tree = _make_tree("mutants_tree")
        try:
            for idx, mutant in enumerate(selected):
                outcomes[idx] = _evaluate(mutant, tree)
        finally:
            shutil.rmtree(tree, ignore_errors=True)
    else:
        # Each worker owns a writable tree copy; the queue guarantees a mutant
        # runs alone on its tree, so parallel results match a sequential run.
        trees = [_make_tree(f"mutants_tree_{i}") for i in range(workers)]
        tree_pool: "queue.Queue[Path]" = queue.Queue()
        for tree in trees:
            tree_pool.put(tree)

        def _run_with_tree(idx: int, mutant: Mutant) -> None:
            tree = tree_pool.get()
            try:
                outcomes[idx] = _evaluate(mutant, tree)
            finally:
                tree_pool.put(tree)

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [
                    pool.submit(_run_with_tree, idx, mutant)
                    for idx, mutant in enumerate(selected)
                ]
                for fut in futures:
                    fut.result()
        finally:
            for tree in trees:
                shutil.rmtree(tree, ignore_errors=True)

    killed = 0
    survived = 0
    timeout_count = 0
    error_count = 0
    survived_list: list[dict[str, Any]] = []
    for kind, survivor in outcomes:
        if kind == "killed":
            killed += 1
        elif kind == "survived":
            survived += 1
            survived_list.append(survivor)
        elif kind == "timeout":
            timeout_count += 1
        else:
            error_count += 1

    evaluated = len(selected)
    active = killed + survived
    score = (killed / active) if active > 0 else 0.0

    if min_score > 0 and score < min_score:
        verdict = Verdict.VETO.value
        reason = (
            f"mutation score {score:.2f} is below minimum required score {min_score:.2f} "
            f"({killed} killed, {survived} survived)"
        )
    else:
        verdict = Verdict.CLEAR.value
        reason = f"mutation score {score:.2f} ({killed} killed, {survived} survived)"

    return {
        "status": "completed",
        "enabled": True,
        "verdict": verdict,
        "score": round(score, 4),
        "min_score": min_score,
        "total_candidates": len(all_mutants),
        "evaluated": evaluated,
        "killed": killed,
        "survived": survived,
        "timeout": timeout_count,
        "error": error_count,
        "reason": reason,
        "survived_mutants": survived_list,
        "mutants": [m.to_dict() for m in selected],
    }

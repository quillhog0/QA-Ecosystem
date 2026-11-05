"""Diff scope for the fast pre-push mode (``--diff <base-ref>``).

A diff run scopes each role to the files that actually changed since a base ref,
so a push check stays under a couple of minutes:

* **Lexi** reads only the changed docs plus the docs that mention a symbol
  defined in the changed code;
* **Riley** and **Sable** scan only the changed files (tree-level guards such as
  the secret and live-tree checks still run over the whole copy, and every
  ``conftest.py`` is always scanned, because a changed conftest can affect tests
  it is not itself part of);
* **Nova** runs only the tests related to the changed files (same-name test
  files, tests that import a changed module) plus any changed tests.

Mutation testing is switched off by the orchestrator in diff mode.

If the scope cannot be computed - no git, an unknown base ref, or a diff that
touches too many files - :class:`DiffScopeError` is raised and the orchestrator
falls back to a full run, never a silent skip.
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_MAX_FILES = 200

_DOC_SUFFIXES = (".md", ".markdown", ".rst", ".txt", ".adoc")
_TEST_NAME_MARKERS = ("test_", "_test.py")


def _posix(path: str) -> str:
    return str(path).replace("\\", "/")


def _is_doc(rel: str) -> bool:
    return rel.lower().endswith(_DOC_SUFFIXES)


def _is_test(rel: str) -> bool:
    posix_path = _posix(rel)
    name = posix_path.rsplit("/", 1)[-1]
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if any(name.endswith(ext) for ext in (".test.ts", ".spec.ts", ".test.js", ".spec.js", ".test.tsx", ".spec.tsx")):
        return True
    if "/__tests__/" in posix_path:
        return True
    return False


def _is_python(rel: str) -> bool:
    return rel.endswith(".py")


def _git(live_root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(live_root), *args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        raise DiffScopeError(
            f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()[:200]}"
        )
    return proc.stdout


@dataclass
class DiffScope:
    base_ref: str
    changed: list[str] = field(default_factory=list)
    changed_code: list[str] = field(default_factory=list)
    changed_tests: list[str] = field(default_factory=list)
    changed_docs: list[str] = field(default_factory=list)
    # Docs Lexi may report drift on: changed docs plus docs naming a symbol
    # defined in the changed code. Keys are strings matched by path suffix so
    # the repo-relative git path lines up with Lexi's docs-root-relative path.
    lexi_docs_allowlist: list[str] = field(default_factory=list)
    # Files Riley and Sable scan (relative posix paths inside the copy).
    riley_sable_paths: list[str] = field(default_factory=list)
    # Test file paths Nova should collect; empty means "no related tests".
    nova_test_targets: list[str] = field(default_factory=list)
    # 1-based line numbers added/modified per changed code file vs base_ref.
    changed_lines: dict[str, list[int]] = field(default_factory=dict)
    # Changed code files that have no related test target in nova_test_targets.
    uncovered_changed_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_ref": self.base_ref,
            "changed": list(self.changed),
            "changed_code": list(self.changed_code),
            "changed_tests": list(self.changed_tests),
            "changed_docs": list(self.changed_docs),
            "nova_test_targets": list(self.nova_test_targets),
            "changed_lines": dict(self.changed_lines),
            "uncovered_changed_files": list(self.uncovered_changed_files),
            "riley_sable_path_count": len(self.riley_sable_paths),
            "lexi_doc_count": len(self.lexi_docs_allowlist),
        }


class DiffScopeError(Exception):
    """Raised when a diff scope cannot be computed; the caller runs in full."""


def _symbols_defined_in(rel_paths: list[str], live_root: Path) -> set[str]:
    names: set[str] = set()
    for rel in rel_paths:
        path = live_root / rel
        if not path.is_file():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
    return names


def _docs_naming_symbols(docs_root: Path, symbols: set[str]) -> list[str]:
    """Docs-root-relative paths of any doc file that mentions one of ``symbols``."""
    hits: list[str] = []
    if not symbols or not docs_root.is_dir():
        return hits
    for path in sorted(docs_root.rglob("*")):
        if not path.is_file():
            continue
        if not _is_doc(_posix(str(path.relative_to(docs_root)))):
            continue
        if any(marker in path.parts for marker in (".git", "__pycache__", ".venv")):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = _posix(str(path.relative_to(docs_root)))
        if any(sym and sym in text for sym in symbols):
            hits.append(rel)
    return hits


def _related_tests(
    live_root: Path,
    changed_code: list[str],
    changed_tests: list[str],
) -> list[str]:
    """Test files that name or import a changed module, plus the changed tests."""
    targets: set[str] = set(changed_tests)
    module_stems = {Path(rel).stem for rel in changed_code if _is_python(rel)}
    if not module_stems:
        return sorted(targets)
    for path in sorted(live_root.rglob("*.py")):
        if any(part in {".venv", "__pycache__", ".git", "node_modules"} for part in path.parts):
            continue
        rel = _posix(str(path.relative_to(live_root)))
        name = path.name
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        stem_matches_module = any(
            name in {f"test_{stem}.py", f"{stem}_test.py"} for stem in module_stems
        )
        if stem_matches_module:
            targets.add(rel)
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for stem in module_stems:
            if f"import {stem}" in text or f"from {stem}" in text or f"from .{stem}" in text:
                targets.add(rel)
                break
    return sorted(targets)


def compute_diff_scope(
    live_root: Path,
    base_ref: str,
    docs_root: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
) -> DiffScope:
    """Compute the diff scope, or raise :class:`DiffScopeError` when impossible."""
    base_ref = str(base_ref or "").strip()
    if not base_ref:
        raise DiffScopeError("no base ref given")
    live_root = Path(live_root)
    try:
        inside = _git(live_root, "rev-parse", "--is-inside-work-tree").strip()
    except (DiffScopeError, FileNotFoundError, OSError) as exc:
        raise DiffScopeError(f"not a git work tree: {exc}") from exc
    if inside != "true":
        raise DiffScopeError("target is not a git work tree")
    try:
        _git(live_root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
    except DiffScopeError as exc:
        raise DiffScopeError(f"unknown base ref {base_ref!r}: {exc}") from exc

    raw = _git(live_root, "diff", "--name-only", base_ref)
    changed = sorted({_posix(line) for line in raw.splitlines() if line.strip()})
    if not changed:
        # A legitimate empty diff (base == HEAD with a clean tree) is not an
        # error; the caller simply has nothing scoped and runs nothing.
        return DiffScope(base_ref=base_ref, changed=[])
    if len(changed) > max_files:
        raise DiffScopeError(
            f"diff touches {len(changed)} files (> {max_files}); running the full cycle"
        )

    changed_code = [p for p in changed if _is_python(p) and not _is_test(p)]
    changed_tests = [p for p in changed if _is_python(p) and _is_test(p)]
    changed_docs = [p for p in changed if _is_doc(p)]

    symbols = _symbols_defined_in(changed_code, live_root)
    doc_allowlist = list(dict.fromkeys(changed_docs + _docs_naming_symbols(docs_root, symbols)))

    nova_targets = _related_tests(live_root, changed_code, changed_tests)
    changed_lines = compute_changed_lines(live_root, base_ref, changed_code)
    uncovered_files = _find_uncovered_changed_files(live_root, changed_code, nova_targets)

    return DiffScope(
        base_ref=base_ref,
        changed=changed,
        changed_code=changed_code,
        changed_tests=changed_tests,
        changed_docs=changed_docs,
        lexi_docs_allowlist=doc_allowlist,
        riley_sable_paths=changed,
        nova_test_targets=nova_targets,
        changed_lines=changed_lines,
        uncovered_changed_files=uncovered_files,
    )


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def compute_changed_lines(
    live_root: Path,
    base_ref: str,
    changed_files: list[str],
) -> dict[str, list[int]]:
    """Return 1-based changed line numbers per file in ``changed_files`` vs ``base_ref``."""
    out: dict[str, list[int]] = {}
    for rel in changed_files:
        try:
            diff_text = _git(live_root, "diff", "--unified=0", base_ref, "--", rel)
        except Exception:
            continue
        lines: list[int] = []
        for raw_line in diff_text.splitlines():
            m = _HUNK_RE.match(raw_line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                lines.extend(range(start, start + max(count, 0)))
        out[rel] = sorted(set(lines))
    return out


def _find_uncovered_changed_files(
    live_root: Path,
    changed_code: list[str],
    nova_targets: list[str],
) -> list[str]:
    """Return changed Python source files that have no related test in ``nova_targets``."""
    uncovered: list[str] = []
    target_texts: list[tuple[str, str]] = []
    for t_rel in nova_targets:
        t_path = live_root / t_rel
        if t_path.is_file():
            try:
                target_texts.append((t_path.name, t_path.read_text(encoding="utf-8", errors="replace")))
            except OSError:
                pass
    for rel in changed_code:
        stem = Path(rel).stem
        covered = any(
            any(
                (
                    t_name in {f"test_{stem}.py", f"{stem}_test.py"},
                    f"import {stem}" in t_body,
                    f"from {stem}" in t_body,
                    f"from .{stem}" in t_body,
                )
            )
            for t_name, t_body in target_texts
        )
        if not covered:
            uncovered.append(rel)
    return sorted(uncovered)


def _extract_py_test_names(content: str) -> set[str]:
    names: set[str] = set()
    try:
        tree = ast.parse(content)
    except Exception:
        return names
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_") or node.name.endswith("_test"):
                names.add(node.name)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test"):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name.startswith("test_"):
                            names.add(f"{node.name}::{item.name}")
    return names


def _extract_ts_test_names(content: str) -> set[str]:
    names: set[str] = set()
    for m in re.finditer(r"(?:it|test)\s*(?:\.\w+)?\s*\(\s*['\"`]([^'\"`]+)['\"`]", content):
        names.add(m.group(1))
    return names


def detect_deleted_tests(live_root: Path, base_ref: str) -> list[str]:
    """In --diff mode, detect if test files or individual tests were removed versus base_ref."""
    removed: list[str] = []
    try:
        raw = _git(live_root, "diff", "--name-status", base_ref)
    except Exception:
        return removed

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        status, rel_path = parts[0], _posix(parts[1])
        if not _is_test(rel_path):
            continue

        if status.startswith("D"):
            try:
                base_content = _git(live_root, "show", f"{base_ref}:{rel_path}")
                if _is_python(rel_path):
                    tests = _extract_py_test_names(base_content)
                else:
                    tests = _extract_ts_test_names(base_content)
                if tests:
                    for t in sorted(tests):
                        removed.append(f"{rel_path}::{t}")
                else:
                    removed.append(rel_path)
            except Exception:
                removed.append(rel_path)
        elif status.startswith("M"):
            try:
                base_content = _git(live_root, "show", f"{base_ref}:{rel_path}")
                curr_path = live_root / rel_path
                curr_content = curr_path.read_text(encoding="utf-8", errors="replace") if curr_path.is_file() else ""
                if _is_python(rel_path):
                    base_tests = _extract_py_test_names(base_content)
                    curr_tests = _extract_py_test_names(curr_content)
                else:
                    base_tests = _extract_ts_test_names(base_content)
                    curr_tests = _extract_ts_test_names(curr_content)
                diff_tests = base_tests - curr_tests
                for t in sorted(diff_tests):
                    removed.append(f"{rel_path}::{t}")
            except Exception:
                pass
    return sorted(removed)


def _count_py_fn_assertions(fn_node: ast.AST) -> int:
    count = 0
    for sub in ast.walk(fn_node):
        if isinstance(sub, ast.Assert):
            count += 1
        elif isinstance(sub, ast.Call):
            func_str = ast.unparse(sub.func) if hasattr(ast, "unparse") else ""
            if any(
                (
                    func_str.endswith("raises"),
                    func_str.endswith("warns"),
                    "assert" in func_str.lower(),
                )
            ):
                count += 1
    return count


def _count_py_assertions_by_test(content: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        tree = ast.parse(content)
    except Exception:
        return counts
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any((node.name.startswith("test_"), node.name.endswith("_test"))):
                counts[node.name] = _count_py_fn_assertions(node)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("Test"):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name.startswith("test_"):
                            counts[f"{node.name}::{item.name}"] = _count_py_fn_assertions(item)
    return counts


def _count_ts_assertions_by_test(content: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    matches = list(re.finditer(r"(?:it|test)\s*(?:\.\w+)?\s*\(\s*['\"`]([^'\"`]+)['\"`]", content))
    for m, nxt in zip(matches, [*matches[1:], None]):
        name = m.group(1)
        start = m.end()
        end = nxt.start() if nxt is not None else len(content)
        segment = content[start:end]
        counts[name] = len(re.findall(r"\b(?:expect|assert)\s*(?:\(|\.)", segment))
    return counts


def detect_weakened_tests(live_root: Path, base_ref: str) -> list[str]:
    """Detect tests in modified test files whose assertion count decreased vs ``base_ref``."""
    weakened: list[str] = []
    try:
        raw = _git(live_root, "diff", "--name-status", base_ref)
    except Exception:
        return weakened

    for line in raw.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        status, rel_path = parts[0], _posix(parts[1])
        if not all((status.startswith("M"), _is_test(rel_path))):
            continue
        curr_path = live_root / rel_path
        if not curr_path.is_file():
            continue
        try:
            base_content = _git(live_root, "show", f"{base_ref}:{rel_path}")
            curr_content = curr_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        if _is_python(rel_path):
            base_counts = _count_py_assertions_by_test(base_content)
            curr_counts = _count_py_assertions_by_test(curr_content)
        else:
            base_counts = _count_ts_assertions_by_test(base_content)
            curr_counts = _count_ts_assertions_by_test(curr_content)
        for test_name in sorted(base_counts):
            if test_name in curr_counts:
                b_cnt = base_counts[test_name]
                c_cnt = curr_counts[test_name]
                if c_cnt < b_cnt:
                    weakened.append(
                        f"{rel_path}::{test_name} (assertions reduced from {b_cnt} to {c_cnt})"
                    )
    return sorted(weakened)

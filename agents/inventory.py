"""Read-only inventory of docs, code, and tests. Used by Lexi/Aria heuristics."""

from __future__ import annotations

import ast
import re
from pathlib import Path

DOC_GLOBS = ("**/*.md", "**/*.rst", "**/*.txt")
CODE_SKIP = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "node_modules",
    ".pytest_cache",
    "reports",
}


def _iter_files(root: Path, globs: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    for pattern in globs:
        for path in root.glob(pattern):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(root.resolve()).parts
            except ValueError:
                rel_parts = path.parts
            if any(part in CODE_SKIP for part in rel_parts):
                continue
            files.append(path)
    return sorted(set(files))


def list_docs(docs_root: Path, globs: tuple[str, ...] | None = None) -> list[Path]:
    if docs_root.is_file():
        return [docs_root]
    return _iter_files(docs_root, globs or DOC_GLOBS)


def list_python(root: Path) -> list[Path]:
    files: list[Path] = []
    if not root.exists():
        return files
    root = root.resolve()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if any(part in CODE_SKIP for part in rel.parts):
            continue
        files.append(path)
    return sorted(files)


def split_code_and_tests(files: list[Path], root: Path) -> tuple[list[Path], list[Path]]:
    tests: list[Path] = []
    code: list[Path] = []
    for path in files:
        rel = path.relative_to(root)
        parts = {p.lower() for p in rel.parts}
        name = path.name.lower()
        if "tests" in parts or "test" in parts or name.startswith("test_") or name.endswith("_test.py"):
            tests.append(path)
        else:
            code.append(path)
    return code, tests


def public_names(py_file: Path) -> list[str]:
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            names.append(node.name)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            names.append(node.name)
    return names


def doc_tokens(docs_root: Path, limit_chars: int = 12_000) -> tuple[list[str], str]:
    chunks: list[str] = []
    size = 0
    identifiers: list[str] = []
    ident_re = re.compile(r"`([A-Za-z_][A-Za-z0-9_\.]*)`")
    heading_re = re.compile(r"^#+\s+(.+)$", re.MULTILINE)
    for path in list_docs(docs_root):
        text = path.read_text(encoding="utf-8", errors="replace")
        identifiers.extend(ident_re.findall(text))
        identifiers.extend(heading_re.findall(text))
        piece = f"## {path.name}\n{text}\n"
        if size + len(piece) > limit_chars:
            remain = limit_chars - size
            if remain > 0:
                chunks.append(piece[:remain])
            break
        chunks.append(piece)
        size += len(piece)
    cleaned = []
    for token in identifiers:
        token = token.strip()
        if token:
            cleaned.append(token)
    return cleaned, "".join(chunks)


def relative_strings(paths: list[Path], root: Path) -> list[str]:
    base = root if root.is_dir() else root.parent
    out: list[str] = []
    for path in paths:
        try:
            out.append(str(path.relative_to(base)))
        except ValueError:
            out.append(str(path))
    return out

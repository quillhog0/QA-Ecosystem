"""Read-only inventory of docs, code, and tests. Used by Lexi/Aria heuristics."""

from __future__ import annotations

import ast
import re
from pathlib import Path

DOC_GLOBS = ("**/*.md", "**/*.rst", "**/*.txt")
# Vendored, generated or tool-owned directories never count as project source
# or project documentation: their content belongs to a dependency, a build
# product or an editor. Skipping them here is a hard rule of the scan, not an
# operator preference, because a false "the code has it" hit inside
# ``node_modules`` or ``dist`` would silence a real drift finding.
CODE_SKIP = {
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".eggs",
    ".idea",
    ".vscode",
    "dist",
    "build",
    "target",
    "out",
    "coverage",
    "htmlcov",
    "reports",
}
# Editor / packaging backups and swap files. None of them is a live doc: they
# are always alongside the real file (``README.md.bak``) and reading them would
# resurface terms the project already deleted.
BACKUP_DOC_SUFFIXES = (".bak", ".orig", ".rej", ".accept", ".merge", "~")
# Extensions the code index reads. Python plus the everyday web languages: a
# symbol defined in a ``.ts`` file is real project code just as much as one
# defined in a ``.py`` file, so Lexi must not call it missing.
SOURCE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".mjs", ".cjs", ".jsx"})


def is_backup_doc(path: Path) -> bool:
    """True when *path* is a backup / merge artifact rather than a live doc."""
    name = path.name.lower()
    for suffix in BACKUP_DOC_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def _in_skip_dir(parts: tuple[str, ...]) -> bool:
    return any(part in CODE_SKIP for part in parts)


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
            if _in_skip_dir(rel_parts):
                continue
            if is_backup_doc(path):
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
        if _in_skip_dir(rel.parts):
            continue
        files.append(path)
    return sorted(files)


def list_sources(root: Path) -> list[Path]:
    """Every indexed source file: Python plus the web-language extensions."""
    files: list[Path] = []
    if not root.exists():
        return files
    root = root.resolve()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        rel = path.relative_to(root)
        if _in_skip_dir(rel.parts):
            continue
        files.append(path)
    return sorted(files)


def _is_test_path(path: Path, rel_parts_lower: set[str]) -> bool:
    """One test file, in either ecosystem: pytest naming or JS co-located tests."""
    name = path.name.lower()
    if "tests" in rel_parts_lower or "test" in rel_parts_lower:
        return True
    if "__tests__" in rel_parts_lower:
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    if ".test." in name or ".spec." in name:
        return True
    return False


def split_code_and_tests(files: list[Path], root: Path) -> tuple[list[Path], list[Path]]:
    tests: list[Path] = []
    code: list[Path] = []
    for path in files:
        rel = path.relative_to(root)
        parts_lower = {p.lower() for p in rel.parts}
        if _is_test_path(path, parts_lower):
            tests.append(path)
        else:
            code.append(path)
    return code, tests


# JavaScript / TypeScript declaration shapes that name a real project symbol.
# Deliberately narrow: exported declarations, function/class/interface/type/enum
# names, ``const`` bindings and method signatures. Random identifiers in a .ts
# file are not code evidence of a *symbol*; those still need _other_sources_text.
_TS_NAME_RE = re.compile(
    r"\b(?:function|class|interface|type|enum|namespace)\s+"
    r"([A-Za-z_$][\w$]*)"
)
_TS_CONST_RE = re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*[=:]")
_TS_METHOD_RE = re.compile(r"^[ \t]*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(")
_TS_RESERVED = frozenset({"if", "for", "while", "switch", "catch", "return", "function",
                         "class", "new", "await", "typeof", "delete", "export", "import"})


def _public_names_ts(text: str) -> list[str]:
    """Symbol names a JS/TS file declares at module or class scope."""
    names: list[str] = []
    seen: set[str] = set()
    for match in _TS_NAME_RE.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("/*"):
            continue
        for regex in (_TS_CONST_RE, _TS_METHOD_RE):
            m = regex.search(line)
            if m:
                name = m.group(1)
                if name in seen:
                    continue
                if name in _TS_RESERVED:
                    continue
                seen.add(name)
                names.append(name)
    # ``export { a, b as c, d }`` - the public names are a, c, d (alias wins).
    for match in re.finditer(r"export\s*\{([^}]+)\}", text):
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if not piece:
                continue
            ident = piece.split(" as ")[-1].strip() if " as " in piece else piece
            if re.fullmatch(r"[A-Za-z_$][\w$]*", ident) and ident not in seen:
                seen.add(ident)
                names.append(ident)
    return names


def public_names(py_file: Path) -> list[str]:
    suffix = py_file.suffix.lower()
    if suffix in {".ts", ".tsx", ".mjs", ".cjs", ".jsx"}:
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return _public_names_ts(text)
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8-sig", errors="replace"))
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

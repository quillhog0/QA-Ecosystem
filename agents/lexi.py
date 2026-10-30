"""Lexi — librarian. Docs vs code vs tests. VETO on drift."""

from __future__ import annotations

import builtins
import re
import ast
import tomllib
from pathlib import Path
import tokenize
import io
from typing import Any

import yaml

from agents.adapters import complete, complete_with_status, format_llm_error, load_prompt, parse_yaml_block
from agents.inventory import (
    DOC_GLOBS,
    doc_tokens,
    list_docs,
    list_python,
    public_names,
    relative_strings,
    split_code_and_tests,
)
from core.packets import DriftFinding, DriftReport, Verdict

BACKTICK_RE = re.compile(r"`([^`]+)`")
FUNC_LIKE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\(\))?$")
FENCE_RE = re.compile(r"```([^\n]*)\n(.*?)```", re.DOTALL)
CLI_FLAG_RE = re.compile(r"--[A-Za-z][A-Za-z0-9_-]*")

COMMON_WORDS = {
    "readme",
    "overview",
    "usage",
    "api",
    "tests",
    "testing",
    "python",
    "pytest",
    "true",
    "false",
    "none",
    "coverage",
    "clear",
    "veto",
    "block",
    "escalate",
    "docs",
    "code",
    "config",
    "report",
    "reports",
    "project",
    "local",
    "openrouter",
    "heuristic",
    "heuristics",
}

COMMON_HEADINGS = COMMON_WORDS | {"behavior", "sample", "calculator"}


def _flatten_keys(obj: Any, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = str(key)
            keys.add(name)
            dotted = f"{prefix}.{name}" if prefix else name
            keys.add(dotted)
            keys |= _flatten_keys(value, dotted)
    return keys


def _yaml_fence_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for lang, block in FENCE_RE.findall(text):
        keys |= set(CLI_FLAG_RE.findall(block))
        if not re.search(r"ya?ml", lang, re.I) and lang.strip():
            continue
        try:
            loaded = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        keys |= _flatten_keys(loaded)
    keys |= set(CLI_FLAG_RE.findall(text))
    return keys


def _imported_names(project_root: Path) -> set[str]:
    names: set[str] = set()
    for path in project_root.rglob("*.py"):
        if any(
            part in path.parts
            for part in [".venv", "venv", ".git", "node_modules", "__pycache__"]
        ):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name.split(".")[0])
                    if alias.asname:
                        names.add(alias.asname)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module.split(".")[0])
                for alias in node.names:
                    names.add(alias.name)
                    if alias.asname:
                        names.add(alias.asname)
    return names


def _collect_code_text_content(project_root: Path) -> str:
    content = []
    for path in project_root.rglob("*.py"):
        if any(
            part in path.parts
            for part in [".venv", "venv", ".git", "node_modules", "__pycache__"]
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            # Collect comments
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type == tokenize.COMMENT:
                    content.append(tok.string)
            # Collect docstrings
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module, ast.AsyncFunctionDef)):
                    doc = ast.get_docstring(node)
                    if doc:
                        content.append(doc)
        except (SyntaxError, UnicodeDecodeError, OSError, ValueError):
            continue
    return "\n".join(content)


def _ignore_identifiers(
    *,
    config: dict[str, Any],
    docs_root: Path,
    project_root: Path,
    doc_globs: tuple[str, ...],
) -> set[str]:
    ignore: set[str] = set(COMMON_WORDS)
    ignore |= _flatten_keys(config)
    extra = (config.get("lexi") or {}).get("ignore_identifiers") or []
    ignore |= {str(item) for item in extra}
    # Keys from the config.yaml of the project under test are deliberately not
    # ignored here: a tested project must not be able to hide drift. A key that
    # code really reads is still found via string literals and other sources.
    for path in list_docs(docs_root, globs=doc_globs):
        try:
            ignore |= _yaml_fence_keys(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return ignore


def _backtick_functions(docs_root: Path, doc_globs: tuple[str, ...]) -> list[str]:
    names: list[str] = []
    for path in list_docs(docs_root, globs=doc_globs):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        stripped = re.sub(r"```[^\n]*\n.*?```", "", text, flags=re.DOTALL)
        for raw in BACKTICK_RE.findall(stripped):
            if re.search(r"\s", raw) or "/" in raw or "." in raw:
                continue
            piece = raw.strip()
            if not piece:
                continue
            match = FUNC_LIKE_RE.match(piece)
            if not match:
                continue
            names.append(match.group(1))
            if piece.endswith(")"):
                _CALL_FORM.add(match.group(1))
    return names


_OTHER_SOURCE_EXT = {
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".sh",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".sql",
}
_CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "docs", "dist", "build"}


def _other_sources_text(project_root: Path, limit_bytes: int = 5_000_000) -> str:
    """Text of non-doc project files (other languages, config, tests)."""
    chunks: list[str] = []
    total = 0
    for path in sorted(project_root.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.relative_to(project_root).parts[:-1]):
            continue
        if path.suffix.lower() not in _OTHER_SOURCE_EXT or not path.is_file():
            continue
        if path.suffix.lower() in _CONFIG_EXT:
            # Config files of the project under test are not evidence: a key
            # only counts if code actually reads it (see _collect_code_strings).
            continue
        try:
            size = path.stat().st_size
            if size > 1_000_000 or total + size > limit_bytes:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            chunks.append(text)
            total += size
        except OSError:
            continue
    return "\n".join(chunks)


def _code_shaped(ident: str) -> bool:
    """snake_case, UPPER_SNAKE or camelCase names look like code, plain words do not."""
    return "_" in ident.strip("_") or bool(re.search(r"[a-z][A-Z]", ident))


_CALL_FORM: set[str] = set()


EMOJI_START_RE = re.compile(r"^[\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff\u2b50\u2b55\u200d\ufe0f\s]+")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
CLAIM_KEYWORDS = {
    "always", "never", "must", "guaranteed", "only",
    "vždy", "nikdy", "musí", "pouze", "zaručeně",
}

LABELED_BULLET_RE = re.compile(r"^\s*[-*+]?\s*\*\*[^*]+?(\*\*:|\:\*\*)\s*")

def _clean_and_extract_claims_from_text(text: str) -> list[str]:
    stripped_text = FENCE_RE.sub("", text)
    lines = stripped_text.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        is_bullet = bool(re.match(r"^(\s*[-*+]\s+|\s*\d+\.\s+)", line))
        is_heading = bool(re.match(r"^\s*#+", line))
        if (is_bullet or is_heading) and current:
            blocks.append(" ".join(current))
            current = []
        current.append(trimmed)
    if current:
        blocks.append(" ".join(current))

    claims: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        is_labeled = bool(LABELED_BULLET_RE.search(block))
        block = re.sub(r"^\s*#+\s*", "", block)
        block = re.sub(r"^(\s*[-*+]\s+|\s*\d+\.\s+)", "", block)
        block = EMOJI_START_RE.sub("", block)
        block = block.replace("**", "").replace("__", "")
        block = re.sub(r"(?<!\w)[*_](.*?)[*_](?!\w)", r"\1", block)
        block = block.replace("*", "").strip()
        if not block:
            continue
        sentences = SENTENCE_SPLIT_RE.split(block)
        for idx, s in enumerate(sentences):
            s = s.strip()
            if not s:
                continue
            has_kw = any(re.search(r"\b" + re.escape(kw) + r"\b", s, re.IGNORECASE) for kw in CLAIM_KEYWORDS)
            if not has_kw and not (is_labeled and idx == 0):
                continue
            if len(s) > 200:
                match = list(re.finditer(r"[.!?](?:\s|$)", s[:200]))
                if match:
                    s = s[:match[-1].end()].strip()
                else:
                    last_space = s[:200].rfind(" ")
                    if last_space > 50:
                        s = s[:last_space].strip() + "..."
                    else:
                        s = s[:200].strip()
            if s and s not in seen:
                claims.append(s)
                seen.add(s)
    return claims


def _extract_claims(docs_root: Path, doc_globs: tuple[str, ...]) -> list[str]:
    claims: list[str] = []
    seen_claims: set[str] = set()
    for path in list_docs(docs_root, globs=doc_globs):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for claim in _clean_and_extract_claims_from_text(text):
            if claim not in seen_claims:
                claims.append(claim)
                seen_claims.add(claim)
        if len(claims) >= 50:
            break
    return claims[:50]


def _collect_code_ast_terms(code_files: list[Path]) -> tuple[set[str], set[str]]:
    all_identifiers: set[str] = set()
    function_names: set[str] = set()
    for path in code_files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                function_names.add(node.name)
                all_identifiers.add(node.name)
            elif isinstance(node, ast.ClassDef):
                all_identifiers.add(node.name)
            elif isinstance(node, ast.Name):
                all_identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                all_identifiers.add(node.attr)
    return all_identifiers, function_names


def _collect_code_strings(code_files: list[Path], project_root: Path) -> set[str]:
    """Every exact str constant in the given code files.

    A string literal used as a value or as a dict key is captured here because
    both are ``ast.Constant`` nodes with a ``str`` payload. Lexi uses this set
    to avoid flagging a docs term as ``missing_code`` when the term exists in
    code only as a literal (for example an env-var fragment in a policy tuple,
    or a CLI binary name). Tests are excluded by the caller via
    ``split_code_and_tests``; any stray ``.py`` living under a ``docs`` /
    ``doc`` directory is skipped here.
    """
    values: set[str] = set()
    for path in code_files:
        try:
            rel_parts = path.relative_to(project_root).parts[:-1]
        except ValueError:
            rel_parts = path.parts[:-1]
        if any(part.lower() in {"docs", "doc"} for part in rel_parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                values.add(node.value)
    return values


_PEP508_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
_PEP503_SEP_RE = re.compile(r"[-_.]+")


def _pep503_name(name: str) -> str:
    """PEP 503 package-name normalization: lowercase and collapse ``[-_.]+``."""
    return _PEP503_SEP_RE.sub("-", name.strip().lower())


def _requirement_line_name(line: str) -> str | None:
    """Extract and normalize the distribution name from one pip requirement.

    Strips trailing ``# comment``, environment markers (``;``-suffix), version
    specifiers and extras (``[...]``), and returns the PEP 503 normalized form.
    Returns ``None`` for blank / comment-only / pip-option (``-r``, ``-e``,
    ``--find-links``, ...) or continuation (``\\``) lines and for anything that
    does not begin with a valid PEP 508 name. URL and VCS requirement forms are
    intentionally not parsed here; they will simply not match a plain docs
    term, which is a safe under-approximation.
    """
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    # Environment markers come after the version specifier; drop them first so
    # a name like ``foo; python_version<'3.11'`` is reduced to just ``foo``.
    raw = raw.split(";", 1)[0].strip()
    # Inline comments must be whitespace-preceded per pip's own parser.
    raw = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
    if not raw or raw.startswith("-") or raw.endswith("\\"):
        return None
    match = _PEP508_NAME_RE.match(raw)
    if not match:
        return None
    return _pep503_name(match.group(1))


def _declared_package_names(project_root: Path) -> set[str]:
    """PEP 503 normalized distribution names declared by the project under test.

    Reads every ``requirements*.txt`` at the project root and every dependency
    list under ``[project]`` / ``[project.optional-dependencies]`` in
    ``pyproject.toml``. Missing or broken files yield nothing rather than
    raising, so Lexi can never crash on an unparseable manifest: an empty set
    simply disables the declared-package skip.
    """
    names: set[str] = set()
    try:
        req_files = sorted(
            p for p in project_root.glob("requirements*.txt") if p.is_file()
        )
    except OSError:
        req_files = []
    for path in req_files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            name = _requirement_line_name(line)
            if name:
                names.add(name)
    try:
        data = tomllib.loads(
            (project_root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
        )
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        # FileNotFoundError, missing/unreadable file, invalid UTF-8, or a
        # malformed TOML body — all treated as "no declared packages" here.
        data = None
    if isinstance(data, dict):
        project = data.get("project")
        if isinstance(project, dict):
            deps = project.get("dependencies")
            if isinstance(deps, list):
                for item in deps:
                    if isinstance(item, str):
                        name = _requirement_line_name(item)
                        if name:
                            names.add(name)
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for group in optional.values():
                    if not isinstance(group, list):
                        continue
                    for item in group:
                        if isinstance(item, str):
                            name = _requirement_line_name(item)
                            if name:
                                names.add(name)
    return names


def _is_code_term(ident: str, all_identifiers: set[str], function_names: set[str]) -> bool:
    if ident in all_identifiers:
        return True
    snake = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", ident).lower()
    upper = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", ident).upper()
    variants = {ident, snake, upper, ident.lower(), ident.upper()}
    if any(v in all_identifiers for v in variants):
        return True
    if snake and len(snake) >= 3:
        for fn in function_names:
            if snake in fn.lower():
                return True
    return False


def _heuristic_drift(
    *,
    docs_root: Path,
    project_root: Path,
    config: dict[str, Any],
    execution: dict[str, Any] | None = None,
) -> DriftReport:
    py_files = list_python(project_root)
    code_files, test_files = split_code_and_tests(py_files, project_root)
    tokens, _excerpt = doc_tokens(docs_root)
    findings: list[DriftFinding] = []
    doc_globs = tuple((config.get("lexi") or {}).get("doc_globs") or DOC_GLOBS)
    ignore = _ignore_identifiers(
        config=config, docs_root=docs_root, project_root=project_root, doc_globs=doc_globs
    )
    ignore_lower = {item.lower() for item in ignore}

    imported_names = _imported_names(project_root)
    code_text_content = _collect_code_text_content(project_root)
    all_code_identifiers, function_names = _collect_code_ast_terms(code_files)
    code_strings = _collect_code_strings(code_files, project_root)
    declared_packages = _declared_package_names(project_root)

    code_index: dict[str, str] = {}
    for path in code_files:
        rel = str(path.relative_to(project_root))
        stem = path.stem
        code_index[stem] = rel
        for name in public_names(path):
            code_index[name] = rel

    test_text = ""
    for path in test_files:
        try:
            test_text += path.read_text(encoding="utf-8", errors="replace") + "\n"
        except OSError:
            continue

    # Backticked doc terms often name accounts, config keys or CLI values, so a
    # missing name is a warning by default and a veto only in strict mode.
    strict_missing = bool((config.get("lexi") or {}).get("veto_on_missing_code", False))
    _CALL_FORM.clear()
    other_text: str | None = None
    # A missing name used in a promise ("`x` must ...") or written as a call
    # is real drift; other backticked words are only a warning.
    claim_text = " ".join(_extract_claims(docs_root, doc_globs))
    seen: set[str] = set()
    for ident in _backtick_functions(docs_root, doc_globs):
        if ident in seen:
            continue
        seen.add(ident)
        if ident in ignore or ident.lower() in ignore_lower:
            continue
        if ident in dir(builtins):
            continue
        if ident.isupper() and "_" not in ident:
            continue
        if ident.istitle() and ident.lower() in COMMON_WORDS | {
            "writes",
            "runs",
            "walks",
            "see",
            "head",
        }:
            continue
        if ident in imported_names:
            continue
        if ident in code_index:
            continue
        if _is_code_term(ident, all_code_identifiers, function_names):
            continue
        if ident in code_text_content:
            findings.append(
                DriftFinding(
                    kind="domain_term",
                    path=ident,
                    message=f"docs mention `{ident}` but it appears only in comments/docstrings, not as code",
                    severity="low",
                )
            )
            continue
        if ident in code_strings:
            # The term exists verbatim as a string literal or dict key in a
            # project .py file (excluding tests and docs) — that is not drift.
            continue
        if _pep503_name(ident) in declared_packages:
            # The term names a declared dependency (requirements*.txt or
            # pyproject.toml ``[project]`` / ``optional-dependencies``) — that
            # is not drift either.
            continue
        if other_text is None:
            other_text = _other_sources_text(project_root)
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])", other_text):
            continue
        findings.append(
            DriftFinding(
                kind="missing_code",
                path=ident,
                message=f"docs mention `{ident}` but no matching module or public name exists in code",
                severity="high" if (strict_missing or ident in _CALL_FORM or ident in claim_text or _code_shaped(ident)) else "medium",
            )
        )

    for token in tokens:
        piece = token.split()[0]
        if piece.lower() in COMMON_HEADINGS:
            continue

    if not test_files:
        findings.append(
            DriftFinding(
                kind="missing_tests",
                path="tests",
                message="no pytest files found in the isolated project",
                severity="high",
            )
        )
    else:
        public: list[tuple[str, str]] = []
        for path in code_files:
            rel = str(path.relative_to(project_root))
            for name in public_names(path):
                public.append((name, rel))
        mentioned = 0
        for name, rel in public:
            if name in test_text:
                mentioned += 1
        if public and mentioned == 0:
            findings.append(
                DriftFinding(
                    kind="missing_tests",
                    path=public[0][1],
                    message="public names exist in code but tests never reference them",
                    severity="medium",
                )
            )

    if execution:
        collected = int(execution.get("collected") or 0)
        if collected <= 0:
            findings.append(
                DriftFinding(
                    kind="contradiction",
                    path="pytest",
                    message="post-audit: execution collected 0 tests while a test surface was expected",
                    severity="high",
                )
            )

    high = [f for f in findings if f.severity == "high"]
    verdict = Verdict.VETO.value if high else Verdict.CLEAR.value
    notes = "drift detected" if findings else "docs, code, and tests align at identifier level"
    claims = _extract_claims(docs_root, doc_globs)
    if findings and not high:
        notes = "minor gaps only; not a veto"
    return DriftReport(
        verdict=verdict,
        findings=findings,
        docs_files=relative_strings(list_docs(docs_root, globs=doc_globs), docs_root)
        if docs_root.exists()
        else [],
        code_files=relative_strings(code_files, project_root),
        test_files=relative_strings(test_files, project_root),
        notes=notes,
        claims=claims,
    )


def _from_llm(data: dict[str, Any], fallback: DriftReport) -> DriftReport:
    verdict = str(data.get("verdict") or fallback.verdict).upper()
    if verdict not in {Verdict.CLEAR.value, Verdict.VETO.value}:
        verdict = fallback.verdict
    if fallback.verdict == Verdict.VETO.value:
        verdict = Verdict.VETO.value
    findings = []
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        findings.append(
            DriftFinding(
                kind=str(item.get("kind") or "stale_docs"),
                path=str(item.get("path") or ""),
                message=str(item.get("message") or ""),
                severity=str(item.get("severity") or "medium"),
            )
        )
    combined_findings = list(fallback.findings)
    existing_messages = {f.message for f in combined_findings}
    for f in findings:
        if f.message not in existing_messages:
            combined_findings.append(f)
            existing_messages.add(f.message)
    return DriftReport(
        verdict=verdict,
        findings=combined_findings if fallback.findings else (findings or fallback.findings),
        docs_files=fallback.docs_files,
        code_files=fallback.code_files,
        test_files=fallback.test_files,
        notes=str(data.get("notes") or fallback.notes),
        claims=fallback.claims,
    )


def inspect(
    *,
    docs_root: Path,
    project_root: Path,
    config: dict[str, Any],
    execution: dict[str, Any] | None = None,
) -> DriftReport:
    report = _heuristic_drift(
        docs_root=docs_root,
        project_root=project_root,
        config=config,
        execution=execution,
    )
    payload = {
        "docs_files": report.docs_files[:80],
        "code_files": report.code_files[:80],
        "test_files": report.test_files[:80],
        "heuristic": report.to_dict(),
        "execution": execution or {},
    }
    raw = None
    llm_status = "none"
    try:
        raw, llm_status = complete_with_status(
            system_prompt=load_prompt("lexi.md"),
            user_payload=payload,
            config=config,
        )
    except Exception as exc:
        llm_status = format_llm_error(exc)
        raw = None

    report.llm_status = llm_status
    if not raw:
        return report
    parsed = parse_yaml_block(raw)
    if not parsed:
        report.llm_status = "fallback: invalid YAML"
        return report
    result = _from_llm(parsed, report)
    result.llm_status = "ok"
    return result

"""Lexi — librarian. Docs vs code vs tests. VETO on drift."""

from __future__ import annotations

import builtins
import re
import ast
import sys
import tomllib
import unicodedata
from pathlib import Path
import tokenize
import io
from typing import Any

import yaml

from agents.adapters import complete_with_status, format_llm_error, load_prompt, parse_yaml_block
from agents.inventory import (
    CODE_SKIP,
    DOC_GLOBS,
    doc_tokens,
    list_docs,
    list_sources,
    public_names,
    relative_strings,
    split_code_and_tests,
)
from core.environment import classify_execution
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
        try:
            rel_parts = path.relative_to(project_root).parts[:-1]
        except ValueError:
            rel_parts = path.parts[:-1]
        if any(part in CODE_SKIP for part in rel_parts):
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
    ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".css", ".scss", ".sh",
    ".bash", ".zsh", ".fish", ".ps1",
    ".yaml", ".yml", ".json", ".toml", ".ini", ".cfg", ".conf", ".properties",
    ".service", ".socket", ".timer", ".hcl", ".tf", ".proto",
    ".graphql", ".sql", ".vue", ".svelte", ".php", ".rb", ".go", ".rs", ".java",
    ".kt", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".pyi",
}
_CONFIG_EXT = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
# Vendored / generated / tool-owned directories never count as code evidence.
# The set is inherited from the inventory (the same rule the doc scan uses),
# plus ``docs`` itself - a doc file cannot prove a docs promise about code.
_SKIP_DIRS = CODE_SKIP | {"docs"}


def _iter_other_sources(
    project_root: Path,
    limit_bytes: int = 5_000_000,
    extra_roots: list[Path] | None = None,
) -> list[tuple[str, str]]:
    """``(rel_path, text)`` for every non-doc project file scanned.

    The isolated copy is the primary and only code evidence: the isolation step
    expands untracked directories file by file, and anything it could not carry
    is a reported skip (isolation.skipped + WARN), not something Lexi re-reads
    from the live root. ``extra_roots`` stays available for callers that must
    widen the scan deliberately. Vendored / generated directories are skipped
    on every path so a bundled dependency's identifier cannot silence a real
    drift.
    """
    roots: list[Path] = []
    seen_roots: set[Path] = set()
    for candidate in [project_root, *(extra_roots or [])]:
        if candidate is None:
            continue
        try:
            resolved = Path(candidate).resolve()
        except OSError:
            continue
        if not resolved.exists() or resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        roots.append(resolved)
    collected: list[tuple[str, str]] = []
    total = 0
    seen_rel: set[str] = set()
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part in _SKIP_DIRS for part in rel.parts[:-1]):
                continue
            if path.suffix.lower() not in _OTHER_SOURCE_EXT:
                continue
            if path.suffix.lower() in _CONFIG_EXT and "workflows" not in path.parts:
                # Config files of the project under test are not evidence: a key
                # only counts if code actually reads it (see _collect_code_strings).
                # CI pipeline definitions (a ``workflows/`` directory, the universal
                # GitHub Actions / GitLab CI layout) are the exception: a workflow
                # file *is* the executable that declares its own job inputs, so a doc
                # naming such an input is describing real code, not missing code.
                continue
            key = rel.as_posix()
            if key in seen_rel:
                continue
            try:
                size = path.stat().st_size
                if size > 1_000_000 or total + size > limit_bytes:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            seen_rel.add(key)
            collected.append((key, text))
            total += size
    return collected


def _other_sources_text(
    project_root: Path,
    limit_bytes: int = 5_000_000,
    extra_roots: list[Path] | None = None,
) -> str:
    """Text of non-doc project files (other languages, config, tests)."""
    return "\n".join(text for _rel, text in _iter_other_sources(project_root, limit_bytes, extra_roots))


def _code_shaped(ident: str) -> bool:
    """snake_case, UPPER_SNAKE or camelCase names look like code, plain words do not."""
    return "_" in ident.strip("_") or bool(re.search(r"[a-z][A-Z]", ident))


_CALL_FORM: set[str] = set()


# ---------------------------------------------------------------------------
# Prose versus code.
#
# Lexi may only escalate a missing docs term when the documentation uses the
# term *as code*. A word that a document merely lists, glosses in parentheses,
# or forbids is prose, in any language, and must never reach the veto line.
# Every test below is structural - it looks at what surrounds the backticks,
# whether the line is a list item, and which heading owns it - because a
# per-language word list cannot cover "any language". The negation vocabulary
# is a deliberately small secondary signal: it only ever downgrades a finding,
# and no rule that must fire depends on it.
# ---------------------------------------------------------------------------

_NEGATION_WORDS = {
    # English
    "not", "no", "never", "without", "cannot", "cant", "dont", "doesnt", "isnt",
    "arent", "neither", "nor", "removed", "drops",
    # Czech / Slovak
    "ne", "bez", "nikdy", "zakaz", "zakazany", "zakazane", "nesmi", "zadne",
    "zadny", "vyluceno", "odmitnuto",
    # German
    "nicht", "ohne", "niemals", "keine", "kein", "verbot", "verboten",
    # Spanish / French / Italian / Portuguese
    "no", "nunca", "sin", "sans", "jamais", "interdit", "prohibido", "evite",
}

# Longer, unambiguous stems: matched at the start of a word, so they also catch
# inflected and compound forms ("forbidden", "forbids", "ForbiddenWords").
_NEGATION_STEMS = (
    "forbid", "prohibit", "banned", "avoid", "unless", "exclude", "missing",
    "zakaz", "nesmi", "vylou", "odmit", "nepouz", "neuklad",
    "unterlass", "vermeid", "verbot",
    "prohib", "interd", "evit",
)

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_STDLIB_MODULE_NAMES = frozenset(getattr(sys, "stdlib_module_names", frozenset()))
_EXCEPTION_SUFFIXES = ("Error", "Exception")
# A dotted name is one leading identifier plus one or more ".component" parts. The
# leading part must NOT be wrapped in its own "+": `(?:[A-Za-z_][A-Za-z0-9_]*)+` is
# a nested quantifier, and a long run of word characters with no following dot can
# then be partitioned exponentially - the scan of a plain docs word backtracked to
# a near-hang on a large project. This shape is linear and matches the same names.
_DOTTED_NAME_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b")
_CODE_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+(.*)$")
_SENTENCE_BREAK_RE = re.compile(r"[.!?:;]")


def _fold(text: str) -> str:
    """Lowercase and strip accents so one stem list serves any Latin script."""
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _negated(text: str) -> bool:
    """True when the text carries a negation or prohibition marker.

    Secondary signal only, and only ever in the direction of peace: the result
    can demote a finding, it can never create or escalate one.
    """
    if not text:
        return False
    for word in _WORD_RE.findall(_fold(text)):
        if word in _NEGATION_WORDS:
            return True
        if len(word) > 4 and any(word.startswith(stem) for stem in _NEGATION_STEMS):
            return True
    return False


def _has_words(gap: str) -> bool:
    """False when a gap between two backticked spans carries no word at all."""
    return bool(_WORD_RE.search(gap))


def _is_prose_phrase(token: str) -> bool:
    """True when a backticked span is NOT a single code-shaped identifier.

    ``_has_words`` answers a question about a *gap* (does it carry prose words),
    and returns True for a lone identifier such as ``add``, so it cannot tell a
    prose phrase from a name. This is the term-side predicate the mention scan,
    ``_anchored`` and ``resolvable_symbol`` need: a span with a space, a slash, a
    dot, call parentheses or any non-identifier punctuation is prose; a bare
    ``name`` (or ``name()``) is a candidate symbol, whatever language the word is
    written in.
    """
    if not token:
        return True
    raw = token.strip()
    if not raw or re.search(r"\s", raw) or "/" in raw or "." in raw:
        return True
    match = FUNC_LIKE_RE.match(raw)
    return match is None or not match.group(1)


def _mention_ident(token: str) -> str:
    """The identifier a backticked span stands for, or ``''`` when it is prose."""
    raw = (token or "").strip()
    match = FUNC_LIKE_RE.match(raw)
    return match.group(1) if match and match.group(1) else ""


class _Mention:
    """One backticked occurrence of a term, with the structure around it."""

    __slots__ = (
        "line", "heading", "intro", "is_list_item", "in_paren",
        "gap_before", "gap_after", "prev_term", "next_term", "clause_terms",
        "in_blockquote", "definition", "narrated", "historical",
        "doc_file", "doc_line",
    )

    def __init__(
        self,
        line: str,
        heading: str,
        intro: str,
        is_list_item: bool,
        in_paren: bool,
        gap_before: str,
        gap_after: str,
        prev_term: str,
        next_term: str,
        clause_terms: tuple[str, ...],
        in_blockquote: bool = False,
        definition: bool = False,
        narrated: bool = False,
        historical: bool = False,
        doc_file: str = "",
        doc_line: int = 0,
    ) -> None:
        self.line = line
        self.heading = heading
        self.intro = intro
        self.is_list_item = is_list_item
        self.in_paren = in_paren
        self.gap_before = gap_before
        self.gap_after = gap_after
        self.prev_term = prev_term
        self.next_term = next_term
        self.clause_terms = clause_terms
        self.in_blockquote = in_blockquote
        self.definition = definition
        self.narrated = narrated
        self.historical = historical
        self.doc_file = doc_file
        self.doc_line = doc_line

    def enumerated(self) -> bool:
        """Part of a run of backticked terms separated only by punctuation.

        ``1. **Label:** `alpha`, `beta`, `gamma`.`` is a vocabulary list: the
        terms are items, not APIs, whatever language the label is written in.
        """
        if self.prev_term and not _has_words(self.gap_before):
            return True
        if self.next_term and not _has_words(self.gap_after):
            return True
        return False

    def gloss(self) -> bool:
        """Sits inside a parenthetical aside, so it explains rather than promises."""
        return self.in_paren

    def aside(self) -> bool:
        """Sits in a Markdown block-quote, which quotes someone else's words.

        A ``> ...`` line reports or explains - it is not the document asserting
        its own API surface - so a term that only ever appears there is a quoted
        aside, not a promise the project must keep.
        """
        return self.in_blockquote

    def defined(self) -> bool:
        """Heads a glossary list item (``- `term`: description``), so it is being
        defined rather than promised as an API."""
        return self.definition

    def cited(self) -> bool:
        """Sits on a line that cites a ``file.ext:line`` source location.

        Such a line narrates existing code (an incident write-up, a change plan)
        rather than declaring an API, so a name only ever seen here is reported
        about, not promised.
        """
        return self.narrated

    def forbidden(self, term: str = "") -> bool:
        """Line-level negation, or a list item under a prohibiting heading/intro.

        The term's *own* name is stripped before the line is tested: an
        identifier such as ``missing_fn`` or ``error_handler`` contains a
        negation stem, but that is the symbol's name, not the sentence forbidding
        it. Only the surrounding prose may mark a mention as a prohibition.
        """
        line = self.line
        if term:
            esc = re.escape(term)
            line = re.sub(rf"`{esc}`", " ", line)
            line = re.sub(rf"(?<![A-Za-z0-9_]){esc}(?![A-Za-z0-9_])", " ", line)
        if _negated(line):
            return True
        if self.is_list_item and (_negated(self.heading) or _negated(self.intro)):
            return True
        return _negated(self.heading) and bool(self.heading) and _looks_like_rule_heading(self.heading)


def _looks_like_rule_heading(heading: str) -> bool:
    """A heading that governs a rule list ("Forbidden", "Forbidden lexicon", ...)."""
    words = _WORD_RE.findall(_fold(heading))
    return len(words) <= 8


def _strip_fences(text: str) -> str:
    """Blank out fenced code blocks while keeping the line numbering intact.

    A mention's line is an IDE anchor into the real file, so replacing a fence
    with newlines (one per removed line) is deliberate: prose scan sees no
    code, but ``splitlines()`` indices still match the file on disk.
    """

    def _blank(match: re.Match) -> str:
        return "\n" * match.group(0).count("\n")

    return FENCE_RE.sub(_blank, text)


_GLOSSARY_TAIL_RE = re.compile(r"^\s*(?:\([^)]*\))?\s*:")
# A concrete source citation - ``<file>.<ext>:<line>`` - is how an incident
# report or a change plan points at the code it is describing (``core_verify.py:
# 39``, ``rent_service.py:115-117``). A line that cites a location is *narrating*
# existing code, not declaring the project's public surface, so a name that only
# ever appears on such lines is a reference the docs are reporting about, not a
# promise the code broke. Kept deliberately strict: a real dotted extension plus
# a line number, so ordinary prose ("HTTP 500", "version 2.14") never matches.
_SOURCE_CITATION_RE = re.compile(r"\b[\w-]+\.\w+:\d+")


def _is_glossary_definition(line: str, span: Any) -> bool:
    """True when a list item *defines* the backticked term it opens with.

    ``- `DIFF_WATCH`: Analyzuj jen git diff`` is a glossary entry: the marker,
    then the term, then a colon and a plain-language description. The term is the
    thing being explained, not an API the document promises - unlike a sentence
    that merely *uses* it ("Provides `handle_user_logout` endpoint"). A bolded
    label ahead of the term disqualifies it (that prose, not the term, heads the
    line), and this never fires on a call form, which is escalated on its own.
    """
    if not _LIST_ITEM_RE.match(line):
        return False
    if not _GLOSSARY_TAIL_RE.match(line[span.end():]):
        return False
    head = _LIST_ITEM_RE.sub("", line[:span.start()]).strip()
    head = head.strip("*`").strip()
    return head == ""


# Historical record documents narrate what already happened - incident logs,
# post-mortems, changelogs, release notes, decision (ADR) logs and retrospectives.
# They cite symbols and file:line references as *reporting* on past code, never as
# a promise about the project's current public surface, so a name that appears only
# in such a document must never veto. Detection is by path shape alone (a directory
# name or a file stem), so it is project-agnostic: no allowlist of names, and a term
# that also appears in an ordinary doc is still scored on its non-historical mentions.
_HISTORICAL_DIR_NAMES = frozenset(
    {
        "incident", "incidents", "postmortem", "postmortems", "post-mortem",
        "post-mortems", "changelog", "changelogs", "history", "histories",
        "adr", "adrs", "decisions", "retrospective", "retrospectives",
        "release-notes", "releasenotes", "release_notes",
    }
)
_HISTORICAL_FILE_STEMS = frozenset(
    {
        "incident", "incidents", "postmortem", "postmortems", "post-mortem",
        "post-mortems", "changelog", "changelogs", "changes", "history",
        "histories", "releasenotes", "release-notes", "release_notes",
    }
)
_HISTORICAL_FILE_PREFIXES = (
    "changelog", "history", "postmortem", "post-mortem", "incident",
)


def _is_historical_doc(path: Path, docs_root: Path) -> bool:
    """True when *path* is a historical record document (by path shape only)."""
    try:
        rel = Path(path).resolve().relative_to(Path(docs_root).resolve())
    except (ValueError, OSError):
        rel = Path(Path(path).name)
    parts = [p.lower() for p in rel.parts]
    for directory in parts[:-1]:
        if directory in _HISTORICAL_DIR_NAMES:
            return True
    name = parts[-1] if parts else ""
    stem = name.rsplit(".", 1)[0]
    if stem in _HISTORICAL_FILE_STEMS:
        return True
    if any(stem == pre or stem.startswith((pre + "-", pre + "_")) for pre in _HISTORICAL_FILE_PREFIXES):
        return True
    return False


def _relative_doc_path(path: Path, docs_root: Path) -> str:
    """POSIX path of a doc file relative to the docs root (its name if outside)."""
    try:
        if docs_root.is_file():
            return path.name
        return path.resolve().relative_to(docs_root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.name


def _mention_location(
    ident: str, mentions: dict[str, list[_Mention]] | None
) -> tuple[str | None, int | None]:
    """Where the docs make their main promise about ``ident``, for IDE anchors.

    A live promise line (not a historical record, not a source-cited narration)
    is the sentence an operator must fix, so it wins; any located mention is
    the fallback. ``None`` pair only when no mention carried a location.
    """
    found = (mentions or {}).get(ident) or []
    fallback: tuple[str | None, int | None] = (None, None)
    for mention in found:
        if not mention.doc_file:
            continue
        if not (mention.historical or mention.narrated):
            return mention.doc_file, mention.doc_line or None
        if fallback[0] is None:
            fallback = (mention.doc_file, mention.doc_line or None)
    return fallback


def _doc_mentions(docs_root: Path, doc_globs: tuple[str, ...]) -> dict[str, list[_Mention]]:
    """Every backticked term in the docs, with the structure of each mention.

    Code fences are removed first, so a mention counted here is a mention in
    prose. Cached per docs root because Lexi runs the scan twice per cycle.
    """
    key = (str(docs_root.resolve()), doc_globs, _docs_signature(docs_root, doc_globs))
    cached = _MENTION_CACHE.get(key)
    if cached is not None:
        return cached

    mentions: dict[str, list[_Mention]] = {}
    for path in list_docs(docs_root, globs=doc_globs):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        doc_file = _relative_doc_path(path, docs_root)
        heading = ""
        previous: list[str] = []
        for lineno, raw_line in enumerate(_strip_fences(text).splitlines(), start=1):
            line = raw_line.rstrip()
            if not line.strip():
                previous = []
                continue
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                heading = heading_match.group(1).strip()
                previous = []
                continue
            intro = previous[-1] if previous else ""
            spans = list(BACKTICK_RE.finditer(line))
            for index, span in enumerate(spans):
                term = span.group(1).strip()
                ident = _mention_ident(term)
                if not ident:
                    continue
                gap_before = (
                    line[spans[index - 1].end():span.start()] if index else ""
                )
                gap_after = (
                    line[span.end():spans[index + 1].start()]
                    if index + 1 < len(spans)
                    else ""
                )
                prev_term = spans[index - 1].group(1).strip() if index else ""
                next_term = spans[index + 1].group(1).strip() if index + 1 < len(spans) else ""
                opens = line.count("(", 0, span.start()) + line.count("[", 0, span.start())
                closes = line.count(")", 0, span.start()) + line.count("]", 0, span.start())
                tail = line[span.end():]
                in_paren = opens > closes and bool(_CLOSING_RE.search(tail))
                is_definition = _is_glossary_definition(line, span)
                mentions.setdefault(ident, []).append(
                    _Mention(
                        line=line,
                        heading=heading,
                        intro=intro,
                        is_list_item=bool(_LIST_ITEM_RE.match(line)),
                        in_paren=in_paren,
                        gap_before=gap_before,
                        gap_after=gap_after,
                        prev_term=prev_term,
                        next_term=next_term,
                        clause_terms=_clause_terms(spans, index),
                        in_blockquote=line.lstrip().startswith(">"),
                        definition=is_definition,
                        narrated=bool(_SOURCE_CITATION_RE.search(line)),
                        historical=_is_historical_doc(path, docs_root),
                        doc_file=doc_file,
                        doc_line=lineno,
                    )
                )
            previous = [line]
    _MENTION_CACHE[key] = mentions
    return mentions


_CLOSING_RE = re.compile(r"[)\]]")
_MENTION_CACHE: dict[tuple[str, tuple[str, ...], tuple], dict[str, list[_Mention]]] = {}


def _docs_signature(docs_root: Path, doc_globs: tuple[str, ...]) -> tuple:
    """Cheap identity of the docs tree, so a cached scan is never stale.

    Isolated copies are fresh directories, but a test or a rerun may rewrite the
    same path; mtime plus size catches that without hashing contents.
    """
    parts: list[tuple[str, int, int]] = []
    for path in list_docs(docs_root, globs=doc_globs):
        try:
            info = path.stat()
        except OSError:
            parts.append((str(path), -1, -1))
            continue
        parts.append((str(path), info.st_mtime_ns, info.st_size))
    return tuple(parts)


def _clause_terms(spans: list[Any], index: int) -> tuple[str, ...]:
    """Backticked terms in the same clause as ``index`` - no sentence break.

    Two terms belong together when at most two words separate them, which is how
    an API list reads ("includes `add` and `divide`") while a full sentence
    between them is a break.
    """
    terms = [spans[index].group(1).strip()]
    line_text = spans[index].string
    # walk right
    cursor = index
    while cursor + 1 < len(spans):
        gap = line_text[spans[cursor].end():spans[cursor + 1].start()]
        if _SENTENCE_BREAK_RE.search(gap) or len(_WORD_RE.findall(gap)) > 2:
            break
        cursor += 1
        terms.append(spans[cursor].group(1).strip())
    # walk left
    cursor = index
    while cursor - 1 >= 0:
        gap = line_text[spans[cursor - 1].end():spans[cursor].start()]
        if _SENTENCE_BREAK_RE.search(gap) or len(_WORD_RE.findall(gap)) > 2:
            break
        cursor -= 1
        terms.insert(0, spans[cursor].group(1).strip())
    return tuple(terms)


def _prose_only(term: str, mentions: dict[str, list[_Mention]]) -> bool:
    """True when no mention of ``term`` is an API promise.

    A mention is a non-promise when it is prose (list item, gloss, negation,
    quote, definition), when it sits on a line citing a ``file.ext:line``
    location (so it narrates existing code), or when it lives in a historical
    record. An incident log, changelog or postmortem reports on what the code
    *was* - it never promises the current public surface - so, exactly like a
    cited narration, a historical mention cannot escalate a term on its own.
    Only a mention that is none of these (a real, present-tense code-shaped
    promise) can drive a veto.
    """
    found = mentions.get(term) or []
    if not found:
        return False
    return all(
        m.enumerated() or m.gloss() or m.forbidden(term) or m.aside() or m.defined()
        or m.cited()
        or m.historical
        for m in found
    )


def _anchored(term: str, mentions: dict[str, list[_Mention]], resolvable) -> bool:
    """True when the docs enumerate ``term`` beside a name that exists in code.

    Documentation that lists a missing name together with a real one is
    describing an API surface, so the plain shape of the word is not evidence
    that the word is prose.
    """
    for mention in mentions.get(term) or []:
        if (
            mention.enumerated()
            or mention.gloss()
            or mention.forbidden(term)
            or mention.aside()
            or mention.defined()
            or mention.cited()
        ):
            continue
        for sibling in mention.clause_terms:
            sib = _mention_ident(sibling)
            if not sib or sib == term:
                continue
            if resolvable(sib):
                return True
    return False


def _exception_like(ident: str) -> bool:
    """``ExtensionError``/``SomethingException``: a raised-class convention.

    Names like this are code, but they are code from whatever runtime reports
    the failure. When the project does not define the class at all, the docs are
    pointing at an external error type, not promising a project API.
    """
    return ident.endswith(_EXCEPTION_SUFFIXES) and len(ident) > len("Error")


def _qualified_in_docs(ident: str, docs_text: str) -> bool:
    """The docs name it as part of a dotted path: ``concurrent.futures.X``."""
    for path in _DOTTED_NAME_RE.findall(docs_text):
        if path.split(".")[-1] == ident:
            return True
    return False


def _external_reference(
    ident: str,
    docs_text: str,
    *,
    project_symbols: set[str],
    lowered_symbols: set[str],
) -> bool:
    """A name that belongs to a runtime, another language, or another package.

    Only names that are *not* project symbols reach here, so every rule below
    can treat the name as foreign: a stdlib module, a dotted path the docs
    already qualified, or a raised-class convention. An external name is a
    reference, never missing code.
    """
    if ident.lower() in _STDLIB_MODULE_NAMES:
        return True
    if ident in project_symbols or ident.lower() in lowered_symbols:
        return False
    if _qualified_in_docs(ident, docs_text):
        return True
    return _exception_like(ident)


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


# Documentation often *disclaims* an API ("the core library does not include
# `dump`"), forbids a word ("Forbidden words: `compliant`"), glosses a term in
# parentheses ("(`preload`, `font-display`)") or uses a backticked word as a
# prose *placeholder* ("where `rowIDs` is some iterable"). None of those names a
# real callable, so none may escalate a missing identifier to a veto.
#
# The old English-only marker list is gone: `_doc_mentions` reads the structure
# of each mention instead (see _Mention), so a negated or forbidden sentence in
# any language is recognised without a translation table, and the small
# multilingual stem list in `_negated` only ever downgrades. A genuine call form
# (`divide()`) and an operator ``--strict`` run still veto.
_PLACEHOLDER_RES_CACHE: dict[str, tuple[re.Pattern, ...]] = {}


def _docs_plain_text(docs_root: Path, doc_globs: tuple[str, ...]) -> str:
    """Raw text of every doc file, used only for placeholder detection."""
    parts: list[str] = []
    for path in list_docs(docs_root, globs=doc_globs):
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _mentions_ident(sentence: str, ident: str) -> bool:
    esc = re.escape(ident)
    return bool(re.search(rf"(?<![A-Za-z0-9_])`?{esc}`?(?![A-Za-z0-9_])", sentence))


# A heading that frames what follows as an open question, a plan, or an idea -
# not a promise the project makes today. Matching is substring-based on the
# lowercase heading text, so "## Future work", "### Ideas" and "Open questions"
# all qualify while "API Surface" or "Usage" never do. Non-English stems follow
# the same multilingual convention as _NEGATION_STEMS: "otaz" catches the Czech
# words for "question(s)", "frage" the German "Fragen".
_SPECULATIVE_HEADINGS = (
    "open question",
    "question",
    "otaz",
    "frage",
    "todo",
    "next step",
    "future",
    "idea",
    "proposal",
)
# A conditional or future modal in the mentioning sentence: "we could add `x`",
# "`x` might land", "until / once `x` exists" all describe a possible tomorrow,
# not the present contract, so the term carries no present promise.
_SPECULATIVE_MODAL_RE = re.compile(
    r"\b(?:could|might|once|until|would(?:\s+also)?|we\s+can\s+also|plan(?:s|ned)?|want(?:ed)?\s+to)\b",
    re.IGNORECASE,
)


def _is_speculative_mention(m: "_Mention") -> bool:
    """True when the mention sits in clearly speculative prose.

    Either the surrounding section is an open-questions / roadmap / ideas
    heading, or the sentence itself hedges with a conditional or future modal.
    A term that only ever appears like this is not a present API promise.
    """
    heading = _fold(m.heading or "")
    if any(stem in heading for stem in _SPECULATIVE_HEADINGS):
        return True
    # Strip the backticked term itself before testing the sentence, so a name
    # like ``once_runner`` cannot trigger the modal on its own.
    line = BACKTICK_RE.sub(" ", m.line or "")
    return bool(_SPECULATIVE_MODAL_RE.search(line))


def _all_speculative(found: list["_Mention"]) -> bool:
    """Every mention of the term is speculative prose, so it is not a promise.

    One present-tense mention anywhere keeps the term a real claim; only when
    all of them are questions or plans does the finding lose its edge.
    """
    return bool(found) and all(_is_speculative_mention(m) for m in found)


def _missing_code_severity(
    ident: str,
    *,
    mentions: dict[str, list[_Mention]],
    strict: bool,
    docs_plain: str,
    resolvable,
) -> str:
    """Decide how loudly a docs term with no code counterpart is reported.

    Only code-shaped evidence may veto: an operator's ``--strict`` run, a call
    form, a name written the way code names are written (snake_case, UPPER_SNAKE,
    camelCase), or a name the docs enumerate beside a symbol that really exists
    in the project - which is how an API list reads. Everything else is a
    warning at most, in every language: a plain word, a term that only ever
    appears as an item of a vocabulary list, a parenthetical gloss, a negated or
    forbidden mention, or a prose placeholder. A term that only ever appears
    under an open-questions / future heading, or inside a conditional or future
    modal sentence, is speculation rather than a present promise and is capped
    at low even in strict mode.
    """
    found = mentions.get(ident) or []
    if found and all(m.historical for m in found):
        return "low"
    if found and _all_speculative(found):
        # Questions and plans are not promises: the cap holds even when the
        # name is code-shaped or written in call form, because the prose around
        # it never commits to the symbol existing today.
        return "low"
    if strict or ident in _CALL_FORM:
        return "high"
    if _prose_only(ident, mentions):
        return "low"
    if _is_placeholder(ident, docs_plain):
        return "medium"
    if _code_shaped(ident) or _anchored(ident, mentions, resolvable):
        return "high"
    return "medium"


def _is_placeholder(ident: str, docs_text: str) -> bool:
    """A backticked word used to define a value in prose, not to name a real
    callable. Placeholders never justify a veto."""
    if not docs_text:
        return False
    compiled = _PLACEHOLDER_RES_CACHE.get(ident)
    if compiled is None:
        esc = re.escape(ident)
        compiled = tuple(
            re.compile(p, re.IGNORECASE)
            for p in (
                rf"where\s+`?{esc}`?\s+is\b",
                rf"`{esc}`\s+is\s+(?:some|any|an|a)\b",
                rf"\b{esc}\s+is\s+some\b",
                rf"(?:e\.g\.|i\.e\.|for example)\s*[:\-]?\s*`{esc}`",
                rf"`[^`]*[=:]\s*{esc}\b[^`]*`",
                rf"\b{esc}\b\s*=\s*(?:some|an|a|any)\b",
            )
        )
        _PLACEHOLDER_RES_CACHE[ident] = compiled
    return any(p.search(docs_text) for p in compiled)


def _string_identifiers(code_strings: set[str]) -> set[str]:
    """Identifiers that live *inside* string constants.

    A schema declaration is one long literal: ``CREATE TABLE till_consumed_
    signatures (...)`` names a real table without ever holding the bare string
    ``till_consumed_signatures``. Collecting exact literals only made Lexi report
    such names as missing code. Tokens are taken from the literals, but only
    code-shaped ones (snake_case, UPPER_SNAKE, camelCase): a plain word inside an
    error message must not silence a real drift finding.
    """
    found: set[str] = set()
    for value in code_strings:
        for token in _CODE_TOKEN_RE.findall(value):
            if _code_shaped(token):
                found.add(token)
    return found


def _collect_code_ast_terms(code_files: list[Path]) -> tuple[set[str], set[str], set[str]]:
    all_identifiers: set[str] = set()
    function_names: set[str] = set()
    class_names: set[str] = set()
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
                class_names.add(node.name)
                all_identifiers.add(node.name)
            elif isinstance(node, ast.Name):
                all_identifiers.add(node.id)
            elif isinstance(node, ast.Attribute):
                all_identifiers.add(node.attr)
            elif isinstance(node, ast.keyword):
                # ``f(ignore_errors=True)`` names ``ignore_errors`` as a real
                # code term even though it is never bound as a variable.
                if node.arg:
                    all_identifiers.add(node.arg)
            elif isinstance(node, ast.arg):
                # A function parameter name is part of the public shape of the
                # project even when the body never rebinds it.
                all_identifiers.add(node.arg)
    return all_identifiers, function_names, class_names


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


def _git_ref_names(project_root: Path) -> set[str]:
    """Branch names known to the project's own repository, via plain file reads.

    Reads ``.git/HEAD`` (a ``ref: refs/heads/<name>`` line, or a bare commit id
    when detached) and every file under ``.git/refs/heads``. A project with no
    ``.git`` directory - including a worktree, where ``.git`` is a pointer file -
    yields an empty set, so nothing changes for isolated copies. No subprocess
    is run and no word list is hardcoded: only names actually present in the
    repository are returned.
    """
    names: set[str] = set()
    git = project_root / ".git"
    if not git.is_dir():
        return names
    try:
        head = (git / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        head = ""
    prefix = "ref: refs/heads/"
    if head.startswith(prefix):
        branch = head[len(prefix):].strip()
        if branch:
            names.add(branch)
    heads_dir = git / "refs" / "heads"
    if heads_dir.is_dir():
        try:
            for entry in heads_dir.rglob("*"):
                if entry.is_file():
                    rel = entry.relative_to(heads_dir).as_posix().strip()
                    if rel:
                        names.add(rel)
        except OSError:
            pass
    return names


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


def _path_suffix_match(target: str, candidates: set[str]) -> bool:
    """True when ``target`` equals, or is a path-suffix/prefix of, a candidate.

    Git reports repo-relative paths while Lexi's findings are docs-root-relative
    (or project-root-relative), so the two only line up on their shared tail.
    Matching is done component by component so ``api.md`` does not accidentally
    match ``other/api.md`` unless the whole trailing path agrees.
    """
    if not target:
        return False
    t = str(target).replace("\\", "/").lstrip("./")
    for cand in candidates:
        c = str(cand).replace("\\", "/").lstrip("./")
        if not c:
            continue
        if t == c or t.endswith("/" + c) or c.endswith("/" + t):
            return True
    return False


def _in_lexi_scope(
    finding: DriftFinding,
    docs_allowlist: set[str],
    changed_code: set[str],
) -> bool:
    """Whether a drift finding belongs to a diff scope.

    A doc-anchored finding survives when its doc is in the allowlist (the changed
    docs plus docs naming a symbol defined in the changed code). A code-anchored
    ``missing_tests`` finding survives when its file changed. Structural findings
    with no file anchor (no tests at all, a zero-collection contradiction) always
    survive, because dropping them could hide a real signal.
    """
    if finding.docs_file:
        return _path_suffix_match(finding.docs_file, docs_allowlist)
    if finding.kind == "missing_tests":
        anchor = finding.code_file or finding.path
        if anchor in {"tests", "", None}:
            return True
        return _path_suffix_match(anchor, changed_code)
    return True


def _rescope_lexi(
    report: DriftReport,
    docs_allowlist: set[str] | None,
    changed_code: set[str] | None,
) -> DriftReport:
    """Re-apply the diff scope after an LLM merge and recompute the verdict."""
    if docs_allowlist is None and changed_code is None:
        return report
    kept = [
        f
        for f in report.findings
        if _in_lexi_scope(f, docs_allowlist or set(), changed_code or set())
    ]
    if len(kept) == len(report.findings):
        return report
    report.findings = kept
    high = [f for f in kept if f.severity == "high"]
    report.verdict = Verdict.VETO.value if high else Verdict.CLEAR.value
    if not kept:
        report.notes = "no drift in the changed scope"
    elif not high:
        report.notes = "minor gaps only; not a veto"
    else:
        report.notes = "drift detected"
    return report


def _heuristic_drift(
    *,
    docs_root: Path,
    project_root: Path,
    config: dict[str, Any],
    execution: dict[str, Any] | None = None,
    live_root: Path | None = None,
    docs_allowlist: set[str] | None = None,
    changed_code: set[str] | None = None,
) -> DriftReport:
    code_files, test_files = split_code_and_tests(list_sources(project_root), project_root)
    tokens, _excerpt = doc_tokens(docs_root)
    findings: list[DriftFinding] = []
    doc_globs = tuple((config.get("lexi") or {}).get("doc_globs") or DOC_GLOBS)
    ignore = _ignore_identifiers(
        config=config, docs_root=docs_root, project_root=project_root, doc_globs=doc_globs
    )
    ignore_lower = {item.lower() for item in ignore}

    imported_names = _imported_names(project_root)
    code_text_content = _collect_code_text_content(project_root)
    all_code_identifiers, function_names, class_names = _collect_code_ast_terms(code_files)
    code_strings = _collect_code_strings(code_files, project_root)
    # Identifiers that only ever appear inside a longer literal (SQL DDL, a
    # systemd unit, a connection string) are still real code evidence.
    code_string_identifiers = _string_identifiers(code_strings)
    declared_packages = _declared_package_names(project_root)
    known_refs = _git_ref_names(project_root)
    # The tree under test is an isolated copy that never carries ``.git``; read
    # branch names from the untouched live root as well (plain file reads only).
    if live_root is not None:
        known_refs |= _git_ref_names(live_root)
    known_refs_lower = {name.lower() for name in known_refs}

    code_index: dict[str, str] = {}
    for path in code_files:
        rel = str(path.relative_to(project_root))
        stem = path.stem
        code_index[stem] = rel
        for name in public_names(path):
            code_index[name] = rel

    test_text = ""
    test_defined_names: set[str] = set()
    for path in test_files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            test_text += content + "\n"
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    test_defined_names.add(node.name)
        except (OSError, SyntaxError):
            continue

    # Backticked doc terms often name accounts, config keys or CLI values, so a
    # missing name is a warning by default and a veto only in strict mode.
    strict_missing = bool((config.get("lexi") or {}).get("veto_on_missing_code", False))
    _CALL_FORM.clear()
    other_bundle: list[tuple[str, str]] | None = None
    other_text: str | None = None
    # A missing name is a warning by default. It may only escalate on code-shaped
    # evidence: a call form (`divide()`), a name written the way code names are
    # written, or a name the docs enumerate beside a real project symbol. Terms
    # that are merely listed, glossed in parentheses, negated or used as prose
    # placeholders stay under the veto line in every language (see
    # _missing_code_severity), and ``--strict`` remains an explicit opt-in.
    claims = _extract_claims(docs_root, doc_globs)
    docs_plain = _docs_plain_text(docs_root, doc_globs)
    mentions: dict[str, list[_Mention]] | None = None
    # Everything that counts as "this name exists in the project", used to tell
    # an API list apart from a vocabulary list.
    project_symbol_names: set[str] = (
        set(all_code_identifiers) | set(code_index) | set(imported_names)
    )
    project_symbols_lower: set[str] = {name.lower() for name in project_symbol_names}

    def resolvable_symbol(name: str) -> bool:
        # Only defined functions and classes count as anchors for an API list.
        # Attribute accesses (json.dumps), keywords, args and string fragments
        # do not — otherwise a plain docs word next to any identifier becomes
        # a high-severity finding.
        if not name:
            return False
        return name in function_names or name in class_names

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
        if ident in known_refs or ident.lower() in known_refs_lower:
            # A real branch name in the project's own repository is a known
            # term, not documentation drift.
            continue
        if ident in test_defined_names:
            # A name defined only in tests (helper, fixture, test function)
            # is not missing product code.
            continue
        if _is_code_term(ident, all_code_identifiers, function_names):
            continue
        if ident in code_strings or ident in code_string_identifiers:
            # The term exists verbatim as a string literal or dict key, or as an
            # identifier inside a longer literal (SQL DDL, a systemd unit, a
            # connection string) in a project .py file (excluding tests and
            # docs) - that is not drift. This check runs *before* the comment
            # scan below: a name that is both a literal and mentioned in a
            # comment is code, and must not be filed as a mere domain term.
            continue
        if _pep503_name(ident) in declared_packages:
            # The term names a declared dependency (requirements*.txt or
            # pyproject.toml ``[project]`` / ``optional-dependencies``) — that
            # is not drift either.
            continue
        if other_bundle is None:
            # The isolated copy is the only code evidence: the isolation step
            # now expands untracked directories file by file (see
            # core.isolation), so a source tree missing from the copy is a
            # reported skip (isolation.skipped + WARN), not something Lexi may
            # silently re-read from the live root. Vendored / generated
            # directories stay excluded (see _iter_other_sources).
            other_bundle = _iter_other_sources(project_root)
            other_text = "\n".join(text for _rel, text in other_bundle)
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ident)}(?![A-Za-z0-9_])", other_text):
            continue
        if mentions is None:
            mentions = _doc_mentions(docs_root, doc_globs)
        if _external_reference(
            ident,
            docs_plain,
            project_symbols=project_symbol_names,
            lowered_symbols=project_symbols_lower,
        ):
            # A stdlib module, a name the docs already qualify with a dotted
            # path, or a raised-class convention the project never defines: the
            # docs point at somebody else's code, so it is a reference and not
            # missing project code.
            continue
        if ident in code_text_content:
            doc_file, doc_line = _mention_location(ident, mentions)
            findings.append(
                DriftFinding(
                    kind="domain_term",
                    path=ident,
                    message=f"docs mention `{ident}` but it appears only in comments/docstrings, not as code",
                    severity="low",
                    docs_file=doc_file,
                    docs_line=doc_line,
                )
            )
            continue
        prose = _prose_only(ident, mentions)
        doc_file, doc_line = _mention_location(ident, mentions)
        findings.append(
            DriftFinding(
                kind="missing_code",
                path=ident,
                message=(
                    f"docs mention `{ident}` only as a listed or negated word, not as an API"
                    if prose
                    else f"docs mention `{ident}` but no matching module or public name exists in code"
                ),
                severity=_missing_code_severity(
                    ident,
                    mentions=mentions,
                    strict=strict_missing,
                    docs_plain=docs_plain,
                    resolvable=resolvable_symbol,
                ),
                docs_file=doc_file,
                docs_line=doc_line,
            )
        )

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
                    code_file=public[0][1],
                )
            )

    if execution:
        collected = int(execution.get("collected") or 0)
        if collected <= 0:
            # An empty collection caused by the environment is not a
            # contradiction between docs and code: nothing ran, so nothing
            # disagrees. Keep the finding, but below the veto line.
            env_cause = classify_execution(execution, project_root=project_root)
            message = "post-audit: execution collected 0 tests while a test surface was expected"
            if env_cause:
                message += f" (environment blocked: {env_cause.cause})"
            findings.append(
                DriftFinding(
                    kind="contradiction",
                    path="pytest",
                    message=message,
                    severity="medium" if env_cause else "high",
                )
            )

    if docs_allowlist is not None or changed_code is not None:
        # Diff mode: keep only findings inside the computed scope, then let the
        # verdict reflect the surviving findings (a dropped high finding must not
        # still veto).
        findings = [
            f
            for f in findings
            if _in_lexi_scope(
                f,
                docs_allowlist or set(),
                changed_code or set(),
            )
        ]

    high = [f for f in findings if f.severity == "high"]
    verdict = Verdict.VETO.value if high else Verdict.CLEAR.value
    notes = "drift detected" if findings else "docs, code, and tests align at identifier level"
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
    live_root: Path | None = None,
    docs_allowlist: set[str] | None = None,
    changed_code: set[str] | None = None,
) -> DriftReport:
    report = _heuristic_drift(
        docs_root=docs_root,
        project_root=project_root,
        config=config,
        execution=execution,
        live_root=live_root,
        docs_allowlist=docs_allowlist,
        changed_code=changed_code,
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
        return _rescope_lexi(report, docs_allowlist, changed_code)
    parsed = parse_yaml_block(raw)
    if not parsed:
        report.llm_status = "fallback: invalid YAML"
        return _rescope_lexi(report, docs_allowlist, changed_code)
    result = _from_llm(parsed, report)
    result.llm_status = "ok"
    return _rescope_lexi(result, docs_allowlist, changed_code)

"""Lexi — librarian. Docs vs code vs tests. VETO on drift."""

from __future__ import annotations

import builtins
import re
from pathlib import Path
from typing import Any

import yaml

from agents.adapters import complete, load_prompt, parse_yaml_block
from agents.inventory import (
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


def _ignore_identifiers(
    *,
    config: dict[str, Any],
    docs_root: Path,
    project_root: Path,
) -> set[str]:
    ignore: set[str] = set(COMMON_WORDS)
    ignore |= _flatten_keys(config)
    extra = (config.get("lexi") or {}).get("ignore_identifiers") or []
    ignore |= {str(item) for item in extra}
    cfg_path = project_root / "config.yaml"
    if cfg_path.is_file():
        try:
            loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            loaded = {}
        if isinstance(loaded, dict):
            ignore |= _flatten_keys(loaded)
    for path in list_docs(docs_root):
        try:
            ignore |= _yaml_fence_keys(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return ignore


def _backtick_functions(docs_root: Path) -> list[str]:
    names: list[str] = []
    for path in list_docs(docs_root):
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
    return names


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
    ignore = _ignore_identifiers(
        config=config, docs_root=docs_root, project_root=project_root
    )
    ignore_lower = {item.lower() for item in ignore}

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

    seen: set[str] = set()
    for ident in _backtick_functions(docs_root):
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
        if ident in code_index:
            continue
        findings.append(
            DriftFinding(
                kind="missing_code",
                path=ident,
                message=f"docs mention `{ident}` but no matching module or public name exists in code",
                severity="high",
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
    if findings and not high:
        notes = "minor gaps only; not a veto"
    return DriftReport(
        verdict=verdict,
        findings=findings,
        docs_files=relative_strings(list_docs(docs_root), docs_root)
        if docs_root.exists()
        else [],
        code_files=relative_strings(code_files, project_root),
        test_files=relative_strings(test_files, project_root),
        notes=notes,
    )


def _from_llm(data: dict[str, Any], fallback: DriftReport) -> DriftReport:
    verdict = str(data.get("verdict") or fallback.verdict).upper()
    if verdict not in {Verdict.CLEAR.value, Verdict.VETO.value}:
        verdict = fallback.verdict
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
    return DriftReport(
        verdict=verdict,
        findings=findings or fallback.findings,
        docs_files=fallback.docs_files,
        code_files=fallback.code_files,
        test_files=fallback.test_files,
        notes=str(data.get("notes") or fallback.notes),
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
    try:
        raw = complete(
            system_prompt=load_prompt("lexi.md"),
            user_payload=payload,
            config=config,
        )
    except Exception:
        return report
    if not raw:
        return report
    parsed = parse_yaml_block(raw)
    if not parsed:
        return report
    return _from_llm(parsed, report)

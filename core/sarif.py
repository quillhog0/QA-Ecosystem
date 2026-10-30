"""SARIF 2.1.0 projection of a finished QA run.

This module converts the report dictionary produced by ``FinalReport.to_dict``
into a Static Analysis Results Interchange Format document so external tools
can render findings as code annotations. It works only on the plain dictionary
shape; it never imports anything from ``agents/`` and does not depend on the
role implementations.

Rule identifiers and their ordering are fixed by :data:`RULE_CATALOG`. The
``rules[]`` array in every emitted document contains the full catalog in that
same order, so ``ruleIndex`` values are stable across runs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from core import __version__ as _CORE_VERSION

_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
_SPEC_VERSION = "2.1.0"
_TOOL_NAME = "QA-Ecosystem"
_TOOL_INFORMATION_URI = "https://github.com/quillhog0/QA-Ecosystem"

# --- rule catalog -----------------------------------------------------------
# The order of this tuple is the SARIF contract. Adding a rule means appending
# here and updating the table in README; never reorder existing entries.
RULE_CATALOG: tuple[dict[str, str], ...] = (
    # --- Lexi (documentation drift) ---
    {
        "id": "LEXI001",
        "short": "Docs term has no matching code symbol",
        "full": (
            "A term documented in the docs root is not a module, public name, "
            "string literal, dict key or declared dependency anywhere in the "
            "project under test."
        ),
        "level": "warning",
    },
    {
        "id": "LEXI002",
        "short": "Missing or unreferenced test surface",
        "full": (
            "The test tree is empty, or public names exist in code but no test "
            "references any of them."
        ),
        "level": "warning",
    },
    {
        "id": "LEXI003",
        "short": "Post-audit contradicts the docs",
        "full": (
            "Execution produced a signal that contradicts the documented "
            "surface (for example a claim that maps to a failing test)."
        ),
        "level": "error",
    },
    {
        "id": "LEXI004",
        "short": "Docs term appears only in comments or docstrings",
        "full": (
            "The term exists in the codebase only as prose (a comment or "
            "docstring), not as a real symbol or literal value."
        ),
        "level": "note",
    },
    {
        "id": "LEXI005",
        "short": "Documentation drift flagged by review",
        "full": (
            "Documentation references something that no longer matches the "
            "current code surface."
        ),
        "level": "warning",
    },
    {
        "id": "LEXI999",
        "short": "Other drift",
        "full": "A Lexi finding kind that does not map to a specific rule id.",
        "level": "warning",
    },
    # --- Sable (policy gate) ---
    {
        "id": "SABLE001",
        "short": "Command is not pytest-only",
        "full": (
            "The command Nova was about to run was not pytest with a safe "
            "token set."
        ),
        "level": "error",
    },
    {
        "id": "SABLE002",
        "short": "Secret-looking environment keys reach Nova",
        "full": (
            "Nova's environment contains variables whose names look like "
            "credentials."
        ),
        "level": "error",
    },
    {
        "id": "SABLE003",
        "short": "Secret-looking files in isolated copy",
        "full": (
            "The isolated copy contains a file whose name or extension looks "
            "secret."
        ),
        "level": "error",
    },
    {
        "id": "SABLE004",
        "short": "Refusing to run against the live tree",
        "full": "The isolated copy resolved to the live working tree.",
        "level": "error",
    },
    {
        "id": "SABLE005",
        "short": "Isolated root is missing",
        "full": "The isolated copy directory does not exist.",
        "level": "error",
    },
    {
        "id": "SABLE006",
        "short": "pytest path escapes isolated copy",
        "full": (
            "A pytest target, --rootdir or cwd resolves outside the isolated "
            "copy."
        ),
        "level": "error",
    },
    {
        "id": "SABLE007",
        "short": "Blocked call in the isolated copy",
        "full": (
            "Static AST scan flagged a dangerous call (subprocess, socket, "
            "eval, exec, os.system, __import__ and similar) inside the "
            "isolated copy."
        ),
        "level": "error",
    },
    {
        "id": "SABLE008",
        "short": "Network utility while allow_network is false",
        "full": (
            "The command invokes curl, wget, ssh or another network binary "
            "while the network policy is closed."
        ),
        "level": "error",
    },
    {
        "id": "SABLE009",
        "short": "Collected zero tests",
        "full": (
            "pytest collected 0 tests, so the run cannot witness the planned "
            "coverage."
        ),
        "level": "error",
    },
    # --- Riley (adversarial review) ---
    {
        "id": "RILEY001",
        "short": "Weak test detected",
        "full": (
            "A test is skipped, contains only tautological assertions, "
            "swallows assertion failures, or has no meaningful assertion."
        ),
        "level": "warning",
    },
    {
        "id": "RILEY002",
        "short": "Uncovered claim",
        "full": (
            "A documented claim has no covering test, or is covered only by a "
            "weak test."
        ),
        "level": "warning",
    },
    {
        "id": "RILEY999",
        "short": "Other adversarial-review gap",
        "full": "A Riley gap that does not map to a specific rule id.",
        "level": "warning",
    },
    # --- Nova (execution) ---
    {
        "id": "NOVA001",
        "short": "pytest failure",
        "full": "A pytest test failed during the isolated run.",
        "level": "error",
    },
    {
        "id": "NOVA002",
        "short": "Dependency install failure",
        "full": "pip install of the project's declared dependencies failed.",
        "level": "error",
    },
    # --- Vesper (control plane) ---
    {
        "id": "VESPER001",
        "short": "Cycle did not close",
        "full": (
            "The orchestrator could not close the loop and escalated to a "
            "domain expert."
        ),
        "level": "error",
    },
    {
        "id": "VESPER002",
        "short": "Run vetoed before execution",
        "full": (
            "The verdict is not CLEAR but no other rule produced a result, for "
            "example because the run was vetoed before any role executed "
            "(sandbox required but unavailable)."
        ),
        "level": "error",
    },
)

_RULE_INDEX: dict[str, int] = {rule["id"]: i for i, rule in enumerate(RULE_CATALOG)}

_LEXI_KIND_TO_RULE: dict[str, str] = {
    "missing_code": "LEXI001",
    "missing_tests": "LEXI002",
    "contradiction": "LEXI003",
    "domain_term": "LEXI004",
    "stale_docs": "LEXI005",
}

_SABLE_CHECK_TO_RULE: dict[str, str] = {
    "pytest_only": "SABLE001",
    "no_secret_env": "SABLE002",
    "no_secret_files": "SABLE003",
    "not_live_tree": "SABLE004",
    "isolated_exists": "SABLE005",
    "target_inside_isolated": "SABLE006",
    "tests_ast_clean": "SABLE007",
    "network_denied": "SABLE008",
    "collected_gt_zero": "SABLE009",
}

# Substrings that must never appear inside a SARIF artifact location. They
# mark paths that belong to the isolated copy or the tool's scratch area, not
# to the project under test.
_LOCATION_BLOCK_MARKERS: tuple[str, ...] = (
    "qa-agent-ecosystem-isolations",
    "isolated_copy",
)
_WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
_DRIVE_OR_UNC_RE = re.compile(r"^\\\\")
_PY_FILE_REF_RE = re.compile(r"(?P<file>[A-Za-z0-9_.\-/]+\.py)\s*:")
_FAILED_LINE_RE = re.compile(
    r"^FAILED\s+(?P<node>\S+)(?:\s+-\s+(?P<msg>.*?))?\s*$",
    re.MULTILINE,
)
_WEAK_TEST_PREFIX = "weak test: "
_CLAIM_NOT_COVERED_PREFIX = "claim not covered by tests:"
_UNCOVERED_CLAIMS_PREFIX = "uncovered claims:"
_WEAK_CLAIM_FRAGMENT = "is covered only by weak test"


# --- path safety ------------------------------------------------------------
def _safe_rel_path(value: Any) -> str | None:
    """Return a POSIX-relative project path, or ``None`` if unsafe.

    Rejects empty values, absolute POSIX/Windows paths, ``..`` traversal,
    embedded null bytes, and any string that contains an isolated-copy or
    scratch directory marker. Only forward-slash output is produced.
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or "\x00" in raw:
        return None
    if raw.startswith("/") or raw.startswith("\\\\"):
        return None
    if _WINDOWS_ABSOLUTE_RE.match(raw):
        return None
    lowered = raw.lower()
    for marker in _LOCATION_BLOCK_MARKERS:
        if marker in lowered:
            return None
    posix = raw.replace("\\", "/")
    parts = PurePosixPath(posix).parts
    if not parts:
        return None
    if any(part == ".." for part in parts):
        return None
    if parts[0].lower() in {"tmp", "var", "etc", "usr", "home", "root"}:
        return None
    return posix


def _make_location(rel_uri: str) -> dict[str, Any]:
    return {
        "physicalLocation": {
            "artifactLocation": {
                "uri": rel_uri,
                "uriBaseId": "%SRCROOT%",
            }
        }
    }


# --- result assembly --------------------------------------------------------
def _append_result(
    results: list[dict[str, Any]],
    *,
    rule_id: str,
    level: str,
    message: str,
    location_uri: str | None,
) -> None:
    entry: dict[str, Any] = {
        "ruleId": rule_id,
        "ruleIndex": _RULE_INDEX[rule_id],
        "level": level,
        "message": {"text": message or rule_id},
    }
    safe_uri = _safe_rel_path(location_uri) if location_uri else None
    if safe_uri:
        entry["locations"] = [_make_location(safe_uri)]
    results.append(entry)


def _severity_to_level(severity: str) -> str:
    s = (severity or "").lower()
    if s in {"high", "critical", "blocker", "error"}:
        return "error"
    if s in {"low", "info", "note", "hint"}:
        return "note"
    return "warning"


# --- per-role collectors ----------------------------------------------------
def _add_lexi(results: list[dict[str, Any]], report: dict[str, Any]) -> None:
    drift = report.get("drift") or {}
    if not isinstance(drift, dict):
        return
    findings = drift.get("findings") or []
    if not isinstance(findings, list):
        return
    known_files: set[str] = set()
    for key in ("code_files", "test_files", "docs_files"):
        for entry in drift.get(key) or []:
            safe = _safe_rel_path(entry)
            if safe:
                known_files.add(safe)
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        kind = str(finding.get("kind") or "")
        rule_id = _LEXI_KIND_TO_RULE.get(kind, "LEXI999")
        level = _severity_to_level(str(finding.get("severity") or "medium"))
        message = str(finding.get("message") or kind or rule_id)
        path_value = finding.get("path")
        safe_path = _safe_rel_path(path_value)
        location_uri = safe_path if safe_path and safe_path in known_files else None
        _append_result(
            results,
            rule_id=rule_id,
            level=level,
            message=message,
            location_uri=location_uri,
        )


def _sable_phases(policy: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield ``(phase_name, phase_data)`` pairs from either policy shape.

    ``report.policy`` is written as a bare ``PolicyDecision`` dict when the
    preflight blocks early, and as ``{"preflight": ..., "post": ...}``
    otherwise. Both shapes are handled here.
    """
    if not isinstance(policy, dict):
        return
    if "checks" in policy and "blocked_reasons" in policy:
        yield "preflight", policy
        return
    for key in ("preflight", "post"):
        entry = policy.get(key)
        if isinstance(entry, dict):
            yield key, entry


def _extract_first_py_path(reason: str) -> str | None:
    for match in _PY_FILE_REF_RE.finditer(reason or ""):
        safe = _safe_rel_path(match.group("file"))
        if safe:
            return safe
    return None


def _add_sable(results: list[dict[str, Any]], report: dict[str, Any]) -> None:
    policy = report.get("policy") or {}
    if not isinstance(policy, dict):
        return
    for phase_name, phase_data in _sable_phases(policy):
        checks = phase_data.get("checks") or {}
        if not isinstance(checks, dict):
            continue
        blocked = phase_data.get("blocked_reasons") or []
        joined = "; ".join(str(r) for r in blocked if r)
        failing_keys = [k for k, ok in checks.items() if ok is False]
        for check_key in failing_keys:
            rule_id = _SABLE_CHECK_TO_RULE.get(str(check_key))
            if not rule_id:
                continue
            message = f"[{phase_name}] {joined}" if joined else (
                f"[{phase_name}] policy check failed: {check_key}"
            )
            location_uri = (
                _extract_first_py_path(joined) if check_key == "tests_ast_clean" else None
            )
            _append_result(
                results,
                rule_id=rule_id,
                level="error",
                message=message,
                location_uri=location_uri,
            )


def _classify_riley_gap(gap: str) -> tuple[str, str | None]:
    if gap.startswith(_WEAK_TEST_PREFIX):
        rest = gap[len(_WEAK_TEST_PREFIX):].strip()
        node = rest.split(" ", 1)[0]
        file_part = node.split("::", 1)[0] if node else ""
        return "RILEY001", _safe_rel_path(file_part)
    if (
        gap.startswith(_CLAIM_NOT_COVERED_PREFIX)
        or gap.startswith(_UNCOVERED_CLAIMS_PREFIX)
        or _WEAK_CLAIM_FRAGMENT in gap
    ):
        return "RILEY002", None
    return "RILEY999", None


def _add_riley(results: list[dict[str, Any]], report: dict[str, Any]) -> None:
    post = report.get("post_audit") or {}
    if not isinstance(post, dict):
        return
    riley = post.get("riley") or {}
    if not isinstance(riley, dict):
        return
    gaps = riley.get("gaps") or []
    if not isinstance(gaps, list):
        return
    verdict = str(riley.get("verdict") or "").upper()
    level = "error" if verdict == "VETO" else "warning"
    for gap in gaps:
        if not isinstance(gap, str):
            continue
        rule_id, location_uri = _classify_riley_gap(gap)
        _append_result(
            results,
            rule_id=rule_id,
            level=level,
            message=gap,
            location_uri=location_uri,
        )


def _nova_failed_entries(text: str) -> list[tuple[str, str | None, str]]:
    out: list[tuple[str, str | None, str]] = []
    for match in _FAILED_LINE_RE.finditer(text or ""):
        node = (match.group("node") or "").strip()
        msg = (match.group("msg") or "").strip()
        if not node:
            continue
        file_part = node.split("::", 1)[0]
        out.append((node, _safe_rel_path(file_part), msg))
    return out


def _add_nova(results: list[dict[str, Any]], report: dict[str, Any]) -> None:
    execution = report.get("execution") or {}
    if not isinstance(execution, dict) or not execution:
        return
    install_status = str(execution.get("install_status") or "")
    install_failed = install_status.startswith("pip install failed")
    if install_failed:
        _append_result(
            results,
            rule_id="NOVA002",
            level="error",
            message=f"dependency installation failed: {install_status}",
            location_uri=None,
        )
        # Installation broke before pytest could produce a signal; skip NOVA001.
        return
    exit_code = execution.get("exit_code")
    if not isinstance(exit_code, int):
        return
    failed_count = int(execution.get("failed") or 0)
    if exit_code == 0 and failed_count == 0:
        return
    text = f"{execution.get('stdout') or ''}\n{execution.get('stderr') or ''}"
    entries = _nova_failed_entries(text)
    if not entries:
        _append_result(
            results,
            rule_id="NOVA001",
            level="error",
            message=f"pytest exit={exit_code} failed={failed_count}",
            location_uri=None,
        )
        return
    for node, safe_uri, msg in entries:
        body = f"{node}: {msg}" if msg else node
        _append_result(
            results,
            rule_id="NOVA001",
            level="error",
            message=body,
            location_uri=safe_uri,
        )


def _add_vesper(results: list[dict[str, Any]], report: dict[str, Any]) -> None:
    escalation = report.get("escalation")
    if not isinstance(escalation, str) or not escalation.strip():
        return
    _append_result(
        results,
        rule_id="VESPER001",
        level="error",
        message=escalation.strip(),
        location_uri=None,
    )


def _add_early_veto(results: list[dict[str, Any]], report: dict[str, Any]) -> None:
    verdict = str(report.get("verdict") or "").upper()
    if results or not verdict or verdict == "CLEAR":
        return
    summary = ""
    for entry in report.get("role_results") or []:
        if isinstance(entry, dict) and str(entry.get("verdict") or "").upper() == verdict:
            summary = str(entry.get("summary") or "").strip()
            if summary:
                break
    message = f"run vetoed before execution: verdict {verdict}"
    if summary:
        message += f" ({summary})"
    _append_result(
        results,
        rule_id="VESPER002",
        level="error",
        message=message,
        location_uri=None,
    )


# --- SARIF document ---------------------------------------------------------
def _rule_object(entry: dict[str, str]) -> dict[str, Any]:
    return {
        "id": entry["id"],
        "name": entry["id"],
        "shortDescription": {"text": entry["short"]},
        "fullDescription": {"text": entry["full"]},
        "defaultConfiguration": {"level": entry["level"], "enabled": True},
        "helpUri": f"{_TOOL_INFORMATION_URI}#sarif-output",
    }


def _as_report_dict(report: Any) -> dict[str, Any]:
    if isinstance(report, dict):
        return report
    to_dict = getattr(report, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        if isinstance(data, dict):
            return data
    raise TypeError("sarif builder expects a mapping or an object with to_dict()")


def build_sarif(report: Any) -> dict[str, Any]:
    """Convert a finished report into a SARIF 2.1.0 document.

    Accepts either a plain dictionary (typically ``FinalReport.to_dict()``) or
    any object that exposes ``to_dict()``. The output always contains the full
    :data:`RULE_CATALOG` in a fixed order, so ``ruleIndex`` values remain
    stable across runs.
    """
    data = _as_report_dict(report)
    results: list[dict[str, Any]] = []
    _add_lexi(results, data)
    _add_sable(results, data)
    _add_riley(results, data)
    _add_nova(results, data)
    _add_vesper(results, data)
    _add_early_veto(results, data)
    driver = {
        "name": _TOOL_NAME,
        "version": _CORE_VERSION,
        "informationUri": _TOOL_INFORMATION_URI,
        "rules": [_rule_object(entry) for entry in RULE_CATALOG],
    }
    run: dict[str, Any] = {
        "tool": {"driver": driver},
        "results": results,
        "originalUriBaseIds": {
            "%SRCROOT%": {"description": {"text": "Project root under test"}}
        },
    }
    return {
        "$schema": _SCHEMA_URI,
        "version": _SPEC_VERSION,
        "runs": [run],
    }


def write_sarif(report: Any, dest: Path | str) -> Path:
    """Serialise :func:`build_sarif` to ``dest`` and return the path written."""
    document = build_sarif(report)
    target = Path(dest)
    parent = target.parent
    if str(parent) not in {"", "."}:
        parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return target

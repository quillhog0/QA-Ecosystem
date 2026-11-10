"""Optional LLM triage of already-emitted, ambiguous deterministic findings.

The deterministic scan always runs first. This module may only downgrade or
annotate a finding, and only for a short allowlist of ambiguous classes. It
never creates a finding, never upgrades severity, and never clears a P0 that
still has evidence. When the LLM is off or unavailable the findings are
unchanged.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from agents.adapters import complete_with_status, llm_enabled, scrub_secrets

# Classes the deterministic layer already marks as ambiguous-enough to ask
# a model about. Everything else is left untouched.
TRIAGE_TYPES = frozenset({
    "drift",
    "claim",
    "doc_drift",
    "undocumented_env",
    "install_mismatch",
    "mutation",
    "dangerous_ast",
    "policy",
})

_PRIORITY_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_DOWNGRADES = {"P1": "P2", "P2": "P3"}


def _priority_rank(value: str) -> int:
    return _PRIORITY_RANK.get(str(value or "").upper(), 99)


def is_triage_candidate(finding: dict[str, Any]) -> bool:
    """True when this finding may be shown to the model."""
    if not isinstance(finding, dict):
        return False
    if str(finding.get("priority") or "").upper() == "P0" and str(finding.get("evidence") or "").strip():
        # An evidenced P0 is never sent; the model must not be able to clear it.
        return False
    ftype = str(finding.get("type") or finding.get("ftype") or "")
    fclass = str(finding.get("class") or finding.get("fclass") or "")
    if ftype in TRIAGE_TYPES:
        return True
    if ftype == "mutation" or "likely" in str(finding.get("impact") or "").lower():
        return True
    if fclass in {"heuristic", "doc"} and ftype in TRIAGE_TYPES:
        return True
    return False


def _snippet_for(finding: dict[str, Any], isolated_root: Path | None, limit: int = 8) -> str:
    path = str(finding.get("file") or "")
    line = finding.get("line")
    if not path or isolated_root is None:
        ev = str(finding.get("evidence") or finding.get("impact") or "")
        return ev[:800]
    candidate = isolated_root / path
    if not candidate.is_file():
        ev = str(finding.get("evidence") or "")
        return ev[:800]
    try:
        rows = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return str(finding.get("evidence") or "")[:800]
    if not isinstance(line, int) or line <= 0:
        return "\n".join(rows[:limit])
    start = max(0, line - 1 - 2)
    end = min(len(rows), line - 1 + limit - 2)
    return "\n".join(rows[start:end])


def _payload_for(
    findings: list[dict[str, Any]],
    isolated_root: Path | None,
) -> dict[str, Any]:
    items = []
    for finding in findings:
        items.append({
            "id": finding.get("id") or finding.get("fingerprint") or finding.get("term"),
            "fingerprint": finding.get("fingerprint"),
            "type": finding.get("type"),
            "priority": finding.get("priority"),
            "term": finding.get("term"),
            "file": finding.get("file"),
            "line": finding.get("line"),
            "impact": str(finding.get("impact") or "")[:400],
            "evidence": str(finding.get("evidence") or "")[:400],
            "snippet": _snippet_for(finding, isolated_root),
        })
    return {"task": "triage", "findings": items}


def _parse_actions(raw: str | None) -> list[dict[str, str]]:
    if not raw or not str(raw).strip():
        return []
    text = str(raw).strip()
    fence = re.search(r"```(?:json|yaml)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    blob = fence.group(1) if fence else text
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        # Tolerate a YAML-ish "actions:" list with fingerprint/action keys.
        actions: list[dict[str, str]] = []
        current: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- "):
                if current:
                    actions.append(current)
                current = {}
                stripped = stripped[2:].strip()
            if ":" in stripped:
                key, _, value = stripped.partition(":")
                key = key.strip().lower()
                value = value.strip().strip("\"'")
                if key in {"fingerprint", "id", "term", "action", "reason", "priority"}:
                    current[key] = value
        if current:
            actions.append(current)
        return actions
    if isinstance(data, dict):
        data = data.get("actions") or data.get("findings") or []
    if not isinstance(data, list):
        return []
    out: list[dict[str, str]] = []
    for item in data:
        if isinstance(item, dict):
            out.append({str(k).lower(): str(v) for k, v in item.items() if v is not None})
    return out


def _match_action(finding: dict[str, Any], action: dict[str, str]) -> bool:
    for key in ("fingerprint", "id"):
        want = action.get(key)
        have = finding.get(key) or finding.get("fingerprint")
        if want and have and str(want) == str(have):
            return True
    term = action.get("term")
    if term and str(finding.get("term") or "") == term:
        return True
    return False


def _apply_action(finding: dict[str, Any], action: dict[str, str], fail_on: str) -> str:
    """Apply a single model action. Returns a short status string."""
    kind = str(action.get("action") or action.get("decision") or "").strip().lower()
    reason = str(action.get("reason") or "").strip() or "llm triage"
    if kind in {"keep", "unchanged", "none", ""}:
        if reason and kind in {"keep", "unchanged"}:
            finding["triage"] = {"action": "keep", "reason": reason}
        return "kept"
    if kind not in {"downgrade", "annotate", "note", "lower"}:
        return "ignored"
    if kind in {"annotate", "note"}:
        finding["triage"] = {"action": "annotate", "reason": reason}
        finding["notes"] = (str(finding.get("notes") or "") + (" " if finding.get("notes") else "") + f"triage: {reason}").strip()
        return "annotated"
    # Downgrade one step at most, never off the board, never a P0 with evidence.
    current = str(finding.get("priority") or "").upper()
    if current == "P0" and str(finding.get("evidence") or "").strip():
        return "refused_p0"
    target = str(action.get("priority") or _DOWNGRADES.get(current) or current).upper()
    if target not in _PRIORITY_RANK:
        target = _DOWNGRADES.get(current, current)
    if _priority_rank(target) <= _priority_rank(current):
        # Never upgrade or stay-as-upgrade; ignore equal-or-higher.
        if _priority_rank(target) < _priority_rank(current):
            return "refused_upgrade"
        target = _DOWNGRADES.get(current, current)
        if _priority_rank(target) <= _priority_rank(current):
            # Already P3 or unknown; annotate only.
            finding["triage"] = {"action": "annotate", "reason": reason}
            return "annotated"
    from core.findings import verdict_effect_for
    finding["priority"] = target
    finding["verdict_effect"] = verdict_effect_for(target, str(finding.get("evidence") or ""), fail_on=fail_on)
    finding["triage"] = {"action": "downgrade", "reason": reason, "from": current, "to": target}
    finding["confidence"] = "medium"
    return "downgraded"


def apply_triage(
    findings: list[dict[str, Any]],
    *,
    config: dict[str, Any] | None,
    report: dict[str, Any] | None = None,
    isolated_root: Path | str | None = None,
    fail_on: str = "P1",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return ``(findings, meta)``. Findings are the same objects, possibly annotated.

    No-op when the LLM is disabled or the call fails. The input list is
    mutated in place for downgrades so callers that already hold the list
    see the result.
    """
    meta: dict[str, Any] = {"status": "skipped", "reason": "llm disabled", "candidates": 0, "applied": []}
    config = config or {}
    if not llm_enabled(config):
        return findings, meta
    candidates = [f for f in findings if is_triage_candidate(f)]
    meta["candidates"] = len(candidates)
    if not candidates:
        meta["status"] = "skipped"
        meta["reason"] = "no eligible findings"
        return findings, meta
    root = Path(isolated_root) if isolated_root else None
    payload = _payload_for(candidates, root)
    payload = json.loads(scrub_secrets(json.dumps(payload, ensure_ascii=False)))
    raw, status = complete_with_status(
        system_prompt=(
            "You triage already-emitted QA findings. You may only downgrade or "
            "annotate. Never create or upgrade a finding. Never clear a P0 that "
            "has evidence. Reply with JSON: {\"actions\": [{\"fingerprint\": "
            "\"...\", \"action\": \"downgrade|annotate|keep\", \"reason\": \"...\"}]}"
        ),
        user_payload=payload,
        config=config,
    )
    if not raw:
        meta["status"] = "skipped"
        meta["reason"] = status or "llm unavailable"
        return findings, meta
    actions = _parse_actions(raw)
    applied: list[dict[str, Any]] = []
    for action in actions:
        matched = [f for f in candidates if _match_action(f, action)]
        for finding in matched:
            result = _apply_action(finding, action, fail_on=fail_on)
            applied.append({
                "fingerprint": finding.get("fingerprint"),
                "action": action.get("action"),
                "result": result,
                "reason": action.get("reason"),
            })
    meta["status"] = "ok"
    meta["reason"] = status or "ok"
    meta["applied"] = applied
    return findings, meta

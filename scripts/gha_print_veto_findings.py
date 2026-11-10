#!/usr/bin/env python3
"""Print P0/P1 findings with file:line and reason for the Action log/summary.

Reads findings.json (preferred) or findings.md. Never exits non-zero: the
caller must still write GitHub outputs and then exit with the QA cycle code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _one_line(text: object) -> str:
    return " ".join(str(text or "").split())


def _from_json(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows: list[str] = []
    for finding in data.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        if str(finding.get("priority") or "").upper() not in {"P0", "P1"}:
            continue
        loc = str(finding.get("file") or "").strip()
        line = finding.get("line")
        if loc and isinstance(line, int) and line > 0:
            loc = f"{loc}:{line}"
        reason = _one_line(finding.get("evidence") or finding.get("impact") or "")
        agent = str(finding.get("agent") or "")
        parts = [str(finding.get("priority")).upper()]
        if agent:
            parts.append(agent)
        if loc:
            parts.append(loc)
        parts.append(reason or "(no evidence)")
        rows.append(" ".join(parts))
    return rows


def _from_markdown(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    rows: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## P0") or stripped.startswith("## P1"):
            rows.append(stripped.lstrip("# ").strip())
        elif stripped.startswith("- ") and ("P0" in stripped or "P1" in stripped):
            rows.append(stripped[2:].strip())
    return rows


def collect(path: Path) -> list[str]:
    if path.is_dir():
        js = path / "findings.json"
        md = path / "findings.md"
        if js.is_file():
            return _from_json(js)
        if md.is_file():
            return _from_markdown(md)
        return []
    if path.suffix.lower() == ".json":
        return _from_json(path)
    if path.suffix.lower() == ".md":
        return _from_markdown(path)
    # Path may be findings.md; also try sibling .json
    sibling = path.with_suffix(".json")
    if sibling.is_file():
        return _from_json(sibling)
    if path.is_file():
        return _from_markdown(path) if path.suffix.lower() == ".md" else _from_json(path)
    return []


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return 0
    rows = collect(Path(args[0]))
    for row in rows:
        sys.stdout.write(row + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

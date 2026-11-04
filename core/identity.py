"""Identity leak detection and sensitive data scrubbing."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agents.inventory import CODE_SKIP, list_docs, list_sources

GENERIC_PATTERNS: dict[str, re.Pattern] = {
    "private_key": re.compile(r"-----BEGIN (?:[A-Z ]*)?PRIVATE KEY-----"),
    "home_path": re.compile(r"(?:/home/(?!sandbox|runner|foo|bar|user|username|example|sample|dev|app|nobody)[a-zA-Z0-9_.-]+|/Users/(?!runner|foo|bar|user|username|example|sample)[a-zA-Z0-9_.-]+|[A-Za-z]:\\Users\\(?!runner|foo|bar|user|username|example|sample)[a-zA-Z0-9_.-]+)"),
    "email": re.compile(r"\b(?<!git@)(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@(?!(?:[A-Za-z0-9.-]+\.)?(?:example|invalid|localhost|test|noreply|domain|host|email)\b|users\.noreply\.github\.com\b|github\.com\b|gitlab\.com\b|bitbucket\.org\b)[A-Za-z0-9.-]+\.(?!(?:png|jpg|jpeg|gif|svg|webp|ico|lock|json|yaml|yml|md|txt)\b)[A-Za-z]{2,}\b"),
    "token": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|\bxox[abprs]-[A-Za-z0-9\-]{10,}\b|\bsk-[A-Za-z0-9_\-]{20,}\b"),
    "rpc_url_with_key": re.compile(r"https?://[^\s'\"<>]*(?:alchemy\.com/v2/|infura\.io/v3/|quicknode\.(?:pro|com)/|helius-rpc\.com/[^\s'\"<>]*api-key=)[A-Za-z0-9_\-]{16,}"),
}

SKIP_STEMS = frozenset({"license", "copying", "notice", "authors", "changelog", "citation", "historical-evidence"})
SKIP_STEM_PREFIXES = ("license", "authors", "changelog", "copying", "notice", "citation")
COMMUNITY_STEMS = frozenset({"code_of_conduct", "contributing", "releasing", "release", "governance", "security", "roadmap", "authors", "maintainers"})
SKIP_DATA_DIRS = frozenset({"test_data", "fixtures", "test_fixtures", "data", "benchmark", "historical", "release-9"})
SKIP_EXTENSIONS = frozenset({
    ".cff", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".pyc", ".wasm",
    ".zip", ".tar", ".gz", ".po", ".pot", ".mo"
})
MANIFEST_FILES = frozenset({"pyproject.toml", "setup.cfg", "setup.py", "package.json", "PKG-INFO", "METADATA"})
LOCK_FILES = frozenset({"package-lock.json", "pnpm-lock.yaml", "yarn.lock", "poetry.lock", "Pipfile.lock"})

REAL_SECRET_TYPES = frozenset({"private_key", "token", "rpc_url_with_key"})

AUTHOR_EMAIL_RE = re.compile(
    r"__(?:author_)?email__\s*=|author(?:_email)?\s*[:=]|maintainer(?:_email)?\s*[:=]|contributors?\s*[:=]",
    re.IGNORECASE,
)

REDACTION_LABEL = "[REDACTED]"


def scrub_identity_data(text: str) -> str:
    """Scrub sensitive identity data (home paths, emails, tokens, keys) from reports/text."""
    if not text:
        return ""
    scrubbed = text
    for pattern in GENERIC_PATTERNS.values():
        scrubbed = pattern.sub(REDACTION_LABEL, scrubbed)
    return scrubbed


def scan_identity_leaks(
    project_root: Path,
    config: dict[str, Any] | None = None,
    only_paths: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Scan tree for identity leaks. Findings report file:line and type only; NEVER the value."""
    findings: list[dict[str, Any]] = []
    if not project_root or not project_root.is_dir():
        return findings

    cfg = config or {}
    ident_cfg = cfg.get("identity") or {}
    identity_strict = bool(cfg.get("identity_strict", False) or ident_cfg.get("strict", False))

    personal_names = [
        str(n).strip() for n in (ident_cfg.get("names") or []) if str(n).strip()
    ]
    name_patterns: list[re.Pattern] = [
        re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE)
        for name in personal_names
    ]

    # Collect files to scan: all sources and docs, skipping vendored/build dirs
    files_to_scan: list[Path] = []
    for p in project_root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(project_root)
        except ValueError:
            continue
        if any(part in CODE_SKIP for part in rel.parts[:-1]):
            continue
        if any(part.endswith((".egg-info", ".dist-info")) for part in rel.parts[:-1]):
            continue
        if any(part.lower() in SKIP_DATA_DIRS for part in rel.parts[:-1]):
            continue
        stem_lower = p.stem.lower()
        if stem_lower in SKIP_STEMS or any(stem_lower.startswith(pfx) for pfx in SKIP_STEM_PREFIXES):
            continue
        # Skip binaries, images, catalogs, and citation metadata
        if p.suffix.lower() in SKIP_EXTENSIONS:
            continue
        rel_posix = str(rel).replace("\\", "/")
        if only_paths is not None and rel_posix not in only_paths:
            continue
        files_to_scan.append(p)

    for path in sorted(files_to_scan):
        try:
            rel_path = path.relative_to(project_root)
            rel = str(rel_path).replace("\\", "/")
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        is_manifest = path.name in MANIFEST_FILES
        is_community = path.stem.lower() in COMMUNITY_STEMS
        is_version_or_about = path.stem.lower() in ("__version__", "__about__", "_version", "about", "version")
        is_test_path = (
            any(part.lower() in ("tests", "test", "testing", "__tests__") for part in rel_path.parts[:-1])
            or path.stem.lower().startswith("test_")
            or path.stem.lower().endswith(("_test", ".test", ".spec"))
        )
        if is_test_path:
            continue

        for lineno, line in enumerate(lines, start=1):
            # Check generic patterns
            for leak_type, pattern in GENERIC_PATTERNS.items():
                if (is_manifest or path.name in LOCK_FILES) and leak_type == "email":
                    continue
                if is_community and leak_type == "email":
                    continue
                if is_version_or_about and leak_type == "email" and AUTHOR_EMAIL_RE.search(line):
                    continue
                if is_test_path and leak_type in ("email", "home_path"):
                    continue
                if pattern.search(line):
                    if leak_type in REAL_SECRET_TYPES:
                        priority = "P1"
                    else:
                        priority = "P1" if identity_strict else "P2"
                    findings.append(
                        {
                            "file": rel,
                            "line": lineno,
                            "type": f"identity_leak:{leak_type}",
                            "priority": priority,
                            "message": f"identity leak detected: {leak_type} (value omitted)",
                            "evidence": f"{rel}:{lineno} identity_leak:{leak_type}",
                        }
                    )

            # Check personal names
            for pat in name_patterns:
                if pat.search(line):
                    priority = "P1" if identity_strict else "P2"
                    findings.append(
                        {
                            "file": rel,
                            "line": lineno,
                            "type": "identity_leak:personal_name",
                            "priority": priority,
                            "message": "identity leak detected: personal name (value omitted)",
                            "evidence": f"{rel}:{lineno} identity_leak:personal_name",
                        }
                    )

    return findings

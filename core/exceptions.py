"""Operator-owned exception policy.

Exceptions let a maintainer deliberately silence a known finding for a bounded
time. They live in the QA repository (or an operator config directory), never in
the project under test, so a tested project can never add or widen its own
exceptions - the same guarantee that keeps a project from changing the effective
config.

A file is ``exceptions/<project-name>.yaml`` by default, or whatever path the
operator sets in ``config["exceptions"]["file"]``. Each entry names a finding
fingerprint to ``match``, a non-empty ``reason``, an ``expires`` date at most
``max_days`` (default 90) days ahead, optional free-text ``added_by`` and an
explicit ``allow_p0`` for the only case a P0 may be excepted.

Loading is strict on purpose: a malformed entry raises
:class:`ExceptionsConfigError` at startup rather than being silently ignored, so
a typo can never quietly disable a guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MAX_DAYS = 90

# Root of the QA repository: this module lives at <repo>/core/exceptions.py.
REPO_ROOT = Path(__file__).resolve().parent.parent


class ExceptionsConfigError(Exception):
    """Raised when an exceptions file is present but invalid. Never ignored."""


@dataclass
class ExceptionEntry:
    match: str
    reason: str
    expires: date
    added_by: str = ""
    allow_p0: bool = False

    def is_expired(self, today: date) -> bool:
        return self.expires < today


def _repo_root() -> Path:
    return REPO_ROOT


def resolve_exceptions_path(config: dict[str, Any], project_name: str) -> Path | None:
    """Absolute path of the exceptions file, or ``None`` when there is none.

    An operator override (``config["exceptions"]["file"]``) wins and is resolved
    relative to the QA repo when it is not absolute. Otherwise the default
    ``exceptions/<project-name>.yaml`` under the QA repo (or an operator
    ``exceptions_dir``) is used. A path is only returned when the file exists;
    a missing file simply means "no exceptions", which is not an error.
    """
    section = config.get("exceptions") or {}
    if not isinstance(section, dict):
        section = {}
    explicit = str(section.get("file") or "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_absolute():
            candidate = _repo_root() / candidate
        return candidate if candidate.is_file() else None

    base_dir = str(section.get("dir") or "").strip()
    root = Path(base_dir).expanduser() if base_dir else (_repo_root() / "exceptions")
    if not project_name:
        return None
    candidate = root / f"{project_name}.yaml"
    return candidate if candidate.is_file() else None


def _parse_expiry(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, bool):
        return value
    text = str(value or "").strip()
    if not text:
        raise ValueError("expires is required")
    # Accept a plain ISO date or a full ISO datetime (date part used).
    iso = text.replace("Z", "+00:00")
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(iso)
        return parsed.date()
    except ValueError as exc:
        raise ValueError(f"expires is not a valid ISO date: {text!r}") from exc


def _entries_from_document(loaded: Any) -> list[dict[str, Any]]:
    if isinstance(loaded, list):
        return [item for item in loaded if isinstance(item, dict)]
    if isinstance(loaded, dict):
        raw = loaded.get("exceptions")
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise ExceptionsConfigError("exceptions: must be a list")
        return [item for item in raw if isinstance(item, dict)]
    if loaded in (None, {}):
        return []
    raise ExceptionsConfigError("exceptions file must be a mapping or a list")


def parse_entries(loaded: Any, *, today: date, max_days: int = DEFAULT_MAX_DAYS) -> list[ExceptionEntry]:
    """Validate every entry; raise :class:`ExceptionsConfigError` on the first bad one."""
    entries: list[ExceptionEntry] = []
    for index, item in enumerate(_entries_from_document(loaded), start=1):
        match = str(item.get("match") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not match:
            raise ExceptionsConfigError(f"exception #{index}: 'match' is required")
        if not reason:
            raise ExceptionsConfigError(f"exception #{index} (match={match!r}): 'reason' is required and non-empty")
        if "expires" not in item or item.get("expires") in (None, ""):
            raise ExceptionsConfigError(f"exception #{index} (match={match!r}): 'expires' is required")
        try:
            expires = _parse_expiry(item.get("expires"))
        except ValueError as exc:
            raise ExceptionsConfigError(f"exception #{index} (match={match!r}): {exc}") from exc
        if expires > today + timedelta(days=max_days):
            raise ExceptionsConfigError(
                f"exception #{index} (match={match!r}): 'expires' {expires.isoformat()} is more "
                f"than {max_days} days ahead"
            )
        allow_p0 = bool(item.get("allow_p0", False))
        added_by = str(item.get("added_by") or "").strip()
        entries.append(
            ExceptionEntry(
                match=match,
                reason=reason,
                expires=expires,
                added_by=added_by,
                allow_p0=allow_p0,
            )
        )
    return entries


def load_exceptions(
    config: dict[str, Any],
    project_name: str,
    *,
    today: date | None = None,
) -> list[ExceptionEntry]:
    """Load and validate the operator exceptions for one project.

    Returns ``[]`` when no exceptions file exists. Raises
    :class:`ExceptionsConfigError` when a file exists but is invalid, so a broken
    exception is a startup failure rather than a silent gap in coverage.
    """
    today = today or date.today()
    path = resolve_exceptions_path(config, project_name)
    if path is None:
        return []
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ExceptionsConfigError(f"exceptions file is not valid YAML ({path}): {exc}") from exc
    section = config.get("exceptions") or {}
    max_days = int((section.get("max_days") if isinstance(section, dict) else None) or DEFAULT_MAX_DAYS)
    return parse_entries(loaded, today=today, max_days=max_days)

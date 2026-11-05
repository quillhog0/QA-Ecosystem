"""Content-hash analysis cache for deterministic AST and claim inspection."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from core import __version__ as _CORE_VERSION


def compute_content_hash(content: str | bytes, namespace: str = "") -> str:
    """Return a SHA-256 hex digest keyed by QA version, namespace, and file bytes."""
    raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    prefix = f"{_CORE_VERSION}:{namespace}:".encode("utf-8")
    return hashlib.sha256(prefix + raw).hexdigest()


class AnalysisCache:
    """Deterministic file-backed cache indexed by :func:`compute_content_hash`."""

    def __init__(self, cache_dir: Path | str | None = None) -> None:
        env_dir = os.environ.get("QA_ANALYSIS_CACHE_DIR", "").strip()
        default_dir = Path.home() / ".cache" / "qa-ecosystem" / "analysis"
        resolved = Path(cache_dir) if cache_dir is not None else (Path(env_dir) if env_dir else default_dir)
        self.root = resolved

    def _entry_path(self, digest: str) -> Path:
        clean = str(digest).strip().lower()
        return self.root / clean[:2] / f"{clean}.json"

    def get(self, digest: str) -> dict[str, Any] | None:
        """Return cached mapping for ``digest``, or ``None`` on cache miss."""
        path = self._entry_path(digest)
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return loaded if isinstance(loaded, dict) else None

    def put(self, digest: str, payload: dict[str, Any]) -> Path:
        """Store ``payload`` under ``digest`` and return the written path."""
        path = self._entry_path(digest)
        os.makedirs(path.parent, exist_ok=bool(path.parent))
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=not bool(), ensure_ascii=bool()) + "\n",
            encoding="utf-8",
        )
        return path


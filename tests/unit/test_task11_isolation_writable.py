"""Task 11 - Nova/isolation: the isolated copy must be writable like a checkout.

The isolated copy is the tree the project's tests actually run in. Stripping its
write bits turned perfectly legitimate in-tree writes - creating an output
directory, opening a local sqlite database, writing a pid file - into
``PermissionError``/``sqlite3.OperationalError`` and mislabelled them as product
defects (the Quillhog anti-scam ``out/`` mkdir and the record HTTP-500 invoice
persist were exactly this).

The fix keeps the copy writable but never weakens the isolation guarantee: the
*live* tree is a different directory, never mounted writable, and must stay
byte-for-byte identical. These tests exercise the real ``Vesper.run`` path and
perform an in-copy write from inside the (stubbed) execution step, which is where
the read-only hardening used to apply.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from core.orchestrator import Vesper
from core.packets import ExecutionResult
from tests.support import mini_project, tree_sha256


def _clear_execution() -> ExecutionResult:
    return ExecutionResult(collected=1, passed=1, failed=0, exit_code=0, sandbox_status="off")


def _config(tmp_path: Path) -> dict:
    return {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "reports"),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "keep_isolated_copy": False,
    }


def test_isolated_copy_is_writable_at_execution_time(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    before = tree_sha256(project)
    seen = {}

    def probe_isolated(isolated, **_kwargs) -> ExecutionResult:
        # The exact shapes that a read-only copy broke: a brand new subdirectory
        # created with parents, a new file inside it, and a rewrite of a file that
        # already exists in the copy.
        isolated = Path(isolated)
        new_dir = isolated / "out" / "nested-mint"
        new_dir.mkdir(parents=True, exist_ok=True)
        (new_dir / "payload.txt").write_text("ok", encoding="utf-8")
        (isolated / "calculator.py").write_text(
            (isolated / "calculator.py").read_text(encoding="utf-8"), encoding="utf-8"
        )
        seen["wrote"] = (new_dir / "payload.txt").read_text(encoding="utf-8")
        return _clear_execution()

    with patch("agents.nova.execute", side_effect=probe_isolated):
        Vesper(_config(tmp_path)).run(project, project / "docs")

    assert seen.get("wrote") == "ok", "in-copy writes must succeed like a normal checkout"
    # The guarantee we must never lose: the live tree is untouched.
    assert tree_sha256(project) == before


def test_sqlite_can_open_a_database_in_the_copy(tmp_path: Path):
    project = mini_project(tmp_path / "proj")
    captured = {}

    def probe_sqlite(isolated, **_kwargs) -> ExecutionResult:
        import sqlite3

        isolated = Path(isolated)
        db = isolated / "reclaim_vault.db"
        conn = sqlite3.connect(str(db), timeout=5.0)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
            conn.commit()
            captured["opened"] = True
        finally:
            conn.close()
        return _clear_execution()

    with patch("agents.nova.execute", side_effect=probe_sqlite):
        Vesper(_config(tmp_path)).run(project, project / "docs")

    assert captured.get("opened") is True

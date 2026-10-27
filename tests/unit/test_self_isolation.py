from __future__ import annotations

from pathlib import Path

from core.isolation import isolation_destination


def test_self_test_copy_is_not_under_live_reports(tmp_path: Path):
    live = tmp_path / "qa-agent-ecosystem"
    live.mkdir()
    reports = live / "reports"
    reports.mkdir()
    dest = isolation_destination(live_root=live, reports_dir=reports, run_id="self")
    resolved = dest.resolve()
    try:
        resolved.relative_to(reports.resolve())
        nested = True
    except ValueError:
        nested = False
    assert nested is False
    assert not str(resolved).startswith(str(reports.resolve()) + "/")

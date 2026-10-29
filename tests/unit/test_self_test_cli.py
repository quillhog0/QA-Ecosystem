from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.environ.get("QA_AGENT_NOVA") == "1",
    reason="avoid nested self-test while Nova runs this suite",
)
def test_self_test_places_copy_outside_reports_and_finishes(tmp_path: Path):
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "main.py"),
            "--self-test",
            "--reports-dir",
            str(tmp_path / "reports"),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert "isolated_root=" in proc.stdout
    isolated = ""
    for line in proc.stdout.splitlines():
        if line.startswith("isolated_root="):
            isolated = line.split("=", 1)[1]
    assert isolated
    iso = Path(isolated)
    live_reports = (ROOT / "reports").resolve()
    assert not str(iso.resolve()).startswith(str(live_reports) + "/")
    try:
        iso.resolve().relative_to(live_reports)
        nested = True
    except ValueError:
        nested = False
    assert nested is False
    assert "verdict=" in proc.stdout


def test_main_self_test_cleanup_ignores_already_removed_docs(tmp_path: Path):
    from unittest.mock import patch
    import main as main_mod
    from core.packets import FinalReport, Verdict

    fake_docs = tmp_path / "temp_docs_dir"
    fake_docs.mkdir()
    fake_report = FinalReport(
        run_id="r-self",
        verdict=Verdict.CLEAR.value,
        sandbox="bwrap",
        isolated_root=str(tmp_path / "iso"),
    )

    def _run_and_remove_docs(_project: Path, _docs: Path) -> FinalReport:
        fake_docs.rmdir()
        return fake_report

    with patch("main.assemble_self_test_docs", return_value=fake_docs), \
         patch("main.Vesper") as mock_vesper:
        mock_vesper.return_value.run.side_effect = _run_and_remove_docs
        rc = main_mod.main(["--self-test", "--reports-dir", str(tmp_path / "reports")])
    assert rc == 0


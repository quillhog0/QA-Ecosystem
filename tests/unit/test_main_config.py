from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import main


class _FakeVesper:
    captured: dict = {}

    def __init__(self, config: dict) -> None:
        _FakeVesper.captured = config

    def run(self, project: Path, docs: Path) -> SimpleNamespace:
        return SimpleNamespace(
            verdict="CLEAR",
            escalation=None,
            sandbox="off",
            mutation=None,
            run_id="test-run",
            isolated_root="",
        )


def test_strict_flag_keeps_existing_riley_settings(tmp_path: Path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("riley:\n  weak_test_threshold: 7\n", encoding="utf-8")
    docs = tmp_path / "docs.md"
    docs.write_text("# Docs\n", encoding="utf-8")
    monkeypatch.setattr(main, "Vesper", _FakeVesper)

    code = main.main(
        [
            "--project", str(tmp_path),
            "--docs", str(docs),
            "--config", str(cfg),
            "--reports-dir", str(tmp_path / "reports"),
            "--strict",
        ]
    )

    assert code == 0
    riley = _FakeVesper.captured["riley"]
    assert riley["veto_on_uncovered_claims"] is True
    assert riley["weak_test_threshold"] == 7

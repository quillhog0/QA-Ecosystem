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


def test_main_returns_1_when_docs_path_missing(tmp_path: Path, capsys):
    """Missing docs must exit 1, not None/0 (guards `return 1` in main)."""
    code = main.main(
        [
            "--project", str(tmp_path),
            "--docs", str(tmp_path / "does_not_exist"),
            "--config", str(tmp_path / "no_config.yaml"),
        ]
    )
    assert code == 1
    assert "docs path does not exist" in capsys.readouterr().err


def test_main_returns_1_when_project_not_a_directory(tmp_path: Path, capsys):
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")
    docs = tmp_path / "docs.md"
    docs.write_text("# Docs\n", encoding="utf-8")
    code = main.main(
        [
            "--project", str(not_a_dir),
            "--docs", str(docs),
            "--config", str(tmp_path / "no_config.yaml"),
        ]
    )
    assert code == 1
    assert "project path is not a directory" in capsys.readouterr().err

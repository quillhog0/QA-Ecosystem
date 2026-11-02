from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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


_MUTATION_BASELINE = (
    "mutation:\n"
    "  enabled: false\n"
    "  workers: 5\n"
    "  min_score: 0.4\n"
    "  max_mutants: 12\n"
)


@pytest.mark.parametrize(
    "extra_args,preserved_key,preserved_value,set_key,set_value",
    [
        (["--mutation"], "workers", 5, "enabled", True),
        (["--mutation-min-score", "0.9"], "workers", 5, "min_score", 0.9),
        (["--mutation-max-mutants", "7"], "workers", 5, "max_mutants", 7),
        (["--mutation-workers", "2"], "max_mutants", 12, "workers", 2),
    ],
)
def test_mutation_flags_keep_existing_mutation_settings(
    tmp_path: Path,
    monkeypatch,
    extra_args: list[str],
    preserved_key: str,
    preserved_value: object,
    set_key: str,
    set_value: object,
):
    """main.py:158-173 - every mutation flag rebinds the configured [mutation]
    section before overriding a single key. ``config.get("mutation") or {}`` may not
    collapse to ``and``: that quietly throws away the operator's other settings."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MUTATION_BASELINE, encoding="utf-8")
    docs = tmp_path / "docs.md"
    docs.write_text("# Docs\n", encoding="utf-8")
    _FakeVesper.captured = {}
    monkeypatch.setattr(main, "Vesper", _FakeVesper)

    code = main.main(
        [
            "--project", str(tmp_path),
            "--docs", str(docs),
            "--config", str(cfg),
            "--reports-dir", str(tmp_path / "reports"),
            *extra_args,
        ]
    )

    assert code == 0
    mutation = _FakeVesper.captured["mutation"]
    assert mutation[preserved_key] == preserved_value
    assert mutation[set_key] == set_value

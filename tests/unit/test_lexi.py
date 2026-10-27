from __future__ import annotations

from pathlib import Path

from agents.lexi import inspect
from core.packets import Verdict
from tests.support import mini_project


def test_lexi_vetoes_documented_function_missing_from_code(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs=(
            "# API\n\n"
            "The public helper `missing_fn` must return a number.\n"
            "Also documented: `add`.\n"
        ),
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict == Verdict.VETO.value
    kinds = {f.kind for f in report.findings}
    assert "missing_code" in kinds


def test_lexi_vetoes_backticked_divide_call_when_missing(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs="# API\n\nPublic helpers `add` and `divide()`.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict == Verdict.VETO.value
    assert any(f.path == "divide" for f in report.findings)


def test_lexi_ignores_config_keys_and_allowlist(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs="# Run\n\nFlags: `dry_run`, `allow_network`, `OPENROUTER_API_KEY`.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={
            "llm": {"provider": "none"},
            "dry_run": True,
            "allow_network": False,
            "lexi": {"ignore_identifiers": ["OPENROUTER_API_KEY"]},
        },
    )
    assert report.verdict != Verdict.VETO.value or not any(
        f.path in {"dry_run", "allow_network", "OPENROUTER_API_KEY"} for f in report.findings
    )

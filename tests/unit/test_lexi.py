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


def test_lexi_ignores_imported_project_libraries(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        code={
            "my_module.py": """from solders.pubkey import Pubkey
import requests""",
        },
        docs="# API\n\nThis document mentions `solders`, `Pubkey`, and `requests`.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict != Verdict.VETO.value
    assert not any(f.path in {"solders", "Pubkey", "requests"} for f in report.findings)


def test_lexi_finds_missing_identifier(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs="# Missing\n\nThis document mentions `FooBarMissing`.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict == Verdict.VETO.value
    assert any(f.path == "FooBarMissing" for f in report.findings)


def test_lexi_uses_doc_globs(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs={
            "guide.rst": "# Guide\n\nThis document mentions `FooBarMissing`.\n",
            "README.md": "# README\n\nThis document mentions `ExistingIdentifier`.\n",
        },
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={
            "llm": {"provider": "none"},
            "dry_run": True,
            "lexi": {"doc_globs": ["**/*.rst"]},
        },
    )
    assert report.verdict == Verdict.VETO.value
    assert any(f.path == "FooBarMissing" for f in report.findings)
    assert not any(f.path == "ExistingIdentifier" for f in report.findings)


def test_lexi_finds_domain_term(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        code={
            "my_code.py": "# FooDomainTerm is the settlement window\ndef my_func(): pass",
        },
        docs="# Terms\n\nThis document mentions `FooDomainTerm`.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict != Verdict.VETO.value
    assert any(f.path == "FooDomainTerm" and f.kind == "domain_term" and f.severity == "low" for f in report.findings)


def test_lexi_extracts_claims(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs=(
            "The API must never return null. Hello world. "
            "This is a sentence with always. "
            "```python\nif x always y:\n    pass\n```"
            "Zaručeně to funguje. "
            "Nikdy to nedělej."
        ),
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    expected_claims = [
        "The API must never return null.",
        "This is a sentence with always.",
        "Zaručeně to funguje.",
        "Nikdy to nedělej.",
    ]
    assert sorted(report.claims) == sorted(expected_claims)
    assert len(report.claims) == 4


def test_lexi_clean_claims_without_markdown_and_intro(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs="Intro:\n\n* **Zero Burns:** Never burns tokens.",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.claims == ["Zero Burns: Never burns tokens."]

from __future__ import annotations

from pathlib import Path

import pytest

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
        config={"llm": {"provider": "none"}, "dry_run": True, "lexi": {"veto_on_missing_code": True}},
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
            "lexi": {"doc_globs": ["**/*.rst"], "veto_on_missing_code": True},
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


def test_lexi_extracts_labeled_bullet_without_keywords(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs="* **MTU Safety:** Payload is capped at 1232 bytes.",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.claims == ["MTU Safety: Payload is capped at 1232 bytes."]


def test_lexi_no_false_drift_on_code_function_term(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        code={
            "core.py": "def create_close_account_instruction():\n    pass\n",
        },
        docs="# API\n\nMentions `CloseAccount`.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert not any(f.kind == "domain_term" for f in report.findings)
    assert not any(f.path == "CloseAccount" for f in report.findings)


def test_lexi_monotonic_veto_llm_cannot_override_to_clear(tmp_path: Path, monkeypatch):
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs="# API\n\nPublic helper `missing_func`.\n",
    )
    import agents.lexi
    monkeypatch.setattr(
        agents.lexi,
        "complete_with_status",
        lambda **kwargs: ("verdict: CLEAR\nfindings: []\nnotes: everything looks great", "ok"),
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "mock"}, "dry_run": False, "lexi": {"veto_on_missing_code": True}},
    )
    assert report.verdict == Verdict.VETO.value
    assert any(f.path == "missing_func" for f in report.findings)



def test_lexi_bare_missing_term_is_warning_by_default(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs="# About\n\nMaintained by `someaccount`. Public helper `add`.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict == Verdict.CLEAR.value
    assert any(f.kind == "missing_code" and f.severity == "medium" for f in report.findings)


def test_lexi_term_present_only_as_string_literal_is_not_missing_code(tmp_path: Path):
    """Regression: a docs term that exists in code as an exact string literal
    (e.g. an env-var fragment or CLI binary name in a policy tuple) must not be
    reported as ``missing_code``. Lexi should still keep the term out of every
    drift bucket because the literal is a real, verbatim code reference."""
    root = mini_project(
        tmp_path / "proj",
        code={
            "bins.py": 'NETWORK_BINARIES = ("netfetch",)\n',
        },
        docs="# Bins\n\nThe tool `netfetch` must never be invoked.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    # No missing_code (or any other) drift finding for a term that lives in
    # code as a bare string literal.
    assert not any(f.path == "netfetch" for f in report.findings)


def test_lexi_term_present_nowhere_is_still_missing_code(tmp_path: Path):
    """A backticked term that appears nowhere in code, tests, comments or other
    sources must still be flagged as ``missing_code``."""
    root = mini_project(
        tmp_path / "proj",
        code={
            "bins.py": 'NETWORK_BINARIES = ("netfetch",)\n',
        },
        docs="# Bins\n\nThe tool `ghostbinary` must never be invoked.\n",
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert any(
        f.kind == "missing_code" and f.path == "ghostbinary" for f in report.findings
    )


def test_lexi_declared_dependency_in_requirements_is_not_missing_code(tmp_path: Path):
    """A docs term that names a distribution listed in ``requirements.txt`` is
    a declared dependency, not drift."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Deps\n\nWe rely on `PyYAML` for parsing.\n",
    )
    (root / "requirements.txt").write_text("PyYAML>=6\n", encoding="utf-8")
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert not any(f.kind == "missing_code" and f.path == "PyYAML" for f in report.findings)


def test_lexi_declared_dependency_matches_by_pep503_normalization(tmp_path: Path):
    """PEP 503 collapses runs of ``[-_.]`` to a single ``-`` and lowercases, so
    a docs term like ``ruamel_yaml`` must match a declared ``ruamel.yaml``."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Deps\n\nImport `ruamel_yaml` at startup.\n",
    )
    (root / "requirements.txt").write_text("ruamel.yaml>=0.18\n", encoding="utf-8")
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert not any(
        f.kind == "missing_code" and f.path == "ruamel_yaml" for f in report.findings
    )


def test_lexi_undeclared_package_like_term_is_still_missing_code(tmp_path: Path):
    """A term shaped like a package but not actually declared must still be
    reported as ``missing_code``."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Deps\n\nWe rely on `PyYAML` and `NotADeclaredPkg` here.\n",
    )
    (root / "requirements.txt").write_text("PyYAML>=6\n", encoding="utf-8")
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert not any(
        f.kind == "missing_code" and f.path == "PyYAML" for f in report.findings
    )
    assert any(
        f.kind == "missing_code" and f.path == "NotADeclaredPkg"
        for f in report.findings
    )


def test_lexi_broken_pyproject_does_not_crash(tmp_path: Path):
    """A malformed ``pyproject.toml`` must not raise: Lexi just falls back to
    treating every term as undeclared and keeps its usual drift behavior."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Deps\n\nWe rely on `PyYAML` and `ghostdep`.\n",
    )
    (root / "pyproject.toml").write_text(
        "[project\ndependencies = [\"PyYAML>=6\"\n", encoding="utf-8"
    )
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    # Because the manifest failed to parse, PyYAML is not treated as declared,
    # so it still surfaces as a (non-veto by default) finding. The important
    # bit is that inspect() returned a report instead of raising.
    paths = {f.path for f in report.findings}
    assert "PyYAML" in paths
    assert "ghostdep" in paths


def test_lexi_project_config_key_unused_in_code_is_missing_code(tmp_path: Path):
    """A key from the config.yaml of the project under test must not hide drift:
    if docs mention it and code never reads it, it is reported."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Config\n\nSet `stale_option` to enable the feature.\n",
    )
    (root / "config.yaml").write_text("stale_option: true\n", encoding="utf-8")
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True, "lexi": {"veto_on_missing_code": True}},
    )
    assert any(
        f.kind == "missing_code" and f.path == "stale_option" for f in report.findings
    )

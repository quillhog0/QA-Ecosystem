from __future__ import annotations

from pathlib import Path

import pytest

from agents.lexi import _git_ref_names, _is_code_term, _requirement_line_name, inspect
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
    flagged = {f.path for f in report.findings}
    assert not flagged & {"dry_run", "allow_network", "OPENROUTER_API_KEY"}


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


def test_is_code_term_three_letter_snake_matches_function_substring():
    """lexi.py:464 - a 3-character snake form is long enough for the substring check."""
    assert _is_code_term("Add", set(), {"address_book"}) is True


def test_is_code_term_two_letter_snake_does_not_match_function_substring():
    """lexi.py:464 - a 2-character snake must not be treated as a code term."""
    assert _is_code_term("Ab", set(), {"cabdriver"}) is False


def test_is_code_term_true_from_case_variant_identifier_not_function():
    """lexi.py:508 - a name whose case-variant is a known code identifier counts
    as a code term even when it matches no function name."""
    # exact ident "MyClass" is absent, but its lowercase variant is a known
    # identifier; the function-name set is empty so only the variant branch fires.
    assert _is_code_term("MyClass", {"myclass"}, set()) is True


def test_lexi_reports_contradiction_when_execution_collects_zero(tmp_path: Path):
    """lexi.py:661 - collected == 0 with a test surface expected is a contradiction."""
    root = mini_project(tmp_path / "proj", docs="# API\n\nPublic helper `add`.\n")
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
        execution={"collected": 0, "passed": 0, "failed": 0, "errors": 0, "exit_code": 0},
    )
    assert any(f.kind == "contradiction" for f in report.findings)


def test_requirement_line_name_returns_none_for_non_requirement_lines():
    """lexi.py:386 - blank and comment-only requirement lines yield None, not truthy."""
    assert _requirement_line_name("# a comment") is None
    assert _requirement_line_name("") is None
    assert _requirement_line_name("  ") is None


def _missing_code_paths(root: Path) -> set[str]:
    report = inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    return {f.path for f in report.findings if f.kind == "missing_code"}


def _make_git_branch(
    project_root: Path, *, head_branch: str, ref_branches: list[str]
) -> None:
    """Create a minimal ``.git`` layout using plain files (no repository init)."""
    git = project_root / ".git"
    (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(f"ref: refs/heads/{head_branch}\n", encoding="utf-8")
    for branch in ref_branches:
        ref = git / "refs" / "heads" / branch
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_text("e3b0c44298fc1c149afbf4c8996fb92427ae41e4\n", encoding="utf-8")


def test_lexi_keyword_argument_name_is_not_missing_code(tmp_path: Path):
    """Case A - a keyword used only as a call kwarg is an existing code term."""
    root = mini_project(
        tmp_path / "proj",
        code={
            "cleanup.py": (
                "import shutil\n\n"
                "def wipe(path):\n"
                "    shutil.rmtree(path, ignore_errors=True)\n"
            )
        },
        docs="# Ops\n\n`wipe` passes `ignore_errors`; `absent_widget` is unknown.\n",
    )
    paths = _missing_code_paths(root)
    assert "ignore_errors" not in paths
    assert "absent_widget" in paths


def test_lexi_function_parameter_name_is_not_missing_code(tmp_path: Path):
    """Case A - a parameter name that the body never rebinds is still a code term."""
    root = mini_project(
        tmp_path / "proj",
        code={"server.py": "def configure(verbosity: int) -> None:\n    return None\n"},
        docs="# Server\n\n`configure` reads `verbosity`; `absent_gadget` is unknown.\n",
    )
    paths = _missing_code_paths(root)
    assert "verbosity" not in paths
    assert "absent_gadget" in paths


def test_lexi_git_branch_from_refs_heads_is_not_missing_code(tmp_path: Path):
    """Case B - a branch present under .git/refs/heads is treated as a known term."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Git\n\n`release` is protected; `absent_flower` is unknown.\n",
    )
    _make_git_branch(root, head_branch="main", ref_branches=["release"])
    paths = _missing_code_paths(root)
    assert "release" not in paths
    assert "absent_flower" in paths


def test_lexi_branch_like_term_is_missing_without_git(tmp_path: Path):
    """Case B - with no .git directory, a bare branch word is still reported."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Git\n\n`release` is protected; `absent_field` is unknown.\n",
    )
    assert not (root / ".git").exists()
    paths = _missing_code_paths(root)
    assert "release" in paths


def test_lexi_absent_term_still_reported_as_missing_code(tmp_path: Path):
    """Constraint - a term that appears nowhere remains missing_code."""
    root = mini_project(
        tmp_path / "proj",
        docs="# Misc\n\nThe helper `totally_unseen_widget` is documented only.\n",
    )
    assert "totally_unseen_widget" in _missing_code_paths(root)


def test_git_ref_names_reads_head_and_refs_and_ignores_missing_git(tmp_path: Path):
    """Case B helper - branch names come only from an existing .git directory."""
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _git_ref_names(plain) == set()

    repo = tmp_path / "repo"
    repo.mkdir()
    _make_git_branch(repo, head_branch="main", ref_branches=["release", "feature/login"])
    assert _git_ref_names(repo) == {"main", "release", "feature/login"}

    detached = tmp_path / "detached"
    _make_git_branch(detached, head_branch="main", ref_branches=[])
    (detached / ".git" / "HEAD").write_text("0123456789abcdef\n", encoding="utf-8")
    assert _git_ref_names(detached) == set()

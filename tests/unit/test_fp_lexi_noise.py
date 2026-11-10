"""Regression: Lexi must drop documented-but-not-API noise without losing real drift."""

from __future__ import annotations

from pathlib import Path

from agents.lexi import inspect
from agents.lexi_claims import check_install_instructions, doc_env_candidates
from core.packets import Verdict
from tests.support import mini_project


def _inspect(root: Path):
    return inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )


def test_lexi_drops_sphinx_inventory_and_dunders(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        docs=(
            "# Docs\n\n"
            "* :ref:`genindex`\n"
            "* :ref:`modindex`\n"
            "* :ref:`search`\n\n"
            "The type implements `__str__`, `__float__` and `__int__`.\n"
        ),
    )
    report = _inspect(root)
    names = {f.path for f in report.findings}
    assert "genindex" not in names
    assert "modindex" not in names
    assert "search" not in names
    assert "__str__" not in names
    assert "__float__" not in names
    assert "__int__" not in names


def test_lexi_drops_historical_constants_in_changelog(tmp_path: Path):
    root = mini_project(tmp_path / "proj", docs="# API\n\nPublic helper `add`.\n")
    (root / "CHANGES.rst").write_text(
        "Changelog\n=========\n\n"
        "- Default for DEFAULT_CURRENCY and DEFAULT_CURRENCY_CODE changed.\n"
        "- DECIMAL_PLACES and ROUNDING documented historically.\n",
        encoding="utf-8",
    )
    report = inspect(
        docs_root=root,
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    names = {f.path for f in report.findings}
    assert "DEFAULT_CURRENCY" not in names
    assert "DEFAULT_CURRENCY_CODE" not in names
    assert "DECIMAL_PLACES" not in names
    assert "ROUNDING" not in names


def test_deity_is_not_an_undocumented_env_var():
    assert doc_env_candidates("($DEITY forbid) this joke stays a joke.") == []
    assert "API_KEY" in doc_env_candidates("Set the environment variable $API_KEY before running.")
    assert "HOME" in doc_env_candidates("export $HOME/bin")


def test_install_ignores_pytest_plugin_but_flags_wrong_project_package(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text(
        "# Install\n\n"
        "pip install pytest-doctestplus\n"
        "pip install wrong-py-pkg\n",
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-lib"\n', encoding="utf-8")
    findings = check_install_instructions(docs, tmp_path)
    texts = " ".join(f.message for f in findings)
    assert "pytest-doctestplus" not in texts
    assert "wrong-py-pkg" in texts

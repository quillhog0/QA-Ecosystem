from __future__ import annotations

from pathlib import Path

from agents.inventory import doc_tokens, split_code_and_tests


def test_split_code_and_tests_classifies_dirs_prefixes_and_suffixes(tmp_path: Path):
    """Tests are detected by tests/ dir, test_ prefix, and _test.py suffix; rest is code."""
    files = [
        tmp_path / "calculator.py",
        tmp_path / "src" / "engine.py",
        tmp_path / "tests" / "test_calculator.py",
        tmp_path / "pkg" / "tests" / "helper.py",
        tmp_path / "root_test.py",
        tmp_path / "tests" / "unit" / "test_nested.py",
    ]
    code, tests = split_code_and_tests(files, tmp_path)
    assert code == [tmp_path / "calculator.py", tmp_path / "src" / "engine.py"]
    assert tests == [
        tmp_path / "tests" / "test_calculator.py",
        tmp_path / "pkg" / "tests" / "helper.py",
        tmp_path / "root_test.py",
        tmp_path / "tests" / "unit" / "test_nested.py",
    ]


def test_split_code_and_tests_is_case_insensitive_without_substring_false_positives(tmp_path: Path):
    files = [
        tmp_path / "TESTS" / "x.py",
        tmp_path / "Test_Bootstrap.py",
        tmp_path / "attest.py",  # contains "test" but must stay code
        tmp_path / "protests" / "views.py",  # dir segment must match exactly
    ]
    code, tests = split_code_and_tests(files, tmp_path)
    assert tests == [tmp_path / "TESTS" / "x.py", tmp_path / "Test_Bootstrap.py"]
    assert code == [tmp_path / "attest.py", tmp_path / "protests" / "views.py"]


def test_doc_tokens_extracts_backticked_ids_and_headings(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# Setup Guide\n\nUse `divide` with `a.b.c` values.\n", encoding="utf-8"
    )
    identifiers, text = doc_tokens(docs)
    assert "divide" in identifiers
    assert "a.b.c" in identifiers
    assert "Setup Guide" in identifiers
    assert "## guide.md" in text
    assert "Use `divide`" in text


def test_doc_tokens_truncates_at_char_limit_and_stops_reading(tmp_path: Path):
    """Overflow cuts the current chunk at limit_chars and skips all later files."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "file_a.md").write_text("x" * 100, encoding="utf-8")
    (docs / "file_b.md").write_text("`BetaIdent` lives here.\n", encoding="utf-8")

    identifiers, text = doc_tokens(docs, limit_chars=10)
    assert len(text) == 10
    assert text == "## file_a."  # first 10 chars of the "## file_a.md\n..." chunk
    assert "BetaIdent" not in identifiers


def test_doc_tokens_on_empty_dir_returns_nothing(tmp_path: Path):
    docs = tmp_path / "docs"
    docs.mkdir()
    identifiers, text = doc_tokens(docs)
    assert identifiers == []
    assert text == ""

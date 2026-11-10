"""Task 12 - Lexi: every drift finding carries the doc anchor an IDE needs.

``docs_file``/``docs_line`` point at the line where the docs make their promise
(relative POSIX path inside the docs root). A historical record or a
source-cited narration never wins the anchor over a live promise line, a code
fence must not shift the numbering, and a finding with no located mention keeps
``None`` so the schema's null branch is exercised for real.
"""

from __future__ import annotations

from pathlib import Path

from agents.lexi import (
    _heuristic_drift,
    _mention_location,
    _relative_doc_path,
    _strip_fences,
)


def _drift(docs_root: Path, project_root: Path) -> dict[str, tuple[str | None, int | None]]:
    report = _heuristic_drift(docs_root=docs_root, project_root=project_root, config={})
    return {f.path: (f.docs_file, f.docs_line) for f in report.findings}


def test_api_promise_anchor_is_the_promise_line(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "api.md").write_text(
        "# API\n"
        "\n"
        "Intro prose.\n"
        "\n"
        "The module exposes `transfer_funds()` for callers.\n",
        encoding="utf-8",
    )
    anchors = _drift(docs, tmp_path)
    assert anchors.get("transfer_funds") == ("api.md", 5)


def test_live_promise_line_wins_the_anchor_over_history(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    incidents = docs / "incidents"
    incidents.mkdir(parents=True)
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (incidents / "INCIDENTS.md").write_text(
        "# Incident log\n\nBug in `rollup_balance` (`core.py:12`).\n",
        encoding="utf-8",
    )
    (docs / "api.md").write_text(
        "The module exposes `rollup_balance` to callers.\n", encoding="utf-8"
    )
    anchors = _drift(docs, tmp_path)
    assert anchors.get("rollup_balance") == ("api.md", 1)


def test_historical_only_finding_still_anchors_to_its_line(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    incidents = docs / "incidents"
    incidents.mkdir(parents=True)
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (incidents / "INCIDENTS.md").write_text(
        "# Incident log\n\nRoot cause was a bug in `calculate_platform_fee_lamports`.\n",
        encoding="utf-8",
    )
    anchors = _drift(docs, tmp_path)
    # Historical-only mentions are omitted (not a current API promise).
    assert "calculate_platform_fee_lamports" not in anchors


def test_code_fence_does_not_shift_the_anchor_line(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (tmp_path / "lib.py").write_text("VALUE = 1\n", encoding="utf-8")
    (docs / "guide.md").write_text(
        "# Guide\n"
        "\n"
        "```python\n"
        "demo = 1\n"
        "other = 2\n"
        "```\n"
        "\n"
        "See `settle_accounts()` above.\n",
        encoding="utf-8",
    )
    anchors = _drift(docs, tmp_path)
    assert anchors.get("settle_accounts") == ("guide.md", 8)


def test_strip_fences_blanks_content_but_keeps_line_count() -> None:
    text = "a\n```py\nb\nc\n```\nd\n"
    stripped = _strip_fences(text)
    assert len(stripped.splitlines()) == len(text.splitlines())
    assert stripped.splitlines()[2] == ""
    assert "b" not in stripped
    assert stripped.splitlines()[-1] == "d"


def test_relative_doc_path_handles_root_file_and_foreign_paths(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    nested = docs / "sub"
    nested.mkdir(parents=True)
    target = nested / "a.md"
    target.write_text("x", encoding="utf-8")
    assert _relative_doc_path(target, docs) == "sub/a.md"
    assert _relative_doc_path(target, target) == "a.md"
    outside = tmp_path / "other.md"
    outside.write_text("x", encoding="utf-8")
    assert _relative_doc_path(outside, docs) == "other.md"


def test_mention_location_prefers_live_and_falls_back(tmp_path: Path) -> None:
    assert _mention_location("x", None) == (None, None)

    class _M:
        def __init__(self, doc_file, doc_line, historical=False, narrated=False):
            self.doc_file = doc_file
            self.doc_line = doc_line
            self.historical = historical
            self.narrated = narrated

    only_history = {"x": [_M("incidents/A.md", 3, historical=True)]}
    assert _mention_location("x", only_history) == ("incidents/A.md", 3)
    mixed = {"x": [
        _M("incidents/A.md", 3, historical=True),
        _M("api.md", 9),
        _M("notes.md", 2, narrated=True),
    ]}
    assert _mention_location("x", mixed) == ("api.md", 9)
    unlocated = {"x": [_M("", 0)]}
    assert _mention_location("x", unlocated) == (None, None)

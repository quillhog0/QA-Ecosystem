"""Regression: likely-equivalent mutants and a clearer red-baseline mutation skip."""

from __future__ import annotations

from pathlib import Path

from core.mutation import (
    annotate_equivalent_mutants,
    function_return_unused_by_callers,
    find_all_mutants,
)


def test_unused_return_is_detected(tmp_path: Path):
    src = tmp_path / "pkg"
    src.mkdir()
    (src / "rows.py").write_text(
        "def _append_basic_row(lines, row):\n"
        "    lines.append(row)\n"
        "    return lines\n"
        "\n"
        "def build():\n"
        "    lines = []\n"
        "    _append_basic_row(lines, 'a')\n"
        "    _append_basic_row(lines, 'b')\n"
        "    return lines\n",
        encoding="utf-8",
    )
    assert function_return_unused_by_callers(tmp_path, "_append_basic_row") is True
    mutants = find_all_mutants(tmp_path)
    returns = [m for m in mutants if m.mutation_type == "return_value" and m.file_path.endswith("rows.py")]
    assert returns
    annotate_equivalent_mutants(tmp_path, returns)
    marked = [m for m in returns if m.likely_equivalent]
    assert marked, returns
    assert all("_append_basic_row" in (m.equivalent_reason or "") for m in marked)
    assert "unused" in marked[0].equivalent_reason


def test_used_return_is_not_marked_equivalent(tmp_path: Path):
    (tmp_path / "mod.py").write_text(
        "def add(a, b):\n"
        "    return a + b\n"
        "\n"
        "def use():\n"
        "    return add(1, 2)\n",
        encoding="utf-8",
    )
    assert function_return_unused_by_callers(tmp_path, "add") is False
    mutants = [m for m in find_all_mutants(tmp_path) if m.mutation_type == "return_value"]
    annotate_equivalent_mutants(tmp_path, mutants)
    assert mutants
    assert all(not m.likely_equivalent for m in mutants)

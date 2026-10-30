from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.mutation import (
    apply_mutation_to_source,
    collect_file_mutants,
    find_all_mutants,
    is_mutable_file,
    select_mutants,
)
from core.orchestrator import Vesper
from core.packets import Verdict
from tests.support import mini_project


def test_mutation_collector_and_applier_comparison(tmp_path: Path):
    src = "def cmp(a, b):\n    return a < b and a == b\n"
    f = tmp_path / "foo.py"
    f.write_text(src, encoding="utf-8")
    mutants = collect_file_mutants(f, "foo.py")

    cmp_muts = [m for m in mutants if m.mutation_type == "comparison"]
    assert len(cmp_muts) == 2
    assert cmp_muts[0].original == "<"
    assert cmp_muts[0].mutated == "<="
    assert cmp_muts[1].original == "=="
    assert cmp_muts[1].mutated == "!="

    # Apply first comparison mutation (< to <=)
    mutated = apply_mutation_to_source(src, cmp_muts[0].node_index)
    assert "a <= b" in mutated


def test_mutation_collector_and_applier_arithmetic(tmp_path: Path):
    src = "def calc(x, y):\n    return x + y - 1\n"
    f = tmp_path / "calc.py"
    f.write_text(src, encoding="utf-8")
    mutants = collect_file_mutants(f, "calc.py")

    arith_muts = [m for m in mutants if m.mutation_type == "arithmetic"]
    assert len(arith_muts) == 2
    assert {m.original for m in arith_muts} == {"+", "-"}

    plus_mut = next(m for m in arith_muts if m.original == "+")
    mutated_add = apply_mutation_to_source(src, plus_mut.node_index)
    assert "x - y" in mutated_add


def test_mutation_collector_and_applier_bool_constant(tmp_path: Path):
    src = "def flags():\n    a = True\n    b = False\n    return a\n"
    f = tmp_path / "flags.py"
    f.write_text(src, encoding="utf-8")
    mutants = collect_file_mutants(f, "flags.py")

    bool_muts = [m for m in mutants if m.mutation_type == "bool_constant"]
    assert len(bool_muts) == 2
    assert bool_muts[0].original == "True"
    assert bool_muts[0].mutated == "False"
    assert bool_muts[1].original == "False"
    assert bool_muts[1].mutated == "True"

    mutated = apply_mutation_to_source(src, bool_muts[0].node_index)
    assert "a = False" in mutated


def test_mutation_collector_and_applier_return_value(tmp_path: Path):
    src = "def get_val():\n    return 42\ndef get_none():\n    return None\n"
    f = tmp_path / "vals.py"
    f.write_text(src, encoding="utf-8")
    mutants = collect_file_mutants(f, "vals.py")

    ret_muts = [m for m in mutants if m.mutation_type == "return_value"]
    assert len(ret_muts) == 2

    # return 42 -> return None
    mutated_expr = apply_mutation_to_source(src, ret_muts[0].node_index)
    assert "return None" in mutated_expr

    # return None -> return True
    mutated_none = apply_mutation_to_source(src, ret_muts[1].node_index)
    assert "return True" in mutated_none


def test_mutation_collector_and_applier_bool_op(tmp_path: Path):
    src = "def check(a, b):\n    return a and b\n"
    f = tmp_path / "logic.py"
    f.write_text(src, encoding="utf-8")
    mutants = collect_file_mutants(f, "logic.py")

    op_muts = [m for m in mutants if m.mutation_type == "bool_op"]
    assert len(op_muts) == 1
    assert op_muts[0].original == "and"
    assert op_muts[0].mutated == "or"

    mutated = apply_mutation_to_source(src, op_muts[0].node_index)
    assert "a or b" in mutated


def test_is_mutable_file_filtering():
    assert is_mutable_file(Path("core/runner.py")) is True
    assert is_mutable_file(Path("calculator.py")) is True
    assert is_mutable_file(Path("tests/test_foo.py")) is False
    assert is_mutable_file(Path("conftest.py")) is False
    assert is_mutable_file(Path("tests/conftest.py")) is False
    assert is_mutable_file(Path(".venv/lib/python3.11/site-packages/pkg.py")) is False
    assert is_mutable_file(Path("benchmark/fixtures/clean/calculator.py")) is False


def test_deterministic_seed_selection(tmp_path: Path):
    src = "\n".join(f"def f{i}(): return {i} + 1" for i in range(50))
    f = tmp_path / "large.py"
    f.write_text(src, encoding="utf-8")
    mutants = find_all_mutants(tmp_path)
    assert len(mutants) >= 50

    sel1 = select_mutants(mutants, max_mutants=10, seed=42)
    sel2 = select_mutants(mutants, max_mutants=10, seed=42)
    sel3 = select_mutants(mutants, max_mutants=10, seed=99)

    assert len(sel1) == 10
    assert [m.id for m in sel1] == [m.id for m in sel2]
    assert [m.id for m in sel1] != [m.id for m in sel3]


def test_integration_strong_tests_kill_mutants(tmp_path: Path):
    proj = tmp_path / "strong_proj"
    proj.mkdir(parents=True, exist_ok=True)
    tests_dir = proj / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = proj / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    (proj / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    (tests_dir / "test_calc.py").write_text(
        "from calculator import add\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(-1, 1) == 0\n"
        "def test_negative():\n"
        "    assert add(1, 1) != 3\n",
        encoding="utf-8",
    )
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (docs_dir / "overview.md").write_text(
        "# Calculator\nFunction `add` returns the sum of two integers.\n",
        encoding="utf-8",
    )

    reports = tmp_path / "reports"
    config = {
        "mutation": {"enabled": True, "min_score": 0.8},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(config).run(proj, docs_dir)
    assert report.verdict == Verdict.CLEAR.value
    assert report.mutation.get("status") == "completed"
    assert report.mutation.get("score") == 1.0
    assert report.mutation.get("killed") > 0
    assert report.mutation.get("survived") == 0


def test_integration_weak_tests_survive_and_trigger_veto(tmp_path: Path):
    proj = tmp_path / "weak_proj"
    proj.mkdir(parents=True, exist_ok=True)
    tests_dir = proj / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = proj / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    (proj / "calculator.py").write_text(
        "def check_threshold(val: int) -> bool:\n"
        "    if val > 50:\n"
        "        return True\n"
        "    return False\n",
        encoding="utf-8",
    )
    (tests_dir / "test_calc.py").write_text(
        "from calculator import check_threshold\n"
        "def test_threshold_high():\n"
        "    assert check_threshold(100) is True\n"
        "def test_threshold_negative():\n"
        "    assert check_threshold(-10) is False\n",
        encoding="utf-8",
    )
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    (docs_dir / "overview.md").write_text(
        "# Calculator\nFunction `check_threshold` checks values.\n",
        encoding="utf-8",
    )

    reports = tmp_path / "reports"
    config = {
        "mutation": {"enabled": True, "min_score": 0.85},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(config).run(proj, docs_dir)
    assert report.verdict == Verdict.VETO.value
    assert report.mutation.get("status") == "completed"
    assert report.mutation.get("survived") > 0
    assert report.mutation.get("score") < 0.85
    assert any("mutation score" in r.summary for r in report.role_results)


def test_tested_project_cannot_enable_or_modify_mutation_config(tmp_path: Path):
    proj = mini_project(tmp_path / "proj")
    # Project attempts to force mutation testing and set an impossible threshold
    (proj / "config.yaml").write_text(
        "mutation:\n  enabled: true\n  min_score: 0.99\n",
        encoding="utf-8",
    )
    reports = tmp_path / "reports"
    system_config = {
        "mutation": {"enabled": False},
        "reports_dir": str(reports),
        "isolation_method": "copy",
    }
    report = Vesper(system_config).run(proj, proj / "docs")
    assert report.verdict == Verdict.CLEAR.value
    assert report.mutation.get("status") == "skipped"
    assert report.mutation.get("reason") == "disabled"


def test_mutant_is_not_killed_when_no_tests_ran(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A crash before any test runs (e.g. missing interpreter) is an error, not a kill."""
    import core.mutation as mutation
    from core.packets import ExecutionResult

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    monkeypatch.setattr(
        mutation, "run_pytest", lambda *a, **k: ExecutionResult(exit_code=1, stderr="execvp: No such file")
    )
    res = mutation.run_mutation_testing(tmp_path, work_dir=tmp_path / "w", config={})
    assert res["killed"] == 0
    assert res["score"] == 0.0


def test_mutation_verdict_clear_when_score_meets_min_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A score at or above min_score must stay CLEAR (guards `min_score > 0 and score < min_score`)."""
    import core.mutation as mutation
    from core.packets import ExecutionResult

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # Every mutant fails the suite -> all killed, score 1.0
    monkeypatch.setattr(
        mutation,
        "run_pytest",
        lambda *a, **k: ExecutionResult(exit_code=1, collected=1, failed=1),
    )
    res = mutation.run_mutation_testing(
        tmp_path,
        work_dir=tmp_path / "w",
        config={"mutation": {"min_score": 0.5}},
    )
    assert res["score"] == 1.0
    assert res["killed"] > 0
    assert res["survived"] == 0
    assert res["verdict"] == Verdict.CLEAR.value


def test_mutation_verdict_clear_when_score_equals_min_score(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """score == min_score must stay CLEAR (guards `score < min_score`, not `<=`)."""
    import core.mutation as mutation
    from core.packets import ExecutionResult

    (tmp_path / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    # Two mutable points (+ and return): first killed, second survives -> score 0.5
    results = iter([
        ExecutionResult(exit_code=1, collected=1, failed=1),
        ExecutionResult(exit_code=0, collected=1, passed=1),
    ])
    monkeypatch.setattr(mutation, "run_pytest", lambda *a, **k: next(results))
    res = mutation.run_mutation_testing(
        tmp_path,
        work_dir=tmp_path / "w",
        config={"mutation": {"min_score": 0.5}},
    )
    assert res["killed"] == 1
    assert res["survived"] == 1
    assert res["score"] == 0.5
    assert res["verdict"] == Verdict.CLEAR.value

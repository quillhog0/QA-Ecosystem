"""Unit tests for plan design and review determinism across Python and TS."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agents import aria, riley
from core.packets import TestPlan, Verdict


def test_python_same_input_twice_same_plan_decision(tmp_path: Path):
    """Assert Python project produces identical plan and review decision on repeated runs."""
    proj = tmp_path / "py_proj"
    proj.mkdir()
    docs = proj / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# Math Lib\nProvides add and subtract.\n", encoding="utf-8")
    src = proj / "math_lib"
    src.mkdir()
    (src / "__init__.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
    tests = proj / "tests"
    tests.mkdir()
    (tests / "test_math.py").write_text(
        "from math_lib import add\n"
        "def test_add(): assert add(2, 3) == 5\n"
        "def test_invalid(): assert add(0, 0) == 0\n",
        encoding="utf-8",
    )

    config = {"llm": {"provider": "none"}, "dry_run": True}

    # Run 1
    plan1 = aria.design(docs_root=docs, project_root=proj, config=config, gaps=[], claims=[])
    rev1 = riley.review_plan(plan1, config=config, project_root=proj)

    # Run 2
    plan2 = aria.design(docs_root=docs, project_root=proj, config=config, gaps=[], claims=[])
    rev2 = riley.review_plan(plan2, config=config, project_root=proj)

    assert plan1.to_dict() == plan2.to_dict()
    assert rev1.verdict == rev2.verdict == Verdict.CLEAR.value
    assert rev1.gaps == rev2.gaps
    assert rev1.root_cause == rev2.root_cause
    assert rev1.notes == rev2.notes


def test_ts_same_input_twice_cold_vs_warm_cache(tmp_path: Path, monkeypatch):
    """Assert TS project produces identical plan and review decision from clean cache to warm cache."""
    cache_dir = tmp_path / "cache"
    tooling_dir = cache_dir / "tooling"
    monkeypatch.setenv("QA_TOOLING_DIR", str(tooling_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))

    proj = tmp_path / "ts_proj"
    proj.mkdir()
    docs = proj / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# TS Service\nService documentation without error keywords.\n", encoding="utf-8")
    (proj / "package.json").write_text(json.dumps({"name": "ts-proj", "type": "module"}), encoding="utf-8")

    src = proj / "src"
    src.mkdir()
    (src / "service.ts").write_text("export function runTask(x: number) { return x * 3; }\n", encoding="utf-8")

    tests = proj / "tests"
    tests.mkdir()
    (tests / "helpers.ts").write_text(
        "import { expect } from 'vitest';\n"
        "export function assertTriple(val: number, exp: number) {\n"
        "  expect(val).toBe(exp * 3);\n"
        "}\n",
        encoding="utf-8",
    )
    (tests / "service.test.ts").write_text(
        "import { it, expect } from 'vitest';\n"
        "import { runTask } from '../src/service.js';\n"
        "import { assertTriple } from './helpers.js';\n"
        "it('verifies triple', () => {\n"
        "  assertTriple(runTask(4), 4);\n"
        "});\n",
        encoding="utf-8",
    )

    config = {"llm": {"provider": "none"}, "dry_run": True}

    # Run 1: Clean cache (no tooling/typescript.js pre-existing in cache)
    assert not (tooling_dir / "typescript.js").exists()
    plan1 = aria.design(docs_root=docs, project_root=proj, config=config, gaps=[], claims=[])
    rev1 = riley.review_plan(plan1, config=config, project_root=proj)

    # Run 2: Cache is now warm
    plan2 = aria.design(docs_root=docs, project_root=proj, config=config, gaps=[], claims=[])
    rev2 = riley.review_plan(plan2, config=config, project_root=proj)

    assert plan1.to_dict() == plan2.to_dict()
    assert rev1.verdict == rev2.verdict == Verdict.CLEAR.value
    assert rev1.gaps == rev2.gaps
    assert rev1.root_cause == rev2.root_cause
    assert rev1.notes == rev2.notes
    assert any(s.negative for s in plan1.scenarios)
    assert any(s.negative for s in plan2.scenarios)


def test_ts_analyzer_helper_discovery_order_independent(tmp_path: Path):
    """Test that ts_analyzer discovers assertion helpers regardless of file order."""
    analyzer_js = Path(__file__).resolve().parents[2] / "agents" / "ts_analyzer.js"
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not available")

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text("{}", encoding="utf-8")
    tests = proj / "tests"
    tests.mkdir()

    # Case A: test file sorts alphabetically before helper file (a_test.ts < z_helper.ts)
    (tests / "a_test.ts").write_text(
        "import { it } from 'vitest';\n"
        "import { myAssert } from './z_helper.js';\n"
        "it('calls helper', () => { myAssert(1); });\n",
        encoding="utf-8",
    )
    (tests / "z_helper.ts").write_text(
        "import { expect } from 'vitest';\n"
        "export function myAssert(x: number) { expect(x).toBe(1); }\n",
        encoding="utf-8",
    )

    proc_a = subprocess.run(
        [node_bin, str(analyzer_js), "--project", str(proj)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_a.returncode == 0
    data_a = json.loads(proc_a.stdout)
    assert data_a["weak_tests"] == []

    # Case B: helper file sorts alphabetically before test file (a_helper.ts < z_test.ts)
    (tests / "a_test.ts").unlink()
    (tests / "z_helper.ts").unlink()

    (tests / "a_helper.ts").write_text(
        "import { expect } from 'vitest';\n"
        "export function myAssert(x: number) { expect(x).toBe(1); }\n",
        encoding="utf-8",
    )
    (tests / "z_test.ts").write_text(
        "import { it } from 'vitest';\n"
        "import { myAssert } from './a_helper.js';\n"
        "it('calls helper', () => { myAssert(1); });\n",
        encoding="utf-8",
    )

    proc_b = subprocess.run(
        [node_bin, str(analyzer_js), "--project", str(proj)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_b.returncode == 0
    data_b = json.loads(proc_b.stdout)
    assert data_b["weak_tests"] == []


def test_ts_analyzer_try_catch_cli_outside_assertion_not_swallowed(tmp_path: Path):
    """Test that try/catch executing CLI/throwing code with assertions outside is not flagged as weak."""
    analyzer_js = Path(__file__).resolve().parents[2] / "agents" / "ts_analyzer.js"
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not available")

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text("{}", encoding="utf-8")
    tests = proj / "tests"
    tests.mkdir()

    (tests / "cli.test.ts").write_text(
        "import { it, expect } from 'vitest';\n"
        "import { execFileSync } from 'node:child_process';\n"
        "it('cli exits non-zero on error', () => {\n"
        "  let failed = false;\n"
        "  try {\n"
        "    execFileSync('bad-cmd', []);\n"
        "  } catch (err) {\n"
        "    failed = true;\n"
        "  }\n"
        "  expect(failed).toBe(true);\n"
        "});\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [node_bin, str(analyzer_js), "--project", str(proj)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert data["weak_tests"] == []


@pytest.mark.skipif(
    os.environ.get("QA_AGENT_NOVA") == "1",
    reason="nested fixture runs cannot run inside Nova sandbox",
)
def test_fixture_determinism_twice_identical_verdicts(tmp_path: Path):
    """Run ts_plan_determinism and py_plan_determinism fixtures twice and verify identical plan & verdict."""
    from benchmark.run_benchmark import run_fixture

    bench_root = Path(__file__).resolve().parents[2] / "benchmark" / "fixtures"

    for fx_name in ("ts_plan_determinism", "py_plan_determinism"):
        fx_dir = bench_root / fx_name
        res1 = run_fixture(fx_dir, tmp_path / f"{fx_name}_run1")
        res2 = run_fixture(fx_dir, tmp_path / f"{fx_name}_run2")

        assert res1["verdict"] == res2["verdict"] == "CLEAR"
        assert res1["exit_code"] == res2["exit_code"] == 0


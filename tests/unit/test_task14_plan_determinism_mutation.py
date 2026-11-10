"""Tests targeting mutation coverage for Task 14 plan determinism changes.

Modules covered:
- agents/aria.py (TS test collection, name stem parsing, pure TS vs mixed scenario generation)
- agents/inventory.py (JS public names and error branches)
- agents/riley.py (weak_tests deterministic sort, TS analyzer QA_TOOLING_DIR env, uncovered claims config)
- core/toolchain.py (ensure_ts_tooling project_root and node_modules cache fallback, boundary checks)
- policies/sable.py (Sable TS scanner QA_TOOLING_DIR env, findings parsing, and subprocess flags)
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents import aria, inventory, riley
from agents.riley import WeakTestHit
from core import toolchain
from core.packets import ExecutionResult, Scenario, TestPlan, Verdict
from policies import sable


def test_aria_ts_test_collection_window_and_slice(tmp_path: Path):
    """Test TS test body collection captures the 40-line window without negative index slicing."""
    test_file = tmp_path / "sample.test.ts"
    lines = [f"// line {i}" for i in range(30)]
    lines[10] = "it('runs task', () => {"
    lines[20] = "  expect(1).toBe(1);"
    lines[25] = "});"
    test_file.write_text("\n".join(lines), encoding="utf-8")

    tests = aria._collect_test_functions([test_file], tmp_path)
    assert len(tests) == 1
    test_id, title, body = tests[0]
    assert test_id == "sample.test.ts::runs task"
    assert title == "runs task"
    # Ensure line 20 is captured in the body text even when file is short
    assert "expect(1).toBe(1);" in body
    assert len(body) > 0


def test_aria_token_matching_stems():
    """Test token stem extraction for 3-letter words and STOP_WORDS filtering."""
    # 3-letter word "add" must be kept, 2-letter word "to" excluded, "with" is in STOP_WORDS
    tests = [
        ("test_math.py::test_add", "test_add", "assert add(2, 3) == 5"),
    ]
    words = aria._compute_common_body_words(tests)
    # Claim matches single 3-letter word "add"
    covered, score = aria._find_covering_tests_scored("can add", tests, words)
    assert "test_math.py::test_add" in covered
    assert score > 0

    # Stop words in test name must NOT match even if keywords had a stop word stem (e.g. "uses" -> stem "use" in STOP_WORDS, or "sets" -> stem "set" in STOP_WORDS)
    tests_stop_stem = [
        ("test_stop.py::test_use_set", "test_use_set", "pass"),
    ]
    covered_ss, score_ss = aria._find_covering_tests_scored("uses sets", tests_stop_stem, words)
    assert not covered_ss
    assert score_ss == 0

    # 2-letter part in test name (e.g. "do") must NOT match a 3-letter keyword whose stem is 2 letters (e.g. "dos" -> not stemmed since len<=3, or "ies" > 4)
    # Also test that a 3-letter word in test name matches a plural claim keyword ("adds" -> stem "add" matches test_add)
    tests_plural_claim = [
        ("test_math.py::test_add", "test_add", "pass"),
    ]
    covered_pl, score_pl = aria._find_covering_tests_scored("adds values", tests_plural_claim, set())
    assert "test_math.py::test_add" in covered_pl
    assert score_pl == 3

    # Stop words alone must not produce stems or spurious matches
    tests_stop = [
        ("test_stop.py::test_for_from", "test_for_from", "pass"),
        ("test_stop.py::test_to", "test_to", "pass"),
    ]
    covered_stop, score_stop = aria._find_covering_tests_scored("for from to", tests_stop, words)
    assert not covered_stop
    assert score_stop == 0.0


def test_aria_pure_ts_vs_mixed_scenarios(tmp_path: Path):
    """Test _heuristic_plan distinguishes pure TS projects from mixed Python/TS projects."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# Docs\nTesting instructions.\n", encoding="utf-8")

    # 1. Pure TS project
    proj_ts = tmp_path / "proj_ts"
    proj_ts.mkdir()
    (proj_ts / "package.json").write_text("{}", encoding="utf-8")
    (proj_ts / "tests").mkdir()
    (proj_ts / "tests" / "app.test.ts").write_text("it('works', () => {});", encoding="utf-8")

    plan_ts = aria.design(docs_root=docs, project_root=proj_ts, config={"llm": {"provider": "none"}}, gaps=[], claims=["app works"])
    s1_ts = next(s for s in plan_ts.scenarios if s.id == "S1")
    s3_ts = next(s for s in plan_ts.scenarios if s.id == "S3")
    c1_ts = next(s for s in plan_ts.scenarios if s.id == "C1")

    assert s1_ts.title == "Existing test suite completes"
    assert "Run the isolated test suite against the project copy" in s1_ts.steps
    assert "test runner collects more than 0 tests" in s1_ts.acceptance
    assert s1_ts.negative is False
    assert c1_ts.negative is False
    assert "the test suite must report failures rather than hiding the error" in s3_ts.steps[0]
    assert s3_ts.negative is True

    # 2. Mixed project (TS and Python)
    proj_mixed = tmp_path / "proj_mixed"
    proj_mixed.mkdir()
    (proj_mixed / "package.json").write_text("{}", encoding="utf-8")
    (proj_mixed / "tests").mkdir()
    (proj_mixed / "tests" / "app.test.ts").write_text("it('works', () => {});", encoding="utf-8")
    (proj_mixed / "tests" / "test_app.py").write_text("def test_app(): pass", encoding="utf-8")

    plan_mixed = aria.design(docs_root=docs, project_root=proj_mixed, config={"llm": {"provider": "none"}}, gaps=[], claims=[])
    s1_mixed = next(s for s in plan_mixed.scenarios if s.id == "S1")
    s3_mixed = next(s for s in plan_mixed.scenarios if s.id == "S3")

    assert s1_mixed.title == "Existing pytest suite completes"
    assert "Run the isolated pytest suite against the project copy" in s1_mixed.steps
    assert "pytest collects more than 0 tests" in s1_mixed.acceptance
    assert "pytest must report failed > 0 rather than hiding the error" in s3_mixed.steps[0]

    # 3. Error talk in docs generates S3 "Invalid input is rejected" even when gaps are empty
    docs_err = tmp_path / "docs_err"
    docs_err.mkdir()
    (docs_err / "README.md").write_text("# Docs\nMust raise error on invalid input.\n", encoding="utf-8")
    plan_err = aria.design(docs_root=docs_err, project_root=proj_ts, config={"llm": {"provider": "none"}}, gaps=[], claims=[])
    assert any(s.title == "Invalid input is rejected" for s in plan_err.scenarios)


def test_inventory_public_names_js(tmp_path: Path):
    """Test public_names extracts exports from .js files and handles errors gracefully."""
    js_file = tmp_path / "service.js"
    js_file.write_text("export function doSomething() {}\nexport const API_VERSION = 1;\n", encoding="utf-8")
    names = inventory.public_names(js_file)
    assert isinstance(names, list)
    assert "doSomething" in names
    assert "API_VERSION" in names

    # Nonexistent file returns empty list, never None
    nonexistent = tmp_path / "missing.js"
    res = inventory.public_names(nonexistent)
    assert res == []


def test_toolchain_ensure_ts_tooling_project_and_cache(tmp_path: Path, monkeypatch):
    """Test ensure_ts_tooling copies compiler strictly from QA vendor and verifies SHA-256."""
    cache_dir = tmp_path / "cache"
    tooling_dir = cache_dir / "tooling"
    monkeypatch.setenv("QA_TOOLING_DIR", str(tooling_dir))
    monkeypatch.setattr(toolchain, "_DEFAULT_CACHE_HOME", tmp_path / "empty_default_cache")

    # Calling twice on existing directory must succeed without FileExistsError (exist_ok=True)
    assert toolchain.ensure_ts_tooling() == tooling_dir
    assert toolchain.ensure_ts_tooling() == tooling_dir

    # Case 1: tooling_dir already has valid typescript.js (> 1000 bytes) matching sha
    ts_js = tooling_dir / "typescript.js"
    ts_js.write_text("x" * 1500, encoding="utf-8")
    import hashlib
    sha = hashlib.sha256(ts_js.read_bytes()).hexdigest()
    (tooling_dir / "typescript.sha256").write_text(sha, encoding="utf-8")
    assert toolchain.ensure_ts_tooling() == tooling_dir

    # Boundary check: file with exactly 1000 bytes is not considered complete compiler
    ts_js.write_text("x" * 1000, encoding="utf-8")
    assert ts_js.stat().st_size == 1000

    # Project node_modules must NEVER be copied
    proj = tmp_path / "proj"
    proj_ts = proj / "node_modules" / "typescript" / "lib" / "typescript.js"
    proj_ts.parent.mkdir(parents=True)
    proj_ts.write_text("// proj compiler code\n" + "y" * 1200, encoding="utf-8")

    with patch("pathlib.Path.is_file", autospec=True) as mock_is_file:
        def custom_is_file(self):
            if "vendor" in str(self):
                return False
            return os.path.isfile(str(self))
        mock_is_file.side_effect = custom_is_file

        # When vendor is missing, ensure_ts_tooling returns None and never loads from project
        res = toolchain.ensure_ts_tooling(project_root=proj)
        assert res is None
        assert not (tooling_dir / "typescript.js").is_file() or (tooling_dir / "typescript.js").read_text() != proj_ts.read_text()


def test_riley_weak_tests_deterministic_sorting():
    """Test riley review_plan and review_results sort weak_tests deterministically and handle uncovered claims."""
    plan = TestPlan(
        scenarios=[
            Scenario(id="C1", title="claim 1", steps=["step"], acceptance=["acc"], covered_by=["tests/test_b.py::test_b"]),
            Scenario(id="C2", title="claim 2", steps=["step"], acceptance=["acc"], covered_by=[]),
            Scenario(id="S3", title="negative", steps=["err"], acceptance=["fail"], negative=True),
        ]
    )
    weak_tests = {
        "tests/test_b.py::test_b": WeakTestHit(reason="vacuous"),
        "tests/test_z.py::test_z": WeakTestHit(reason="no assertions", priority="P1"),
        "tests/test_a.py::test_a": WeakTestHit(reason="vacuous"),
        "tests/test_m.py::test_m": WeakTestHit(reason="tautology"),
    }

    with patch("agents.riley.detect_weak_test_hits", return_value=weak_tests):
        # 1. Default config: veto_on_uncovered_claims is False -> uncovered claims not in gaps
        review = riley.review_plan(plan, config={"llm": {"provider": "none"}}, project_root=Path("."))
        gap_weak_entries = [g for g in review.gaps if g.startswith("weak test:")]
        assert gap_weak_entries == [
            "weak test: tests/test_a.py::test_a (vacuous)",
            "weak test: tests/test_b.py::test_b (vacuous)",
            "weak test: tests/test_m.py::test_m (tautology)",
            "weak test: tests/test_z.py::test_z ([P1] no assertions)",
        ]
        assert not any("uncovered claims" in g for g in review.gaps)
        assert any("claim C1 is covered only by weak test" in g for g in review.gaps)

        # Non-claim scenarios (S3) must NEVER be included in uncovered_claims
        strict_plan_cfg = {"llm": {"provider": "none"}, "riley": {"veto_on_uncovered_claims": True}}
        plan_strict = riley.review_plan(plan, config=strict_plan_cfg, project_root=Path("."))
        uncovered_gap = next(g for g in plan_strict.gaps if "uncovered claims:" in g)
        assert "S3" not in uncovered_gap
        assert "C2" in uncovered_gap

        # Fresh plan for review_results with weak test and strong test with '::'
        fresh_plan = TestPlan(
            scenarios=[
                Scenario(id="C1", title="claim 1", steps=["step"], acceptance=["acc"], covered_by=["tests/test_b.py::test_b"]),
                Scenario(id="C2", title="claim 2", steps=["step"], acceptance=["acc"], covered_by=["tests/test_strong.py::test_strong"]),
            ]
        )
        exec_res = ExecutionResult(collected=2, passed=2, failed=0, exit_code=0)
        res_review = riley.review_results(fresh_plan, asdict(exec_res), config={"llm": {"provider": "none"}}, project_root=Path("."))
        assert any("claim C1 is covered only by weak test" in g for g in res_review.gaps)
        # Strong test must NOT be flagged as weak
        assert not any("claim C2 is covered only by weak test" in g for g in res_review.gaps)

    with patch("agents.riley.detect_weak_test_hits", return_value={}):
        strict_cfg = {"llm": {"provider": "none"}, "riley": {"veto_on_uncovered_claims": True}}
        clean_plan = TestPlan(
            scenarios=[
                Scenario(id="C2", title="claim 2", steps=["step"], acceptance=["acc"], covered_by=[]),
                Scenario(id="S3", title="negative", steps=["err"], acceptance=["fail"], negative=True),
            ]
        )
        strict_res_review = riley.review_results(clean_plan, asdict(exec_res), config=strict_cfg, project_root=Path("."))
        # Uncovered claims stay P2 WARN and must not VETO on their own.
        assert strict_res_review.verdict != Verdict.VETO.value
        assert any("claim not covered by tests: C2" in g for g in strict_res_review.gaps)

        lax_res_review = riley.review_results(clean_plan, asdict(exec_res), config={"llm": {"provider": "none"}}, project_root=Path("."))
        assert lax_res_review.verdict != Verdict.VETO.value


def test_riley_and_sable_ts_tooling_and_subprocess(tmp_path: Path):
    """Test riley and sable invoke ts_analyzer with correct flags, env, and parse findings."""
    with patch("shutil.which", return_value="node"), \
         patch("pathlib.Path.is_file", autospec=True) as mock_is_file, \
         patch("subprocess.run") as mock_subproc:

        def custom_is_file(self):
            if str(self) == "node":
                return False  # bare command not file in cwd
            return True
        mock_is_file.side_effect = custom_is_file

        mock_subproc.return_value = MagicMock(
            returncode=0,
            stdout='{"weak_tests": [{"test_id": "t1", "name": "t1_name", "reason": "empty", "priority": "P2"}], "sable_findings": [{"rule": "network", "file": "f.ts", "line": 5}]}'
        )
        riley_res = riley._detect_ts_weak_tests(tmp_path)
        assert mock_subproc.called
        kwargs = mock_subproc.call_args[1]
        assert kwargs.get("capture_output") is True
        assert kwargs.get("text") is True
        assert kwargs.get("check") is False
        assert "QA_TOOLING_DIR" in kwargs.get("env", {})
        assert riley_res["t1"].reason == "empty"
        assert riley_res["t1"].priority == "P2"
        assert riley_res["t1_name"].reason == "empty"
        assert riley_res["t1_name"].priority == "P2"

        mock_subproc2 = MagicMock(
            returncode=0,
            stdout='{"sable_findings": [{"rule": "network", "file": "f.ts", "line": 5}]}'
        )
        mock_subproc.reset_mock()
        mock_subproc.return_value = mock_subproc2
        findings = sable._scan_ts_for_risks(tmp_path)
        assert mock_subproc.called
        kwargs2 = mock_subproc.call_args[1]
        assert kwargs2.get("capture_output") is True
        assert kwargs2.get("text") is True
        assert kwargs2.get("check") is False
        assert "QA_TOOLING_DIR" in kwargs2.get("env", {})
        assert len(findings) == 1
        assert findings[0]["rule"] == "network"

        # Non-zero returncode with non-empty sable findings returns empty list (must not parse findings on failure)
        mock_subproc.reset_mock()
        mock_subproc.return_value = MagicMock(returncode=1, stdout='{"sable_findings": [{"rule": "bad"}], "weak_tests": [{"test_id": "bad"}]}')
        assert sable._scan_ts_for_risks(tmp_path) == []
        assert riley._detect_ts_weak_tests(tmp_path) == {}

        # Zero returncode with empty stdout returns empty list / dict
        mock_subproc.reset_mock()
        mock_subproc.return_value = MagicMock(returncode=0, stdout="")
        assert sable._scan_ts_for_risks(tmp_path) == []
        assert riley._detect_ts_weak_tests(tmp_path) == {}

    # Node missing completely returns empty dict / list, not None
    with patch("shutil.which", return_value=None), \
         patch("pathlib.Path.is_file", autospec=True) as mock_file_missing:
        def custom_missing_file(self):
            if "ts_analyzer.js" in str(self):
                return True
            return False
        mock_file_missing.side_effect = custom_missing_file
        res_r = riley._detect_ts_weak_tests(tmp_path)
        assert res_r == {}
        assert res_r is not None
        res_s = sable._scan_ts_for_risks(tmp_path)
        assert res_s == []
        assert res_s is not None

    # Node on /tmp/node/node fallback invokes subprocess with /tmp/node/node
    with patch("shutil.which", return_value=None), \
         patch("pathlib.Path.is_file", autospec=True) as mock_file_fallback, \
         patch("subprocess.run") as mock_subproc_fallback:
        def custom_fallback_file(self):
            return str(self) == "/tmp/node/node" or "ts_analyzer.js" in str(self) or "typescript" in str(self)
        mock_file_fallback.side_effect = custom_fallback_file
        mock_subproc_fallback.return_value = MagicMock(returncode=0, stdout='{"sable_findings": [], "weak_tests": []}')

        riley._detect_ts_weak_tests(tmp_path)
        assert mock_subproc_fallback.called
        assert mock_subproc_fallback.call_args[0][0][0] == "/tmp/node/node"

        mock_subproc_fallback.reset_mock()
        sable._scan_ts_for_risks(tmp_path)
        assert mock_subproc_fallback.called
        assert mock_subproc_fallback.call_args[0][0][0] == "/tmp/node/node"





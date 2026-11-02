from __future__ import annotations

from agents.riley import review_plan, review_results
from core.packets import Scenario, TestPlan, Verdict


def test_riley_vetoes_plan_without_negative_scenario():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=["collected > 0"],
                negative=False,
            )
        ]
    )
    review = review_plan(plan, config={"llm": {"provider": "none"}, "dry_run": True})
    assert review.verdict == Verdict.VETO.value
    assert review.root_cause


def test_riley_vetoes_plan_without_acceptance_criteria():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=[],
                negative=False,
            ),
            Scenario(
                id="S2",
                title="invalid input",
                steps=["send bad data"],
                acceptance=[],
                negative=True,
            ),
        ]
    )
    review = review_plan(plan, config={"llm": {"provider": "none"}, "dry_run": True})
    assert review.verdict == Verdict.VETO.value
    assert review.root_cause


def test_riley_collection_error_missing_dependency():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=["collected > 0"],
                negative=False,
            ),
            Scenario(
                id="S2",
                title="negative path",
                steps=["run tests"],
                acceptance=["fail on error"],
                negative=True,
            ),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 0,
        "failed": 0,
        "errors": 1,
        "exit_code": 2,
        "stdout": (
            "ERROR collecting tests/test_calculator.py\n"
            "ModuleNotFoundError: No module named 'qa_nonexistent_pkg_xyz'"
        ),
        "stderr": "",
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}
    )
    assert review.verdict == Verdict.VETO.value
    assert "missing dependency: qa_nonexistent_pkg_xyz" in review.root_cause


def test_riley_parses_missing_dependency_from_stderr_only():
    """riley.py:565 - pytest reports import-time collection errors on stderr. The
    stderr half of the root-cause parse must not be dropped, or the veto degrades
    to the generic 'pytest reported collection or runtime errors' message."""
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=["collected > 0"],
                negative=False,
            ),
            Scenario(
                id="S2",
                title="negative path",
                steps=["run tests"],
                acceptance=["fail on error"],
                negative=True,
            ),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 0,
        "failed": 0,
        "errors": 1,
        "exit_code": 2,
        "stdout": "",
        "stderr": (
            "ERROR collecting tests/test_calculator.py\n"
            "ModuleNotFoundError: No module named 'qa_stderr_only_pkg_xyz'"
        ),
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}
    )
    assert review.verdict == Verdict.VETO.value
    assert "missing dependency: qa_stderr_only_pkg_xyz" in review.root_cause


def test_riley_parses_failed_node_from_stderr_only():
    """riley.py:563-565 - the failing-node summary is read from the combined
    stdout+stderr text, so a run that only prints FAILED lines on stderr must
    still name the test and its assertion."""
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="S1",
                title="happy path",
                steps=["run tests"],
                acceptance=["collected > 0"],
                negative=False,
            ),
            Scenario(
                id="S2",
                title="negative path",
                steps=["run tests"],
                acceptance=["fail on error"],
                negative=True,
            ),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 0,
        "failed": 1,
        "errors": 0,
        "exit_code": 1,
        "stdout": "",
        "stderr": "FAILED tests/test_calculator.py::test_add - assert 5 == 0\n",
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}
    )
    assert review.verdict == Verdict.VETO.value
    assert "pytest failures: test_add" in review.root_cause
    assert "assert 5 == 0" in review.root_cause


def test_riley_records_gap_for_uncovered_claim():
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="C1",
                title="Never burns tokens",
                steps=["verify"],
                acceptance=["pass"],
                negative=False,
                covered_by=[],
            ),
            Scenario(
                id="S1",
                title="negative path",
                steps=["run tests"],
                acceptance=["fail on error"],
                negative=True,
            ),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "stdout": "1 passed",
        "stderr": "",
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}
    )
    assert review.verdict == Verdict.CLEAR.value
    assert "claim not covered by tests: C1" in review.gaps


def test_riley_monotonic_veto_llm_cannot_override_to_clear(monkeypatch):
    import agents.riley
    monkeypatch.setattr(
        agents.riley,
        "complete_with_status",
        lambda **kwargs: ("verdict: CLEAR\nroot_cause: ''\nnotes: looks fine to me", "ok"),
    )
    plan = TestPlan(
        scenarios=[
            Scenario(id="S1", title="happy", steps=["run"], acceptance=["ok"], negative=False),
            Scenario(id="S2", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 0,
        "failed": 1,
        "errors": 0,
        "exit_code": 1,
        "stdout": "FAILED test_x.py::test_fail",
        "stderr": "",
    }
    review = review_results(
        plan, execution, config={"llm": {"provider": "mock"}, "dry_run": False}
    )
    assert review.verdict == Verdict.VETO.value
    assert review.root_cause


def test_riley_vetoes_on_uncovered_claims_when_configured():
    plan = TestPlan(
        scenarios=[
            Scenario(id="C1", title="claim without test", steps=["verify"], acceptance=["pass"], negative=False, covered_by=[]),
            Scenario(id="S1", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "stdout": "1 passed",
        "stderr": "",
    }
    # When false (default): CLEAR
    review_clear = review_results(
        plan, execution, config={"riley": {"veto_on_uncovered_claims": False}, "llm": {"provider": "none"}, "dry_run": True}
    )
    assert review_clear.verdict == Verdict.CLEAR.value

    # When true: VETO
    review_veto = review_results(
        plan, execution, config={"riley": {"veto_on_uncovered_claims": True}, "llm": {"provider": "none"}, "dry_run": True}
    )
    assert review_veto.verdict == Verdict.VETO.value
    assert "uncovered claims" in review_veto.root_cause


def test_riley_review_plan_does_not_flag_uncovered_claims_by_default():
    """riley.py:197 - with veto_on_uncovered_claims absent (default False), an
    otherwise-complete plan must not accumulate an 'uncovered claims' gap."""
    plan = TestPlan(
        scenarios=[
            Scenario(id="C1", title="claim without test", steps=["verify"], acceptance=["pass"], negative=False, covered_by=[]),
            Scenario(id="S1", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    review = review_plan(
        plan,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert review.verdict == Verdict.CLEAR.value
    assert review.gaps == []
    assert not any("uncovered claims" in g for g in review.gaps)


def test_riley_ast_detects_weak_tests_and_vetoes_uncovered_claim(tmp_path):
    from agents.riley import detect_weak_tests
    from pathlib import Path

    proj = tmp_path / "proj"
    proj.mkdir()
    test_file = proj / "test_weak.py"
    test_file.write_text(
        "def test_empty():\n"
        "    x = 1\n"
        "def test_tautology_none():\n"
        "    assert x is not None\n"
        "def test_tautology_true():\n"
        "    assert True\n"
        "def test_tautology_callable():\n"
        "    assert callable(x)\n"
        "def test_strong_assertion():\n"
        "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    weak = detect_weak_tests(proj)
    assert "test_empty" in weak and weak["test_empty"] == "no assertions"
    assert "test_tautology_none" in weak and weak["test_tautology_none"] == "tautology assertion"
    assert "test_tautology_true" in weak and weak["test_tautology_true"] == "tautology assertion"
    assert "test_tautology_callable" in weak and weak["test_tautology_callable"] == "tautology assertion"
    assert "test_strong_assertion" not in weak

    plan = TestPlan(
        scenarios=[
            Scenario(id="C1", title="claim", steps=["verify"], acceptance=["pass"], negative=False, covered_by=["test_weak.py::test_tautology_none"]),
            Scenario(id="S1", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    plan_rev = review_plan(plan, config={"llm": {"provider": "none"}, "dry_run": True}, project_root=proj)
    # Claim coverage is discounted: covered_by cleared to []
    assert plan.scenarios[0].covered_by == []
    assert any("claim C1 is covered only by weak test" in g for g in plan_rev.gaps)

    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "stdout": "1 passed",
        "stderr": "",
    }
    res_rev = review_results(plan, execution, config={"llm": {"provider": "none"}, "dry_run": True}, project_root=proj)
    assert res_rev.verdict == Verdict.VETO.value
    assert "weak test detected" in res_rev.root_cause


def test_riley_vetoes_when_pip_install_failed():
    plan = TestPlan(
        scenarios=[
            Scenario(id="S1", title="happy", steps=["run"], acceptance=["ok"], negative=False),
            Scenario(id="S2", title="negative", steps=["run"], acceptance=["fail"], negative=True),
        ]
    )
    execution = {
        "collected": 1,
        "passed": 1,
        "failed": 0,
        "errors": 0,
        "exit_code": 0,
        "install_status": "pip install failed: unsafe requirement: git+https://example.com/repo.git",
        "stdout": "1 passed",
        "stderr": "",
    }
    rev = review_results(plan, execution, config={"llm": {"provider": "none"}, "dry_run": True})
    assert rev is not None
    assert rev.verdict == Verdict.VETO.value
    assert "dependency installation failed" in rev.root_cause





def test_detects_cheating_patterns(tmp_path):
    from agents.riley import detect_weak_tests

    (tmp_path / "test_cheat.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='flaky')\n"
        "def test_skipped():\n"
        "    assert 1 + 1 == 3\n\n"
        "def test_skip_call():\n"
        "    pytest.skip('later')\n"
        "    assert 1 + 1 == 3\n\n"
        "def test_swallowed():\n"
        "    try:\n"
        "        assert 1 + 1 == 3\n"
        "    except Exception:\n"
        "        pass\n\n"
        "def test_trivial():\n"
        "    x = len([1])\n"
        "    assert x is not None\n"
        "    assert isinstance(x, int)\n"
        "    assert len([x]) >= 0\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert weak["test_skipped"] == "skipped test"
    assert weak["test_skip_call"] == "skipped test"
    assert weak["test_swallowed"] == "assertion failures swallowed by try/except"
    assert weak["test_trivial"] == "only trivial assertions"


def test_honest_patterns_are_not_weak(tmp_path):
    from agents.riley import detect_weak_tests

    (tmp_path / "test_ok.py").write_text(
        "import pytest\n\n"
        "def test_cleanup_with_try_pass():\n"
        "    assert 2 + 2 == 4\n"
        "    try:\n"
        "        open('/nonexistent')\n"
        "    except OSError:\n"
        "        pass\n\n"
        "def test_mixed_strong_and_trivial():\n"
        "    x = 4\n"
        "    assert x is not None\n"
        "    assert x == 4\n\n"
        "@pytest.mark.skipif(False, reason='never')\n"
        "def test_conditional_skip_is_flagged_only_by_decorator():\n"
        "    assert 1 == 1 + 0\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert "test_cleanup_with_try_pass" not in weak
    assert "test_mixed_strong_and_trivial" not in weak


def test_weak_scan_limited_to_pytest_collected_files_and_functions(tmp_path):
    """Cause 5: only pytest-collected files (test_*.py / *_test.py) and functions
    whose name starts with 'test' are scanned, so pluggy-style false positives
    (benchmark.py, run_downstream.py, pytest_timeout_*) are no longer reported."""
    from agents.riley import detect_weak_tests

    proj = tmp_path / "proj"
    (proj / "testing").mkdir(parents=True)
    (proj / "downstream").mkdir(parents=True)

    # benchmark.py is not a collected file, even though its functions look like tests
    (proj / "testing" / "benchmark.py").write_text(
        "def test_speed():\n    x = 1\n", encoding="utf-8"
    )
    # run_downstream.py is not collected; the name merely contains 'test'
    (proj / "downstream" / "run_downstream.py").write_text(
        "def script_has_no_test_steps():\n    x = 1\n", encoding="utf-8"
    )
    # a collected file, but pytest_timeout_foo is not a collected function name
    (proj / "testing" / "test_helpers.py").write_text(
        "def pytest_timeout_foo():\n    x = 1\n", encoding="utf-8"
    )

    assert detect_weak_tests(proj) == {}

    # a genuinely collected weak test is still reported
    (proj / "testing" / "test_real.py").write_text(
        "def test_no_assert():\n    x = 1\n", encoding="utf-8"
    )
    weak = detect_weak_tests(proj)
    assert "test_no_assert" in weak
    assert weak["test_no_assert"] == "no assertions"


def test_unittest_style_assertions_are_not_weak():
    import ast
    from agents.riley import is_weak_test_node as is_weak_test

    src = (
        "class T:\n"
        "    def test_x(self):\n"
        "        self.assertEqual(f(), 1)\n"
    )
    fn = ast.parse(src).body[0].body[0]
    weak, _ = is_weak_test(fn)
    assert weak is False


def test_sable_allows_test_cleanup_only_when_sandboxed(tmp_path):
    from policies.sable import scan_tests_for_risks

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_db.py").write_text("import os\n\ndef test_x():\n    os.remove('x.db')\n")
    hits, allowed, _ = scan_tests_for_risks(tmp_path)
    assert hits
    hits, allowed, _ = scan_tests_for_risks(tmp_path, sandboxed=True)
    assert not hits
    assert allowed and allowed[0]["call"].endswith("remove")


def test_sable_parses_files_with_bom(tmp_path):
    from policies.sable import scan_tests_for_risks

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_ok.py").write_bytes(b"\xef\xbb\xbfdef test_x():\n    assert 1 + 1 == 2\n")
    hits, _, _ = scan_tests_for_risks(tmp_path)
    assert not hits


def test_is_trivial_assert_requires_single_operator_comparison(tmp_path):
    """Only a single-operator self-comparison is trivial; a chained comparison
    is not, and a non-comparison test node must not break the check."""
    import ast
    from agents.riley import is_trivial_assert

    def assert_node(src: str):
        return ast.parse("def t():\n    " + src).body[0].body[0]

    assert is_trivial_assert(assert_node("assert x <= x")) is True
    assert is_trivial_assert(assert_node("assert x <= x <= 5")) is False
    assert is_trivial_assert(assert_node("assert flag")) is False


def test_review_plan_treats_known_weak_function_name_as_weak_coverage(tmp_path):
    """A claim covered only by a weak test function is not strong coverage even
    when the reported file path is not the one that was scanned."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "test_weak.py").write_text(
        "def test_empty():\n    x = 1\n", encoding="utf-8"
    )
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="C1",
                title="claim",
                steps=["run"],
                acceptance=["pass"],
                negative=False,
                covered_by=["tests/unit/test_other.py::test_empty"],
            ),
            Scenario(
                id="S1",
                title="negative",
                steps=["run"],
                acceptance=["fail"],
                negative=True,
            ),
        ]
    )
    review = review_plan(
        plan,
        config={"llm": {"provider": "none"}, "dry_run": True},
        project_root=proj,
    )
    assert any("claim C1 is covered only by weak test" in g for g in review.gaps)


def test_helper_assertion_in_sibling_module_is_not_weak(tmp_path):
    """python-tabulate shape: the test asserts through assert_equal imported from
    a module in the same test package, so it is not an assertion-free test."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "common.py").write_text(
        "def assert_equal(expected, result):\n"
        "    assert expected == result\n\n"
        "def plain_helper(value):\n"
        "    return value\n\n"
        "class Guard:\n"
        "    def method_check(self, value):\n"
        "        assert value\n",
        encoding="utf-8",
    )
    (tests / "test_regression.py").write_text(
        "from common import assert_equal, plain_helper\n\n"
        "def test_long_integers():\n"
        "    plain_helper(1)\n"
        "    assert_equal(1, 1)\n\n"
        "def test_truly_empty():\n"
        "    plain_helper(1)\n\n"
        "def test_dotted_helper_call():\n"
        "    import common\n"
        "    common.assert_equal(1, 1)\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert "test_long_integers" not in weak
    assert "test_dotted_helper_call" not in weak
    assert weak["test_truly_empty"] == "no assertions"


def test_conftest_assertion_helper_is_not_weak(tmp_path):
    """Helpers from conftest.py in an ancestor directory count too."""
    from agents.riley import detect_weak_tests

    (tmp_path / "conftest.py").write_text(
        "def expect_ok(value):\n    assert value == 'ok'\n", encoding="utf-8"
    )
    pkg = tmp_path / "tests" / "sub"
    pkg.mkdir(parents=True)
    (pkg / "test_deep.py").write_text(
        "from conftest import expect_ok\n\n"
        "def test_uses_conftest_helper():\n    expect_ok('ok')\n",
        encoding="utf-8",
    )
    assert "test_uses_conftest_helper" not in detect_weak_tests(tmp_path)


def test_helper_indirection_is_bounded_to_one_level(tmp_path):
    """A helper that merely calls a further asserting helper is not followed; the
    test that only reaches an assertion two levels down stays weak."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "helpers.py").write_text(
        "from deep import real_assert\n\n"
        "def middle(value):\n    real_assert(value)\n",
        encoding="utf-8",
    )
    (tests / "deep.py").write_text(
        "def real_assert(value):\n    assert value\n", encoding="utf-8"
    )
    (tests / "test_two.py").write_text(
        "from helpers import middle\n\n"
        "def test_two_level():\n    middle(1)\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert weak["test_two_level"] == "no assertions"


def test_call_to_unknown_function_is_not_a_helper(tmp_path):
    """Only helpers defined inside the test tree count; a third-party call with a
    plausible name does not rescue an assertion-free test."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_ext.py").write_text(
        "import random\n\n"
        "def test_shuffles_only():\n    random.shuffle([1, 2])\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert weak["test_shuffles_only"] == "no assertions"


def test_has_meaningful_assertion_filters_trivial_and_swallowed_bodies():
    import ast
    from agents.riley import has_meaningful_assertion

    real = ast.parse("def t():\n    assert x == 1\n").body[0]
    unittest_style = ast.parse(
        "class T:\n    def t(self):\n        self.assertEqual(x, 1)\n"
    ).body[0].body[0]
    raises = ast.parse(
        "def t():\n    with pytest.raises(ValueError):\n        f()\n"
    ).body[0]
    tautology = ast.parse("def t():\n    assert True\n").body[0]
    self_compare = ast.parse("def t(v):\n    assert v == v\n").body[0]
    isinstance_only = ast.parse("def t(v):\n    assert isinstance(v, int)\n").body[0]
    swallowed = ast.parse(
        "def t(v):\n    try:\n        assert v == 1\n"
        "    except AssertionError:\n        pass\n"
    ).body[0]
    silent = ast.parse("def t():\n    f()\n").body[0]
    assert has_meaningful_assertion(real) is True
    assert has_meaningful_assertion(unittest_style) is True
    assert has_meaningful_assertion(raises) is True
    assert has_meaningful_assertion(tautology) is False
    assert has_meaningful_assertion(self_compare) is False
    assert has_meaningful_assertion(isinstance_only) is False
    assert has_meaningful_assertion(swallowed) is False
    assert has_meaningful_assertion(silent) is False


def test_is_weak_test_node_uses_supplied_helper_names():
    import ast
    from agents.riley import is_weak_test_node

    fn = ast.parse("def test_a():\n    check(1)\n").body[0]
    assert is_weak_test_node(fn)[0] is True
    assert is_weak_test_node(fn, frozenset({"check"}))[0] is False
    # calling itself is not an assertion helper
    assert is_weak_test_node(fn, frozenset({"test_a"}))[0] is True


def test_helper_sources_are_bounded_to_the_test_tree(tmp_path):
    from agents.riley import _helper_source_files

    root = tmp_path / "proj"
    pkg = root / "tests"
    pkg.mkdir(parents=True)
    test_file = pkg / "test_a.py"
    test_file.write_text("", encoding="utf-8")
    (pkg / "common.py").write_text("", encoding="utf-8")
    (pkg / "conftest.py").write_text("", encoding="utf-8")
    (root / "conftest.py").write_text("", encoding="utf-8")
    (root / "outside.py").write_text("", encoding="utf-8")

    found = _helper_source_files(test_file, root)
    assert test_file not in found
    assert (pkg / "common.py") in found
    assert (pkg / "conftest.py") in found
    assert (root / "conftest.py") in found
    assert (root / "outside.py") not in found


# --- Task 7: helper qualification and call resolution ----------------------


_REAL_HELPERS = (
    "def assert_equal(expected, result):\n    assert expected == result\n\n"
    "def always_ok(value):\n    assert True\n\n"
    "def self_same(value):\n    assert value == value\n\n"
    "def kind_check(value):\n    assert isinstance(value, int)\n"
)


def test_trivial_only_helper_leaves_the_test_weak(tmp_path):
    """A helper whose body is only `assert True` / `assert v == v` /
    `assert isinstance(...)` proves nothing, so it must not rescue an
    assertion-free test - a real assert in the same module still does."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "common.py").write_text(_REAL_HELPERS, encoding="utf-8")
    (tests / "test_trivial.py").write_text(
        "from common import assert_equal, always_ok, self_same, kind_check\n\n"
        "def test_real_helper():\n    assert_equal(1, 2)\n\n"
        "def test_tautology_helper():\n    always_ok(1)\n\n"
        "def test_self_compare_helper():\n    self_same(1)\n\n"
        "def test_isinstance_helper():\n    kind_check(1)\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert "test_real_helper" not in weak
    assert weak["test_tautology_helper"] == "no assertions"
    assert weak["test_self_compare_helper"] == "no assertions"
    assert weak["test_isinstance_helper"] == "no assertions"


def test_swallowed_assertion_helper_leaves_the_test_weak(tmp_path):
    """An assert the helper catches and discards cannot fail anything, so the
    helper does not qualify."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "common.py").write_text(
        "def quiet_check(value):\n"
        "    try:\n        assert value == 1\n"
        "    except AssertionError:\n        pass\n",
        encoding="utf-8",
    )
    (tests / "test_swallow.py").write_text(
        "from common import quiet_check\n\n"
        "def test_uses_quiet():\n    quiet_check(2)\n",
        encoding="utf-8",
    )
    assert detect_weak_tests(tmp_path)["test_uses_quiet"] == "no assertions"


def test_attribute_call_on_non_module_object_is_weak(tmp_path):
    """Only a name imported from the test tree may qualify an attribute call:
    a fixture argument or an attribute chain sharing the helper's name stays
    weak even though common.assert_equal exists next to it."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "common.py").write_text(
        "def assert_equal(expected, result):\n    assert expected == result\n",
        encoding="utf-8",
    )
    (tests / "test_objects.py").write_text(
        "def test_through_fixture(session):\n    session.assert_equal(1, 1)\n\n"
        "def test_through_chain(a, b):\n    a.b.assert_equal(1, 1)\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert weak["test_through_fixture"] == "no assertions"
    assert weak["test_through_chain"] == "no assertions"


def test_aliased_and_relative_module_imports_resolve_helpers(tmp_path):
    """`import common as tools` and `from . import common` both bind the test-tree
    module, so calls through either name count as the assertion they really are."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "common.py").write_text(
        "def assert_equal(expected, result):\n    assert expected == result\n",
        encoding="utf-8",
    )
    (tests / "test_bindings.py").write_text(
        "import common as tools\n"
        "from . import common\n\n"
        "def test_aliased_module():\n    tools.assert_equal(1, 1)\n\n"
        "def test_relative_module():\n    common.assert_equal(1, 1)\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert "test_aliased_module" not in weak
    assert "test_relative_module" not in weak


def test_package_module_and_star_import_resolution(tmp_path):
    """A package's ``__init__.py`` binds for `import kit`; a star import binds no
    module name, and a call through an unbound name stays weak."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "test"
    (tests / "kit").mkdir(parents=True)
    (tests / "kit" / "__init__.py").write_text(
        "def assert_ok(value):\n    assert value == 1\n", encoding="utf-8"
    )
    (tests / "test_pkg.py").write_text(
        "import kit\nfrom kit import *\n\n"
        "def test_via_package():\n    kit.assert_ok(1)\n\n"
        "def test_via_unbound_name():\n    other.assert_ok(1)\n",
        encoding="utf-8",
    )
    weak = detect_weak_tests(tmp_path)
    assert "test_via_package" not in weak
    assert weak["test_via_unbound_name"] == "no assertions"


def test_module_call_named_after_the_test_itself_is_not_an_assertion(tmp_path):
    """A test that only reaches for its own name through a module is recursion, not
    an assertion made by a helper."""
    from agents.riley import detect_weak_tests

    tests = tmp_path / "test"
    tests.mkdir()
    (tests / "common.py").write_text(
        "def test_shared_name(value):\n    assert value == 1\n", encoding="utf-8"
    )
    (tests / "test_selfcall.py").write_text(
        "import common\n\n"
        "def test_shared_name(value):\n    common.test_shared_name(value)\n",
        encoding="utf-8",
    )
    assert detect_weak_tests(tmp_path)["test_shared_name"] == "no assertions"


def test_is_weak_test_node_accepts_module_helper_bindings():
    import ast
    from agents.riley import is_weak_test_node

    fn = ast.parse("def test_a():\n    tools.check(1)\n").body[0]
    assert is_weak_test_node(fn)[0] is True
    assert is_weak_test_node(fn, frozenset(), {"tools": frozenset({"check"})})[0] is False
    # a binding that exports no assertion helper is not an assertion
    assert is_weak_test_node(fn, frozenset(), {"tools": frozenset()})[0] is True
    # the helper name alone must not rescue an attribute call on a foreign object
    assert is_weak_test_node(fn, frozenset({"check"}))[0] is True


def test_module_file_in_tree_resolution_forms(tmp_path):
    from agents.riley import _module_file_in_tree

    root = tmp_path / "proj"
    (root / "kit").mkdir(parents=True)
    (root / "kit" / "__init__.py").write_text("", encoding="utf-8")
    (root / "solo.py").write_text("", encoding="utf-8")
    assert _module_file_in_tree(root, ("solo",)) == root / "solo.py"
    assert _module_file_in_tree(root, ("kit",)) == root / "kit" / "__init__.py"
    assert _module_file_in_tree(root, ("missing",)) is None
    assert _module_file_in_tree(root, ("kit", "missing")) is None
    assert _module_file_in_tree(root, ()) is None


def test_imported_module_files_binds_only_test_tree_modules(tmp_path):
    import ast
    from agents.riley import _ancestor_dirs, _imported_module_files

    root = tmp_path / "proj"
    pkg = root / "tests"
    (pkg / "kit").mkdir(parents=True)
    (pkg / "kit" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "kit" / "sub.py").write_text("", encoding="utf-8")
    (pkg / "common.py").write_text("", encoding="utf-8")
    (tmp_path / "outer.py").write_text("", encoding="utf-8")  # above the project root
    test_file = pkg / "test_imports.py"
    source = (
        "import common\n"
        "import os\n"
        "import outer\n"
        "import kit.sub as kit_sub\n"
        "from kit import sub as inner\n"
        "from . import common as relative\n"
        "from common import *\n"
    )
    test_file.write_text(source, encoding="utf-8")

    assert _ancestor_dirs(test_file, root) == [pkg, root]
    bound = _imported_module_files(ast.parse(source), test_file, root)
    assert bound == {
        "common": pkg / "common.py",
        "kit_sub": pkg / "kit" / "sub.py",
        "inner": pkg / "kit" / "sub.py",
        "relative": pkg / "common.py",
    }


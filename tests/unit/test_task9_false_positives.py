"""Task 9 - one test per false-positive fix A-H found on a large real repo.

Everything here is synthetic (no Quillhog code). The tests are deterministic:
no network, no LLM, no bwrap required.
"""

from __future__ import annotations

import ast
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

from agents.adapters import llm_network_allowed, scrub_secrets
from agents.lexi import inspect as lexi_inspect
from agents.lexi import _DOTTED_NAME_RE, _qualified_in_docs
from agents.riley import (
    environment_failure_cause,
    failure_lines,
    has_meaningful_assertion,
    is_tautology_assert,
)
from core.environment import (
    annotate_failures,
    classify_execution,
    classify_failure,
    product_failure_count,
)
from core.junit import parse_failures
from core.orchestrator import Vesper
from core.packets import ExecutionResult, FinalReport, Verdict
from core.report import render_markdown
from core.sandbox import wrap_command
from tests.support import mini_project


def _fn(source: str) -> ast.FunctionDef:
    module = ast.parse(textwrap.dedent(source))
    fn = next(n for n in module.body if isinstance(n, ast.FunctionDef))
    return fn


def _first_assert(fn: ast.FunctionDef) -> ast.Assert:
    return next(n for n in ast.walk(fn) if isinstance(n, ast.Assert))


# --- A: prose never vetoes, in any language --------------------------------

def test_A_english_prose_word_is_never_a_veto(tmp_path: Path):
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs="# Marketing\n\nOur `seamless` engine is `revolutionary` and `gamified`.\n",
    )
    report = lexi_inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict != Verdict.VETO.value
    assert all(f.severity != "high" for f in report.findings)


def test_A_czech_and_german_prose_never_veto(tmp_path: Path):
    """Fix A is language-agnostic: accented prose words carry no code shape, so
    they may not escalate in Czech or German either."""
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs={
            "cs.md": "# Prehled\n\nNase `revolucionarni` a `plynulou` funkci zisk "
                     "je bez zakazu.\n",
            "de.md": "#_ueberblick\n\nUnsere `nahtlose` und `revolutionaere` "
                     "Funktion ist ohne Verbot.\n",
        },
    )
    report = lexi_inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict != Verdict.VETO.value
    assert all(f.severity != "high" for f in report.findings)


def test_A_forbidden_words_list_is_structural_not_a_veto(tmp_path: Path):
    """A backticked word that only ever appears as an item under a "Forbidden"
    heading is a vocabulary item, not a promised API - read from the structure,
    not from a per-language word list."""
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs=(
            "# Style\n\n"
            "## Forbidden words\n\n"
            "- `compliant`\n"
            "- `guaranteed`\n"
            "- `accurate`\n"
        ),
    )
    report = lexi_inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert report.verdict != Verdict.VETO.value
    banned = [f for f in report.findings if f.path in {"compliant", "guaranteed", "accurate"}]
    assert all(f.severity in {"low", "medium"} for f in banned)


def test_A_external_name_is_a_reference_not_missing_code(tmp_path: Path):
    """nginx / stdlib / foreign-runtime names are references, never missing
    project code. Each one is excused by a different documented mechanism: an
    external config context, a code reference, and a raised-class convention."""
    root = mini_project(
        tmp_path / "proj",
        code={
            "srv.py": "import concurrent.futures\n\n"
            "def go():\n"
            "    return concurrent.futures.ThreadPoolExecutor()\n",
        },
        docs=(
            "# Ops\n\n"
            "Set `error_log` in nginx.\n\n"
            "Worker pool is a `ThreadPoolExecutor` from stdlib.\n\n"
            "Solana may raise `ExtensionError`.\n"
        ),
    )
    # nginx (not a python file) carries error_log as a real directive.
    (root / "nginx.conf").write_text(
        "error_log /var/log/nginx/error.log warn;\n", encoding="utf-8"
    )
    report = lexi_inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    flagged = {f.path for f in report.findings if f.kind == "missing_code"}
    for external in ("error_log", "ExtensionError", "ThreadPoolExecutor"):
        assert external not in flagged, f"{external} should read as a reference"
    assert report.verdict != Verdict.VETO.value


def test_A_dotted_name_regex_is_linear_and_still_matches_paths():
    """The fix-A dotted-name scan must not backtrack catastrophically.

    ``(?:[A-Za-z_][A-Za-z0-9_]*)+`` (a nested quantifier) looked identical to a
    plain run of word characters, but when the scan found no dot it explored an
    exponential number of partitions - the self-test of a large repo stalled for
    minutes inside ``_qualified_in_docs``. A linear shape cannot blow up, yet it
    still finds the dotted path a real external reference carries.
    """
    # Correctness: a qualified dotted name is recognised, its leaf located.
    docs = "Configure `concurrent.futures.ThreadPoolExecutor` and `os.path.join`."
    assert _qualified_in_docs("ThreadPoolExecutor", docs) is True
    assert _qualified_in_docs("join", docs) is True
    assert _qualified_in_docs("notpresent", docs) is False
    # Backtracking guard: a long word-character run with no dot used to explode.
    # A pathological token plus trailing near-misses is scanned in microseconds
    # once the nested quantifier is gone; the loose ceiling only fails if the
    # exponential shape is reintroduced.
    pathological = ("a" * 60 + ".") * 4 + "b" * 60
    assert not _DOTTED_NAME_RE.search("no_dots_here_" + "x" * 50)
    started = time.monotonic()
    _qualified_in_docs("needle", pathological)
    assert time.monotonic() - started < 1.0, "dotted-name scan backtracked (regression)"


# --- B: identifiers inside string constants ---------------------------------

def test_B_identifiers_inside_sql_literal_are_not_missing(tmp_path: Path):
    """A table name that lives only inside a CREATE TABLE string is real code;
    Lexi must tokenise the literal, not compare it exactly."""
    root = mini_project(
        tmp_path / "proj",
        code={
            "db.py": 'SCHEMA = "CREATE TABLE till_consumed_signatures (id INTEGER)"\n',
        },
        docs="# Schema\n\nThe table `till_consumed_signatures` stores signatures.\n",
    )
    report = lexi_inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert not any(
        f.kind == "missing_code" and f.path == "till_consumed_signatures"
        for f in report.findings
    )


# --- C: string-literal check runs before comment-only check -----------------

def test_C_string_literal_is_not_reported_as_comment_only(tmp_path: Path):
    """REFUND_NEEDED lives as a string literal in code. The literal check must
    fire before the "only in comments" path so it is not (mis)reported."""
    root = mini_project(
        tmp_path / "proj",
        code={"state.py": 'STATE = "REFUND_NEEDED"\n\n# nothing here\n'},
        docs="# Refund\n\nThe marker `REFUND_NEEDED` is written to state.\n",
    )
    report = lexi_inspect(
        docs_root=root / "docs",
        project_root=root,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )
    assert not any(f.path == "REFUND_NEEDED" for f in report.findings)


# --- D: riley assertion recognition -----------------------------------------

def test_D_is_not_none_after_a_call_is_not_a_tautology():
    fn = _fn(
        """
        def test_queue():
            entry = build_entry()
            assert entry is not None
        """
    )
    stmt = _first_assert(fn)
    assert is_tautology_assert(stmt, fn_node=fn) is False
    assert has_meaningful_assertion(fn) is True


def test_D_is_not_none_with_no_producing_call_is_vacuous():
    fn = _fn(
        """
        def test_static():
            x = 1
            assert x is not None
        """
    )
    stmt = _first_assert(fn)
    assert is_tautology_assert(stmt, fn_node=fn) is True


def test_D_mock_assertions_count_as_meaningful():
    fn = _fn(
        """
        def test_dispatch():
            mock.notify.assert_called_once_with("x")
        """
    )
    assert has_meaningful_assertion(fn) is True


# --- E: failure details survive ---------------------------------------------

def test_E_junit_parse_keeps_every_failed_node(tmp_path: Path):
    xml = tmp_path / "report.xml"
    xml.write_text(
        textwrap.dedent(
            """
            <?xml version="1.0" ?>
            <testsuites><testsuite name="pytest" tests="3">
              <testcase classname="tests.test_a" name="test_one"/>
              <testcase file="tests/test_a.py" classname="tests.test_a" name="test_two">
                <failure message="AssertionError: expected 1 != 2">traceback body</failure>
              </testcase>
              <testcase file="tests/test_b.py" classname="tests.test_b" name="test_three">
                <error message="NameError: name 'oops' is not defined">boom</error>
              </testcase>
            </testsuite></testsuites>
            """
        ),
        encoding="utf-8",
    )
    failures = [f.to_dict() for f in parse_failures(xml)]
    nodes = {f["node"] for f in failures}
    assert nodes == {"tests/test_a.py::test_two", "tests/test_b.py::test_three"}
    kinds = {f["node"]: f["kind"] for f in failures}
    assert kinds["tests/test_a.py::test_two"] == "failed"
    assert kinds["tests/test_b.py::test_three"] == "error"


def test_E_report_names_every_failure_split_by_class():
    execution = {
        "failed": 2,
        "errors": 0,
        "product_failed": 1,
        "environment_failed": 1,
        "collected": 10,
        "passed": 8,
        "exit_code": 1,
        "stdout": "",
        "stderr": "",
        "failures": [
            {
                "node": "tests/test_a.py::test_two",
                "kind": "failed",
                "message": "AssertionError: expected 1 != 2",
                "classification": "product",
            },
            {
                "node": "tests/test_b.py::test_three",
                "kind": "failed",
                "message": "not a git repository",
                "classification": "environment",
                "environment_cause": "repository metadata absent",
            },
        ],
    }
    md = render_markdown(
        FinalReport(run_id="r-e", verdict=Verdict.VETO.value, execution=execution)
    )
    assert "## Test failures" in md
    assert "tests/test_a.py::test_two" in md
    assert "tests/test_b.py::test_three" in md
    assert "expected 1 != 2" in md
    assert "### Product failures" in md
    assert "### Environment failures" in md


# --- F: environment vs product failures inside a run ------------------------

def test_F_git_absent_failure_is_environment():
    failure = {
        "node": "tests/test_meta.py::test_history",
        "message": "fatal: not a git repository (or any of the parent directories): .git",
        "traceback": "",
    }
    cause = classify_failure(failure)
    assert cause is not None and cause.kind == "missing_vcs_metadata"


def test_F_missing_tool_failure_is_environment():
    failure = {
        "node": "tests/test_build.py::test_node",
        "message": "",
        "traceback": "FileNotFoundError: [Errno 2] No such file or directory: 'node'",
    }
    cause = classify_failure(failure)
    assert cause is not None and cause.kind == "missing_tool"


def test_F_assertion_failure_is_product():
    failure = {"node": "tests/test_a.py::test_two", "message": "assert 1 == 2", "traceback": ""}
    assert classify_failure(failure) is None


def test_F_annotate_counts_and_classify_execution_respects_product():
    failures = [
        {"node": "a", "message": "not a git repository", "traceback": ""},
        {"node": "b", "message": "assert 1 == 2", "traceback": ""},
    ]
    product, environment = annotate_failures(failures)
    assert (product, environment) == (1, 1)
    # A run with even one product failure can never be an environment stop.
    execution = {"collected": 2, "passed": 0, "failed": 2, "errors": 0, "exit_code": 1,
                 "product_failed": 1, "environment_failed": 1, "failures": failures,
                 "stdout": "", "stderr": ""}
    assert product_failure_count(execution) == 1
    assert classify_execution(execution) is None


def test_F_environment_only_failures_become_blocked_env():
    execution = {
        "collected": 2,
        "passed": 0,
        "failed": 2,
        "errors": 0,
        "exit_code": 1,
        "product_failed": 0,
        "environment_failed": 2,
        "failures": [
            {"node": "a", "kind": "failed", "classification": "environment",
             "environment_kind": "missing_vcs_metadata", "message": "", "traceback": ""},
            {"node": "b", "kind": "failed", "classification": "environment",
             "environment_kind": "missing_tool", "message": "", "traceback": ""},
        ],
        "stdout": "",
        "stderr": "",
    }
    cause = environment_failure_cause(execution)
    assert cause is not None and cause.kind == "environment_failures"


# --- G: a Lexi veto still runs the tests ------------------------------------

def test_G_drift_veto_does_not_skip_execution(tmp_path: Path):
    """A doc/code disagreement vetoes, but Riley/Sable/Nova must still run so
    the report shows what the tests actually did."""
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs="# API\n\nPublic API includes `add` and `divide`.\n\n`divide` must return a float.\n",
    )
    reports = tmp_path / "reports"
    config = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(reports),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "sandbox": {"mode": "off"},
    }
    stub = ExecutionResult(
        collected=3, passed=3, failed=0, errors=0, exit_code=0, install_status="skipped",
        sandbox_status="off",
    )
    with patch("agents.nova.execute", return_value=stub) as nova_mock:
        report = Vesper(config).run(root, root / "docs")

    assert report.verdict == Verdict.VETO.value, "the drift veto must survive"
    assert nova_mock.called, "a veto must not skip test execution"
    nova_results = [r for r in report.role_results if r.role == "nova" and r.stage == "execute"]
    assert nova_results, "the report must contain a Nova execution result"


# --- H: LLM network is separate from sandbox network ------------------------

def test_H_llm_network_is_independent_of_sandbox_network():
    only_llm = {"allow_network": False, "llm": {"allow_network": True}}
    assert llm_network_allowed(only_llm) is True
    neither = {"allow_network": False, "llm": {}}
    assert llm_network_allowed(neither) is False
    # allow_network keeps its old meaning: it opens both.
    both = {"allow_network": True, "llm": {}}
    assert llm_network_allowed(both) is True


def test_H_llm_network_on_still_unshares_sandbox_net(tmp_path: Path):
    """With only the LLM network enabled, the sandboxed pytest command must keep
    network isolation: --unshare-all present, --share-net absent."""
    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()
    cmd = wrap_command(
        ["python", "-m", "pytest"],
        isolated_root=iso,
        work_dir=work,
        allow_network=False,  # the tested project stays offline
    )
    assert "--unshare-all" in cmd
    assert "--share-net" not in cmd


def test_H_scrub_secrets_redacts_key_values_and_shapes():
    text = (
        'db_password: "s3cr3t-value"\n'
        "token = abcdef\n"
        "key AKIAIOSFODNN7EXAMPLE here\n"
        "ghp_abcdefghijklmnOPQRSTUVWXYZ0123\n"
    )
    out = scrub_secrets(text)
    assert "s3cr3t-value" not in out
    assert "abcdef" not in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "ghp_abcdefghijklmnOPQRSTUVWXYZ0123" not in out
    assert "db_password" in out  # the key name survives, only the value is gone

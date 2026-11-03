"""Task 12c - kill the mutation survivors from the gate-3 run.

Survivors killed:

* ``agents/lexi.py:1546`` - ``or`` -> ``and`` in ``_from_llm`` verdict selection.
  An AND returns the right operand when both are truthy, so ``data["verdict"]
  = "VETO"`` combined with ``fallback.verdict = "CLEAR"`` would produce "CLEAR"
  (the fallback wins) rather than the LLM's actual verdict.

* ``agents/riley.py:129`` - ``and`` -> ``or`` in ``is_trivial_assert``.
  An OR would classify any ``assert func_call(...)`` as trivial, which is wrong
  for a function not in the isinstance/hasattr/bool whitelist.

* ``core/packets.py:209`` - ``return <expr>`` -> ``return None`` in PostAudit.
  Every caller that uses the dict result of to_dict() would get None.

* ``core/sandbox.py:69`` - ``return False`` -> ``return None`` when platform is
  not Linux. The caller expects a boolean, not None.

* ``policies/sable.py:478`` - ``==`` -> ``!=`` in ``len(parts) == 1 and parts[0]
  in {"rmtree", "system", "popen"}``. A bare ``rmtree(…)`` call would not be
  flagged, hiding a real destructive pattern from Sable's pre-flight scan.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


# --- m-408: agents/lexi.py:1546 (or -> and in _from_llm verdict) -------------


def test_from_llm_prefers_data_verdict_over_clear_fallback() -> None:
    """When the LLM says VETO and the fallback says CLEAR, the result must be VETO.

    With the original ``or``: "VETO" or "CLEAR" → "VETO" (left wins when truthy).
    With the mutant ``and``: "VETO" and "CLEAR" → "CLEAR" (right wins when both
    are truthy). This test kills the mutation.
    """
    from agents.lexi import _from_llm
    from core.packets import DriftReport, Verdict

    fallback = DriftReport(
        verdict=Verdict.CLEAR.value,
        findings=[],
        docs_files=[],
        code_files=[],
        test_files=[],
        notes="clean",
    )
    data = {"verdict": "VETO", "findings": [], "notes": "drift!"}
    result = _from_llm(data, fallback)
    assert result.verdict == Verdict.VETO.value, (
        f"LLM verdict VETO must prevail over fallback CLEAR, got {result.verdict}"
    )


# --- m-477: agents/riley.py:129 (and -> or in is_trivial_assert) -------------


def test_non_whitelisted_call_assertion_is_not_trivial() -> None:
    """assert compute_value(42) must NOT be classified as trivial.

    The condition at line 129 tests ``isinstance(test_node, ast.Call) and
    _call_name(test_node) in {"isinstance", "hasattr", "bool"}``.
    Mutated to ``or``, ANY call (including compute_value) would be trivial.
    """
    from agents.riley import is_trivial_assert

    tree = ast.parse("assert compute_value(42)\n")
    stmt = tree.body[0]
    assert isinstance(stmt, ast.Assert)
    assert is_trivial_assert(stmt) is False, (
        "a bare call not in the whitelist must not be trivial"
    )


def test_isinstance_call_assertion_is_trivial() -> None:
    """assert isinstance(x, int) IS trivial (the positive case of the condition)."""
    from agents.riley import is_trivial_assert

    tree = ast.parse("assert isinstance(x, int)\n")
    stmt = tree.body[0]
    assert isinstance(stmt, ast.Assert)
    assert is_trivial_assert(stmt) is True


# --- m-1386: core/packets.py:209 (return <expr> -> return None) --------------


def test_post_audit_to_dict_returns_dict_when_populated() -> None:
    """PostAudit.to_dict() must return a proper dict, not None."""
    from core.packets import DriftReport, PostAudit, RedTeamReview

    dr = DriftReport(
        verdict="CLEAR",
        findings=[],
        docs_files=["a.md"],
        code_files=["b.py"],
        test_files=[],
        notes="",
    )
    audit = PostAudit(lexi=dr, riley=None)
    d = audit.to_dict()
    assert isinstance(d, dict), f"to_dict must return a dict, got {type(d)}"
    assert "lexi" in d and "riley" in d


# --- m-1519: core/sandbox.py:69 (return False -> return None) ----------------


def test_is_bwrap_available_returns_false_on_non_linux(monkeypatch) -> None:
    """On a non-Linux platform, is_bwrap_available() must return exactly False,
    not None. The ``is False`` contract is used by callers and the report."""
    import core.sandbox as sandbox

    monkeypatch.setattr(sandbox.sys, "platform", "win32")
    result = sandbox.is_bwrap_available()
    assert result is False, f"expected False, got {result!r}"


# --- m-1828: policies/sable.py:478 (== -> !=) --------------------------------


def test_bare_rmtree_call_is_flagged(tmp_path: Path) -> None:
    """A bare ``rmtree("target")`` in a test file must be caught by the
    ``len(parts) == 1`` branch. Mutated to ``!=``, the branch would skip and
    the destructive call would go unrecorded.
    """
    from policies.sable import scan_tests_for_risks

    test_file = tmp_path / "tests" / "test_cleanup.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from shutil import rmtree\n"
        "\n"
        "def test_scratch():\n"
        "    rmtree('target')\n",
        encoding="utf-8",
    )

    hits, allowed, warnings = scan_tests_for_risks(tmp_path)
    # Both the import AND the call should be recorded:
    all_findings = hits + warnings
    rmtree_calls = [f for f in all_findings if "call rmtree" in f]
    assert rmtree_calls, (
        f"bare rmtree() call must be flagged; got hits={hits}, warnings={warnings}"
    )

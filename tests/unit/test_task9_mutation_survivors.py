"""Task 9 - kill tests for the self-test mutation survivors found on the gate run.

The self-test mutation audit (seed 42, 30 sampled) reported score 0.67 (20 killed,
10 survived). Eight of those ten are real, observable behaviours that the suite did
not yet pin down; each test below is written so the specific mutant flips it. Two
are genuine equivalents and are explained at the bottom rather than forced:

* ``agents/lexi.py:329``  ``or`` -> ``and`` in ``_is_prose_phrase``
* ``core/isolation.py:284`` ``==`` -> ``!=`` in an unreachable ``except AttributeError``

Everything here is offline and deterministic: no network, no LLM, no bwrap.
"""

from __future__ import annotations

import subprocess

from agents.aria import _extract_claim_keywords, _stems
from agents.lexi import _Mention, _is_placeholder
from agents.riley import review_plan
from core.packets import Scenario, TestPlan
from core.report import _failures_section
from core.runner import run_pytest


# --- aria.py:32  `word.endswith("ies") and len(word) > 4` -> `or` -------------
def test_aria_stems_does_not_yize_a_short_ies():
    """"ies" is 3 chars, so the ies->y rule must not fire.

    Mutating the guard's ``and`` to ``or`` makes the branch run on any ``ies``
    ending regardless of length, inventing a bogus ``"y"`` stem.
    """
    assert "y" not in _stems("ies")
    # The rule still applies to a genuine long word.
    assert "berry" in _stems("berries")


# --- aria.py:51  `len(p_low) >= 3 and ...` -> `>` -----------------------------
def test_aria_keeps_three_letter_camel_parts():
    """A 3-letter camelCase part is a keyword the moment it is not a stop word.

    Raising the bound to ``> 3`` would silently drop ``foo``/``bar`` from the
    extracted keyword list.
    """
    keywords = _extract_claim_keywords("getFooBar")
    assert "foo" in keywords
    assert "bar" in keywords


# --- lexi.py:383  second `return True` in _Mention.enumerated() -> False ------
def test_lexi_enumerated_detects_a_trailing_sibling():
    """A term that is first in a punctuation-only run is still enumerated.

    ``enumerated()`` has two exits; this one has no previous term but a following
    term with only ``","`` in the gap, so the second ``return True`` must fire.
    Flipping it to ``False`` would let an API-list term read as a promised symbol.
    """
    mention = _Mention(
        line="`add`, `divide`",
        heading="",
        intro="",
        is_list_item=False,
        in_paren=False,
        gap_before="",
        gap_after=", ",
        prev_term="",
        next_term="divide",
        clause_terms=("add", "divide"),
    )
    assert mention.enumerated() is True


# --- lexi.py:752  `if not docs_text: return False` -> `True` ------------------
def test_lexi_placeholder_is_false_on_empty_docs():
    """No docs text can make a term a placeholder, so the early guard says False.

    The ``True`` mutant would call everything a placeholder when there are no
    docs, silencing real drift.
    """
    assert _is_placeholder("anything", "") is False


# --- report.py:176  `classification or "product"` -> `and` --------------------
def test_report_environment_failure_is_not_counted_as_product():
    """A failure tagged environment must not land in the product bucket.

    With ``or`` mutated to ``and`` the expression ``"environment" and "product"``
    evaluates to ``"product"``, so the classified-environment failure would be
    counted as a product defect (product failures=1 instead of 0).
    """
    execution = {
        "failed": 1,
        "errors": 0,
        # product_failed deliberately absent so the section uses len(product).
        "failures": [
            {
                "node": "tests/test_a.py::test_needs_git",
                "classification": "environment",
                "message": "not a git repository",
                "traceback": "",
            }
        ],
    }
    section = "\n".join(_failures_section(execution))
    assert "product failures=0" in section
    assert "environment failures=1" in section


# --- runner.py:337  `exc.stderr or ""` -> `and` -------------------------------
def test_run_pytest_timeout_preserves_nonempty_stderr(tmp_path, monkeypatch):
    """On timeout the captured stderr must still reach the report, plus TIMEOUT.

    ``exc.stderr and ""`` returns the empty string for any non-empty stderr, so a
    mutant would drop the real error text and leave only ``"\\nTIMEOUT"``.
    """
    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="pytest", timeout=1, output="", stderr="E: collecting hung"
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    res = run_pytest(
        iso,
        work_dir=work,
        allow_network=False,
        timeout=1,
        config={"sandbox": {"mode": "off"}},
    )
    assert res.exit_code == 124
    assert "E: collecting hung" in res.stderr
    assert "TIMEOUT" in res.stderr


# --- sandbox.py:71  `return False` (bwrap missing) -> `return None` -----------
def test_is_bwrap_available_returns_false_when_binary_missing(monkeypatch):
    """A missing bwrap is a definite False, not None.

    ``is_bwrap_available`` promises a bool; the ``return None`` mutant satisfies
    most truthiness checks but breaks the ``is False`` contract the callers and
    the report rely on.
    """
    import core.sandbox as sandbox

    monkeypatch.setattr(sandbox.sys, "platform", "linux")
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert sandbox.is_bwrap_available() is False


# --- riley.py:496  `startswith("C") and not covered_by` -> `or` ---------------
def test_riley_does_not_call_a_covered_claim_uncovered():
    """A claim already covered by a strong test is not an uncovered claim.

    ``startswith("C") or not covered_by`` would list the covered claim as
    uncovered under ``veto_on_uncovered_claims``, manufacturing a gap that the
    plan already satisfies.
    """
    plan = TestPlan(
        scenarios=[
            Scenario(
                id="C1",
                title="add returns the sum",
                steps=["call add(2, 3)"],
                acceptance=["result equals 5"],
                negative=True,
                covered_by=["tests/test_calc.py::test_add"],
            )
        ]
    )
    review = review_plan(
        plan,
        config={"riley": {"veto_on_uncovered_claims": True}},
        project_root=None,
    )
    assert not any(gap.startswith("uncovered claims") for gap in review.gaps)


# The following two survivors are equivalent mutants and cannot be killed by any
# test without asserting on dead or unreachable code:
#
# * agents/lexi.py:329 - `not raw or re.search(r"\s", raw) or "/" in raw or "."
#   in raw`. Every token that makes any disjunct True (empty, whitespace, slash
#   or dot) also fails FUNC_LIKE_RE.match, so the fall-through `return match is
#   None or not match.group(1)` returns True anyway; and `and`-of-the-whole can
#   never be True for a clean identifier. The function's output is identical for
#   every input, so the mutant is equivalent.
#
# * core/isolation.py:284 - inside `except AttributeError:`, the branch that only
#   runs when Path.is_relative_to is missing (Python < 3.9). On the 3.11+ runtime
#   baseline line 281 always handles it, so line 284 is unreachable dead code and
#   flipping it is unobservable.

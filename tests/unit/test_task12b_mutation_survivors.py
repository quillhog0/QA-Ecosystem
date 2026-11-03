"""Task 12b - kill the sample-42 mutation survivors the new widening rotated in.

Each test pins one behaviour the run left surviving:

* ``agents/lexi.py:181`` - a whitespace-padded backtick is prose; the
  ``re.search(r"\\s", raw) or "/" in raw or "." in raw`` OR must stay an OR,
  because an AND would let `` divide() `` through to FUNC_LIKE_RE and fabricate
  a drift.
* ``agents/lexi.py:383`` - ``_is_prose_phrase("9foo")`` must be True: the
  OR at line 383 catches a non-identifier with no whitespace / slash / dot, and
  an AND would raise ``AttributeError`` on ``match.group(1)`` when ``match`` is
  None.
* ``agents/lexi.py:476`` - ``_Mention.cited()`` must reflect the ``narrated``
  field; a ``return None`` mutant keeps the call only-falsy-compatible when
  narrated is False, so the True branch is what proves the wiring.
* ``agents/lexi.py:1517`` - ``_from_llm`` must prefer the LLM ``notes`` over
  the heuristic fallback; AND would silently return the fallback (or ``None``
  stringified) whenever the LLM provided a note.
* ``core/environment.py:610`` - a failure with no message and no traceback
  stays a product failure (``None``); a ``return True`` mutant would let the
  classifier hide a defect behind exit 2.
* ``core/report.py:66`` - a non-empty stderr must reach the rendered Markdown;
  AND replaces it with ``""`` when stderr is truthy, so the report would
  silently drop the tail of a failing run.

Production code is never edited to pass a mutant; each claim is proved by the
test alone.
"""

from __future__ import annotations

from pathlib import Path

from agents.lexi import (
    _Mention,
    _backtick_functions,
    _from_llm,
    _is_prose_phrase,
)
from core.environment import classify_failure
from core.packets import DriftReport, FinalReport
from core.report import render_markdown


# --- lexi.py:181 - OR pre-filter in _backtick_functions --------------------

def test_backtick_functions_skips_whitespace_padded_call_form(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    # A space inside the backticks marks the span as prose, not an API call.
    # The pre-filter must short-circuit on the whitespace branch; swapping the
    # OR for an AND would let this through to FUNC_LIKE_RE and yield ``divide``.
    (docs / "api.md").write_text("Call ` divide() ` for arithmetic.\n", encoding="utf-8")
    names = _backtick_functions(docs, ("**/*.md",))
    assert "divide" not in names, (
        "a whitespace-padded backtick must be rejected by the OR pre-filter"
    )


def test_backtick_functions_still_accepts_clean_call_form(tmp_path: Path) -> None:
    # The other side of the same branch: a clean backtick still counts, so the
    # fix above is not a blanket "skip everything" that quietly flips the
    # mutant to a pass.
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "api.md").write_text("Use `divide()` for arithmetic.\n", encoding="utf-8")
    names = _backtick_functions(docs, ("**/*.md",))
    assert "divide" in names


# --- lexi.py:383 - OR tail in _is_prose_phrase -----------------------------

def test_is_prose_phrase_flags_non_identifier_token_without_whitespace() -> None:
    # "9foo" has no whitespace / slash / dot, so the pre-filter does not apply;
    # only FUNC_LIKE_RE can classify it. Under the OR the return is True; a
    # swap to AND would evaluate ``match.group(1)`` on a None match and raise.
    assert _is_prose_phrase("9foo") is True


def test_is_prose_phrase_accepts_bare_identifier() -> None:
    # Companion branch, so a blanket "always True" mutant is also killed.
    assert _is_prose_phrase("divide") is False
    assert _is_prose_phrase("divide()") is False


# --- lexi.py:476 - _Mention.cited() ---------------------------------------

def _mention(**overrides) -> _Mention:
    fields = {
        "line": "",
        "heading": "",
        "intro": "",
        "is_list_item": False,
        "in_paren": False,
        "gap_before": "",
        "gap_after": "",
        "prev_term": "",
        "next_term": "",
        "clause_terms": (),
    }
    fields.update(overrides)
    return _Mention(**fields)


def test_mention_cited_returns_true_when_narrated() -> None:
    # ``cited()`` is the public accessor the external-reference filter uses;
    # ``return None`` (the mutant) is falsy so the True branch is the kill shot.
    m = _mention(narrated=True)
    assert m.cited() is True


def test_mention_cited_returns_false_when_not_narrated() -> None:
    m = _mention(narrated=False)
    assert m.cited() is False


# --- lexi.py:1517 - _from_llm notes fallback ------------------------------

def test_from_llm_keeps_llm_notes_over_the_fallback() -> None:
    fallback = DriftReport(verdict="CLEAR", notes="heuristic note")
    data = {"verdict": "CLEAR", "notes": "llm note", "findings": []}
    out = _from_llm(data, fallback)
    # ``or`` picks the LLM note; ``and`` would hand back the fallback.
    assert out.notes == "llm note"


def test_from_llm_falls_back_to_heuristic_notes_when_llm_is_silent() -> None:
    fallback = DriftReport(verdict="CLEAR", notes="heuristic note")
    out = _from_llm({"verdict": "CLEAR", "findings": []}, fallback)
    # ``or`` returns the fallback when the LLM key is absent; ``and`` would
    # short-circuit on the missing key and stringify ``None``.
    assert out.notes == "heuristic note"


# --- core/environment.py:610 - empty failure text -------------------------

def test_classify_failure_with_no_text_is_not_an_env_cause() -> None:
    # An empty message + traceback means "no evidence of an environment cause";
    # the classifier must stay silent (``None`` = product failure) rather than
    # smuggle the run through exit 2.
    assert classify_failure({"node": "t", "message": "", "traceback": ""}) is None


# --- core/report.py:66 - stderr passthrough -------------------------------

def test_render_markdown_preserves_a_non_empty_stderr() -> None:
    report = FinalReport(
        run_id="r1",
        verdict="CLEAR",
        execution={"stderr": "boom at the tail"},
    )
    text = render_markdown(report)
    # Under ``and`` the line reduces to ``_short(str(""), 2000)``, and the
    # stderr would silently disappear from the Markdown report.
    assert "boom at the tail" in text

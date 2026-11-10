"""Task 10 - kill mutation survivors with behaviour tests (never production edits).

Adding the false-positive fixes to ``agents/lexi.py`` and ``core/environment.py``
grew the candidate pool, so the seeded sample (``seed=42``, 30 of the whole tree)
landed on a set that includes these survivors. Each test here pins one mutant's
*observable* behaviour: the unmutated source passes it, the mutated source fails
it. Nothing here changes production code; it only makes an existing gap visible.

Survivors targeted (from the run's ``survived_mutants``):
  agents/lexi.py:331   _is_prose_phrase('') must be True   (bool_constant)
  agents/lexi.py:392   _Mention.enumerated() prev-token    (return_value)
  agents/riley.py:106  _handler_swallows call body         (bool_op and->or)
  agents/riley.py:623  environment_failure_cause filter    (bool_op and->or)
  core/packets.py:186  ExecutionResult.to_dict omitted     (arithmetic -->+)
  core/report.py:195   _failures_section return lines      (return_value)
"""

from __future__ import annotations

import ast

from agents.lexi import _Mention, _is_prose_phrase
from agents.riley import _handler_swallows, environment_failure_cause
from core.packets import ExecutionResult
from core.report import _failures_section


# --- agents/lexi.py:331 ------------------------------------------------------


def test_is_prose_phrase_of_empty_token_is_true():
    # Mutating ``return True`` to ``return False`` would treat an empty span as a
    # candidate symbol; an empty token is always prose.
    assert _is_prose_phrase("") is True
    assert _is_prose_phrase("   ") is True


# --- agents/lexi.py:392 ------------------------------------------------------


def _mention(**over):
    base = dict(
        line="", heading="", intro="", is_list_item=False, in_paren=False,
        gap_before="", gap_after="", prev_term="", next_term="", clause_terms=(),
    )
    base.update(over)
    return _Mention(**base)


def test_mention_enumerated_by_preceding_punctuation():
    # A term whose previous term is separated only by punctuation is enumerated;
    # returning None instead of True would let a vocabulary-list item veto.
    m = _mention(prev_term="alpha", gap_before=", ", next_term="")
    assert m.enumerated() is True


# --- agents/riley.py:106 -----------------------------------------------------


def _except_handler(source: str) -> ast.ExceptHandler:
    mod = ast.parse(source)
    return mod.body[0].handlers[0]


def test_handler_running_a_call_is_not_swallowed():
    # ``and`` -> ``or`` would call any expression statement a swallow. A handler
    # that actually does work (a call) is not swallowing.
    handler = _except_handler("try:\n    x()\nexcept ValueError:\n    log('boom')\n")
    assert _handler_swallows(handler) is False


def test_handler_with_only_pass_is_swallowed():
    handler = _except_handler("try:\n    x()\nexcept Exception:\n    pass\n")
    assert _handler_swallows(handler) is True


def test_handler_narrow_valueerror_pass_is_not_swallowed():
    handler = _except_handler("try:\n    x()\nexcept ValueError:\n    pass\n")
    assert _handler_swallows(handler) is False


# --- agents/riley.py:623 -----------------------------------------------------


def test_environment_cause_names_only_environment_failures():
    # ``and`` -> ``or`` would drag a product failure into the environmental list;
    # a run that did judge the project must not have that name in the sentence.
    execution = {
        "failed": 2,
        "product_failed": 0,
        "environment_failed": 2,
        "failures": [
            {"node": "env_test", "classification": "environment", "environment_kind": "missing_tool"},
            {"node": "prod_test", "classification": "product"},
        ],
    }
    cause = environment_failure_cause(execution)
    assert cause is not None
    assert "env_test" in cause.cause
    assert "prod_test" not in cause.cause


# --- core/packets.py:186 -----------------------------------------------------


def test_to_dict_omitted_counts_are_correct():
    # ``len - MAX`` mutated to ``len + MAX`` would report spurious omissions for a
    # small run and wrong counts for a large one.
    from core.packets import MAX_FAILURES_KEPT

    small = ExecutionResult(failures=[{"node": str(i)} for i in range(MAX_FAILURES_KEPT)])
    assert "failures_omitted" not in small.to_dict()

    big = ExecutionResult(failures=[{"node": str(i)} for i in range(MAX_FAILURES_KEPT + 7)])
    assert big.to_dict()["failures_omitted"] == 7


# --- core/report.py:195 ------------------------------------------------------


def test_failures_section_returns_lines_when_only_failed_count():
    # Returning None instead of the assembled lines would drop the whole section
    # when a run failed but carried no structured failure detail.
    lines = _failures_section({"failures": [], "failed": 3, "errors": 0})
    assert isinstance(lines, list)
    assert any("Test failures" in line for line in lines)

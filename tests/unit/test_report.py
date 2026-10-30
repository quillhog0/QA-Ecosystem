from __future__ import annotations

from core.packets import FinalReport, RoleResult, Verdict
from core.report import _short, _yaml, append_result, render_markdown


def _report_with_mutation(status: str) -> FinalReport:
    return FinalReport(
        run_id="r-report",
        verdict=Verdict.CLEAR.value,
        sandbox="off",
        mutation={
            "status": status,
            "score": 0.75,
            "killed": 3,
            "survived": 1,
            "timeout": 0,
            "error": 0,
            "min_score": 0.0,
            "survived_mutants": [
                {
                    "id": "m-001",
                    "location": "calc.py:1",
                    "type": "arithmetic",
                    "original": "+",
                    "mutated": "-",
                }
            ],
        },
    )


def test_render_markdown_completed_mutation_section():
    """status == 'completed' must render the score block (guards `elif status == "completed"`)."""
    md = render_markdown(_report_with_mutation("completed"))
    assert "## Mutation testing" in md
    assert "- **status:** completed" in md
    assert "- **score:** 75.0% (3 killed, 1 survived, 0 timeout, 0 error)" in md
    assert "### Surviving mutants" in md
    assert "`calc.py:1`: `+` → `-`" in md


def test_render_markdown_skipped_mutation_section():
    md = render_markdown(_report_with_mutation("skipped"))
    assert "- **status:** skipped (disabled)" in md
    assert "- **score:**" not in md


def test_short_returns_short_text_unchanged():
    """Short text must be returned as-is, not None (guards `return text` in _short)."""
    assert _short("hello", 4000) == "hello"
    assert _short("", 2000) == ""
    # Boundary: exactly at the limit stays unchanged
    assert _short("x" * 10, 10) == "x" * 10


def test_short_truncates_long_text():
    out = _short("x" * 15, 10)
    assert out.startswith("x" * 10)
    assert "...[truncated 5 chars]" in out


def test_render_markdown_keeps_short_execution_stdout():
    """Short stdout must appear verbatim in the report, not as null (guards _short return)."""
    report = FinalReport(
        run_id="r-exec",
        verdict=Verdict.CLEAR.value,
        sandbox="off",
        execution={"stdout": "1 passed in 0.01s", "stderr": ""},
    )
    md = render_markdown(report)
    assert "1 passed in 0.01s" in md
    assert "stdout: null" not in md


def test_yaml_returns_dumped_string_preserving_insertion_order():
    """report.py:115 - _yaml dumps with sort_keys=False (and returns text, not None).

    Keys are written out of alphabetical order so flipping sort_keys to True
    reorders the output; allow_unicode keeps non-ASCII text intact.
    """
    out = _yaml({"zeta": 1, "alpha": 2, "list": [1, 2]})
    assert out == "zeta: 1\nalpha: 2\nlist:\n- 1\n- 2"


def test_append_result_returns_the_appended_result():
    """report.py:126 - append_result returns the same RoleResult it appended."""
    results: list[RoleResult] = []
    result = RoleResult(role="lexi", stage="drift_check", verdict="CLEAR", summary="ok")
    returned = append_result(results, result)
    assert returned is result
    assert results == [result]
"""Task 12 - kill the mutation survivors the new findings sample rotated in.

Each test pins one behaviour the seed-42 mutation run left surviving: a chained
``is not`` comparison is a live assertion, a clipped pytest message keeps its
tail, an evidence excerpt keeps its tail, an import error naming the project's
own module stays a product failure, and a dropped unsafe SARIF path stays
``None`` at each guard. Production code is never edited to pass a mutant; the
claim is proven by the test.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

from agents.riley import _one_line as riley_one_line
from agents.riley import is_tautology_assert
from core.environment import classify_failure
from core.findings import MAX_EVIDENCE_CHARS, _one_line as findings_one_line
from core.sarif import _safe_rel_path


def _fn(source: str) -> ast.FunctionDef:
    module = ast.parse(textwrap.dedent(source))
    return next(n for n in module.body if isinstance(n, ast.FunctionDef))


def _first_assert(fn: ast.FunctionDef) -> ast.Assert:
    return next(n for n in ast.walk(fn) if isinstance(n, ast.Assert))


def test_chained_is_not_comparison_after_a_call_is_not_a_tautology() -> None:
    # ``a is not b is not c`` parses to one Compare with TWO ops: the single-op
    # None-check rule must not widen to it (and/or confusion), because the chain
    # compares real call results and can genuinely fail.
    fn = _fn(
        """
        def test_pair():
            first = load_one()
            second = load_two()
            assert first is not second is not None
        """
    )
    stmt = _first_assert(fn)
    assert isinstance(stmt.test, ast.Compare) and len(stmt.test.ops) == 2
    assert is_tautology_assert(stmt, fn_node=fn) is False


def test_riley_clip_keeps_the_tail_of_a_long_message() -> None:
    # riley._one_line collapses a pytest message and keeps its END (the
    # "E   assert ..." line is what matters); the slice must stay negative.
    tail = "E assert 1 == 2"
    text = "filler " * 100 + tail
    out = riley_one_line(text, limit=50)
    assert out.startswith("...")
    assert out.endswith(tail.replace(" ", "")) or out.endswith(tail)
    assert len(out) == 53  # "..." + exactly limit chars
    assert "filler" in out


def test_findings_evidence_keeps_the_head_of_a_long_excerpt() -> None:
    head = "boom-at-the-front"
    text = head + " " + "junk " * 100
    out = findings_one_line(text)
    assert out.startswith(head)
    assert out.endswith("...")
    assert len(out) == MAX_EVIDENCE_CHARS
    assert "\n" not in out


def test_own_module_import_error_stays_a_product_failure(tmp_path: Path) -> None:
    # classify_failure returning None means "count it against the project";
    # the own-module guard must return exactly that None, not a truthy cause.
    root = tmp_path / "isolated_copy"
    (root / "mypkg").mkdir(parents=True)
    (root / "mypkg" / "__init__.py").write_text("", encoding="utf-8")
    tb = (
        "tests/test_api.py:3: in <module>\n"
        "    import mypkg.sub\n"
        "E   ModuleNotFoundError: No module named 'mypkg.sub'\n"
    )
    assert classify_failure({"node": "t", "message": "", "traceback": tb},
                            project_root=root) is None


def test_safe_rel_path_drops_absolute_traversal_and_reserved_roots() -> None:
    assert _safe_rel_path("/home/user/x.py") is None
    assert _safe_rel_path("../outside.py") is None
    assert _safe_rel_path("tmp/staging/x.py") is None
    assert _safe_rel_path("src\\..\\secrets\\x.py") is None
    assert _safe_rel_path("tests/test_a.py") == "tests/test_a.py"

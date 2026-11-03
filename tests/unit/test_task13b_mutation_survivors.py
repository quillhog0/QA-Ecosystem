"""Task 13b - kill the mutation survivors the Task 13 sample rotated in.

The gate run scored 0.86 (25 killed, 4 survived). Each survivor here is a real,
observable behaviour the suite did not yet pin; every one is killed with a
behavioural assertion only. No production code is edited to dodge a mutant and
no test is skipped or marked expected-to-fail.

Survivors targeted:
  agents/riley.py:47   ``_observes_code`` ``True`` -> ``False``
  core/isolation.py:144 git-listing guard ``and`` -> ``or``
  core/isolation.py:252 secret-named symlink ``should_remove = True`` -> ``False``
  core/orchestrator.py:495 Nova verdict ``or`` -> ``and``
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

from core.orchestrator import Vesper
from core.packets import Verdict
from tests.support import mini_project


def test_observes_code_flags_attribute_call_as_real_work():
    """riley.py:47 - a non-assert attribute call means the test observed code.

    ``obj.get()`` returns a value that ``assert value is not None`` can act on,
    so the body is not vacuous. Flipping the return to ``False`` would wrongly
    call a live None-check vacuous, so this pins the real-work branch.
    """
    from agents.riley import _observes_code

    produced = ast.parse(
        "def test_a():\n    value = obj.get()\n    assert value is not None\n"
    ).body[0]
    assert _observes_code(produced) is True

    # An assert-style helper method is not a producer, so the body stays vacuous.
    helper = ast.parse(
        "def test_a():\n    self.assertEqual(1, 1)\n"
    ).body[0]
    assert _observes_code(helper) is False


def test_expand_untracked_dir_walks_disk_when_git_lists_nothing(tmp_path, monkeypatch):
    """isolation.py:144 - git success with an empty listing must use the walk.

    The guard is ``returncode == 0 and stdout``. Weakening it to ``or`` would
    enter the git branch on an empty result and return nothing, silently dropping
    files that exist on disk. A subtree git will not describe is exactly the
    documented fallback, so the on-disk file must still come back.
    """
    from core import isolation

    src = tmp_path / "proj"
    subtree = src / "generated"
    subtree.mkdir(parents=True)
    (subtree / "model.py").write_text("x = 1\n", encoding="utf-8")

    class _EmptySuccess:
        returncode = 0
        stdout = b""

    monkeypatch.setattr(
        isolation.subprocess, "run", lambda *a, **k: _EmptySuccess()
    )

    files = isolation._expand_untracked_dir(src, Path("generated"))
    assert "generated/model.py" in files


def test_secret_named_symlink_is_removed_even_with_safe_in_tree_target(tmp_path):
    """isolation.py:252 - a symlink named like a secret is always removed.

    The target resolves to a harmless file inside the destination, so the earlier
    outside/secret checks leave ``should_remove`` false; only the name-based guard
    (``is_secret_path(path)``) saves it. Setting ``should_remove = False`` would
    leave a ``.env`` symlink in the isolated copy - a secret leak.
    """
    from core import isolation

    dest = tmp_path / "dst"
    dest.mkdir()
    safe = dest / "readme.txt"
    safe.write_text("harmless\n", encoding="utf-8")
    link = dest / ".env"
    link.symlink_to(safe)

    skipped: list[dict] = []
    isolation._clean_symlinks_and_secrets(dest, skipped)

    assert not link.exists(), "secret-named symlink must be removed"
    assert any(
        rec["path"] == ".env" and rec["reason"] == "symlink-removed"
        for rec in skipped
    )
    # The harmless target it pointed at is real content and must survive.
    assert safe.exists()


def test_nova_vetoes_when_tests_failed_even_with_collection(tmp_path):
    """orchestrator.py:495 - collected>0 with a failure is Nova's own VETO.

    The verdict is ``VETO if collected <= 0 or nova_failed``. Weakening it to
    ``and`` would let Nova report CLEAR whenever tests were collected, hiding a
    genuine product failure behind a later stage. Pin Nova's own result directly.
    """
    from core.isolation import IsolationHandle
    from core.packets import ExecutionResult, FinalReport, HandoffPacket

    project = mini_project(tmp_path / "proj")
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    vesper = Vesper(
        {"reports_dir": str(tmp_path / "reports"), "isolation_method": "copy"}
    )
    handle = IsolationHandle(
        live_root=project,
        isolated_root=project,
        method="copy",
        read_only=False,
        skipped_secrets=[],
    )
    packet = HandoffPacket(
        run_id="r1",
        stage="init",
        from_role="vesper",
        to_role="lexi",
        project_root=str(project.parent),
        docs_root=str(project / "docs"),
        isolated_root=str(project),
    )
    report = FinalReport(run_id="r1", verdict=Verdict.PENDING.value)
    failed_exec = ExecutionResult(
        collected=3, passed=2, failed=1, exit_code=1, sandbox_status="off"
    )
    with patch("agents.nova.execute", return_value=failed_exec):
        vesper._loop(packet, handle, work_dir, report)

    nova_results = [r for r in vesper.role_results if r.role == "nova"]
    assert nova_results, "expected a Nova execute result"
    assert nova_results[-1].verdict == Verdict.VETO.value

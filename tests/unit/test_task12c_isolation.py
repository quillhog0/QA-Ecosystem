"""Task 12c - isolation must expand untracked directories file by file.

``git ls-files -co --exclude-standard`` collapses a fully untracked directory
(most typically a nested git repository) into a single ``dir/`` entry. Before
the fix, ``shutil.copy2`` on that directory raised ``OSError``, which was
silently swallowed by a bare ``except OSError: pass``, so the whole tree was
missing from the isolated copy. The tests below verify:

1. A nested git repository (the exporter pattern) is correctly expanded and
   its files are present in the isolated copy.
2. A gitignored directory is NOT copied.
3. ``node_modules`` is NOT copied.
4. A skipped path appears in ``isolation.skipped`` with a reason.
5. Lexi finds a TypeScript symbol from the untracked dir without the old
   live_root extra-bundle fallback.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.isolation import (
    _copy_git_working_tree,
    _expand_untracked_dir,
    create_isolation,
    isolation_destination,
    release_isolation,
)


def _init_git(project: Path) -> None:
    """Init a git repo with user config and one commit of all current content."""
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(project), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(project), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(project), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- expand untracked nested directory ----------------------------------------


def test_untracked_nested_dir_is_copied_file_by_file(tmp_path: Path) -> None:
    """A fully untracked nested git repository (dir/ entry from git ls-files)
    is expanded recursively so all its files land in the isolated copy."""
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "main.py", "def run(): pass\n")
    _write(project / "docs" / "api.md", "# API\n")
    _init_git(project)

    # Create a nested git repo (untracked from the parent's point of view):
    nested = project / "exporter"
    nested.mkdir()
    _write(nested / "src" / "core" / "types.ts", "export function listConversations(): string[] { return []; }\n")
    _write(nested / "README.md", "# Exporter\n")
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)

    # git ls-files -co from the parent collapses it:
    proc = subprocess.run(
        ["git", "-C", str(project), "ls-files", "-co", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    )
    # The nested repo area shows as at most one "exporter/" entry (or is absent);
    # individual files are NOT listed by the parent.
    listed = proc.stdout.splitlines()
    assert not any("src/core/types.ts" in l for l in listed), (
        "precondition: the parent should NOT list nested repo files individually"
    )

    # Now test the expansion helper directly:
    expanded = _expand_untracked_dir(project, Path("exporter"))
    # At least types.ts must appear (via git -C exporter ls-files -co):
    assert any("types.ts" in p for p in expanded), f"expansion missed types.ts: {expanded}"

    # And through the full isolation path:
    dest = isolation_destination(live_root=project, run_id="12c-nested")
    handle = create_isolation(project, dest)
    try:
        assert (dest / "exporter" / "src" / "core" / "types.ts").is_file(), (
            "the nested repo file must be present in the isolated copy"
        )
        assert (dest / "exporter" / "README.md").is_file()
    finally:
        release_isolation(handle, keep=False)


def test_gitignored_dir_is_not_copied(tmp_path: Path) -> None:
    """A directory excluded by .gitignore is not listed by
    git ls-files --exclude-standard and therefore never appears in the copy."""
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "main.py", "x = 1\n")
    _write(project / ".gitignore", "secrets/\n")
    _init_git(project)

    # Create a gitignored directory with content:
    _write(project / "secrets" / "hidden.txt", "classified\n")

    dest = isolation_destination(live_root=project, run_id="12c-gitignored")
    handle = create_isolation(project, dest)
    try:
        assert not (dest / "secrets" / "hidden.txt").exists(), (
            "gitignored content must not be copied"
        )
    finally:
        release_isolation(handle, keep=False)


def test_node_modules_not_copied(tmp_path: Path) -> None:
    """SKIP_DIR_NAMES prevents node_modules from entering the isolated copy
    even if git lists files underneath it."""
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "index.js", "module.exports = {};\n")
    _write(project / ".gitignore", "*.log\n")
    _init_git(project)

    # node_modules is untracked but NOT gitignored (only *.log is), so it shows
    # up in git ls-files -co output; the isolation logic must still skip it.
    _write(project / "node_modules" / "left-pad" / "index.js", "module.exports = 1;\n")

    dest = isolation_destination(live_root=project, run_id="12c-nm")
    handle = create_isolation(project, dest)
    try:
        assert not (dest / "node_modules").exists(), (
            "node_modules must never enter the isolated copy"
        )
    finally:
        release_isolation(handle, keep=False)


def test_skipped_file_appears_in_report(tmp_path: Path) -> None:
    """A secret file is recorded in isolation.skipped with reason 'secret'."""
    project = tmp_path / "project"
    project.mkdir()
    _write(project / "main.py", "x = 1\n")
    _write(project / ".env", "SECRET=leak\n")
    _init_git(project)

    dest = isolation_destination(live_root=project, run_id="12c-skip-report")
    handle = create_isolation(project, dest)
    try:
        assert handle.skipped, "at least one skip must be recorded"
        secrets = [r for r in handle.skipped if r["reason"] == "secret"]
        assert any(r["path"] == ".env" for r in secrets), (
            f".env secret skip not found in {handle.skipped}"
        )
        assert ".env" in handle.skipped_secrets
        # Verify to_dict includes the skipped list:
        d = handle.to_dict()
        assert "skipped" in d
        assert any(item["path"] == ".env" for item in d["skipped"])
    finally:
        release_isolation(handle, keep=False)


def test_copy_failure_is_recorded_and_surfaces_warn(tmp_path: Path, monkeypatch) -> None:
    """A file the copy could not carry is never a silent drop: it lands in
    isolation.skipped with a copy-failed reason, the orchestrator turns that
    into report.warnings, and the Markdown report renders a WARN line."""
    import shutil as shutil_module

    from core.orchestrator import Vesper
    from core.report import render_markdown

    project = tmp_path / "project"
    project.mkdir()
    _write(project / "main.py", "def run(): pass\n")
    _write(project / "setup.cfg", "[metadata]\nname = demo\n")
    _init_git(project)

    real_copy2 = shutil_module.copy2

    def failing_copy2(src, dst, **kwargs):
        if Path(src).name == "setup.cfg":
            raise OSError("simulated permission denied")
        return real_copy2(src, dst, **kwargs)

    monkeypatch.setattr("core.isolation.shutil.copy2", failing_copy2)
    dest = isolation_destination(live_root=project, run_id="12c-copy-fail")
    handle = create_isolation(project, dest)
    try:
        failures = [r for r in handle.skipped if r["reason"].startswith("copy-failed")]
        assert any(r["path"] == "setup.cfg" for r in failures), (
            f"the failed copy must be recorded, got {handle.skipped}"
        )
        # Secrets alone must not produce the WARN: only real carry failures do.
        warn_records = [r for r in handle.skipped if not r["reason"].startswith("secret")]
        assert warn_records
    finally:
        monkeypatch.undo()
        release_isolation(handle, keep=False)

    # The orchestrator wires non-secret skips into report.warnings:
    reports = tmp_path / "reports"
    config = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(reports),
        "llm": {"provider": "none"},
        "isolation_method": "auto",
        "keep_isolated_copy": False,
    }
    monkeypatch.setattr("core.isolation.shutil.copy2", failing_copy2)
    try:
        report = Vesper(config).run(project, project / "docs")
    finally:
        monkeypatch.undo()
    assert any("setup.cfg" in w and "copy-failed" in w for w in report.warnings), (
        f"the skipped path must surface as a warning, got {report.warnings}"
    )
    md = render_markdown(report)
    assert "**warning:**" in md and "setup.cfg" in md, (
        "the Markdown report must render the isolation WARN line"
    )


# --- end-to-end: untracked TypeScript found without live_root fallback --------


def test_lexer_finds_untracked_typescript_after_isolation(tmp_path: Path) -> None:
    """After the isolation fix, the untracked nested directory is in the copy;
    Lexi reads only the isolated root and still sees the TypeScript symbol."""
    from agents.lexi import _heuristic_drift

    project = tmp_path / "project"
    project.mkdir()
    _write(project / "main.py", "def run(): pass\n")
    _write(project / "docs" / "api.md", "The exporter exposes `listConversations()` for callers.\n")
    _init_git(project)

    # Create an untracked nested git repo with TypeScript:
    nested = project / "exporter"
    nested.mkdir()
    _write(
        nested / "src" / "core" / "types.ts",
        "export function listConversations(): string[] { return []; }\n",
    )
    subprocess.run(["git", "init"], cwd=nested, check=True, capture_output=True)

    dest = isolation_destination(live_root=project, run_id="12c-e2e")
    handle = create_isolation(project, dest)
    try:
        # The file must be in the isolated copy:
        ts_file = dest / "exporter" / "src" / "core" / "types.ts"
        assert ts_file.is_file(), f"TypeScript file missing from isolated copy"

        # Lexi must NOT report the symbol as missing code:
        report = _heuristic_drift(
            docs_root=dest / "docs",
            project_root=dest,
            config={},
        )
        missing = {f.path for f in report.findings if f.kind == "missing_code"}
        assert "listConversations" not in missing, (
            "a symbol present in the isolated copy (untracked dir expanded) must not be drift"
        )
    finally:
        release_isolation(handle, keep=False)


# --- open-question / future prose severity cap -------------------------------


def test_speculative_open_question_caps_at_low(tmp_path: Path) -> None:
    """A term under an 'Open Questions' heading receives severity 'low' even
    if it is code-shaped (snake_case)."""
    from agents.lexi import _heuristic_drift

    docs = tmp_path / "docs"
    _write(
        docs / "plan.md",
        "# Roadmap\n\n"
        "## Open Questions\n\n"
        "Should we introduce `batch_exporter` for the next release?\n",
    )
    # No code file contains batch_exporter
    _write(tmp_path / "lib.py", "def helper(): pass\n")

    report = _heuristic_drift(docs_root=docs, project_root=tmp_path, config={})
    findings = {f.path: f for f in report.findings if f.kind == "missing_code"}
    if "batch_exporter" in findings:
        assert findings["batch_exporter"].severity == "low", (
            f"expected severity 'low' for speculative mention, got {findings['batch_exporter'].severity}"
        )


def test_speculative_modal_caps_at_low(tmp_path: Path) -> None:
    """A term mentioned with a future modal ('could', 'might') is not a present
    promise: severity is capped at 'low'."""
    from agents.lexi import _heuristic_drift

    docs = tmp_path / "docs"
    _write(
        docs / "ideas.md",
        "# Ideas\n\n"
        "We could introduce `async_aggregator` in the next sprint.\n",
    )
    _write(tmp_path / "lib.py", "def helper(): pass\n")

    report = _heuristic_drift(docs_root=docs, project_root=tmp_path, config={})
    findings = {f.path: f for f in report.findings if f.kind == "missing_code"}
    if "async_aggregator" in findings:
        assert findings["async_aggregator"].severity == "low", (
            f"expected severity 'low' for modal mention, got {findings['async_aggregator'].severity}"
        )


def test_guard_present_tense_promise_stays_high(tmp_path: Path) -> None:
    """The opposite: a plain present-tense API promise with no speculative
    heading still gets severity 'high' (code-shaped and not under an open
    question heading)."""
    from agents.lexi import _heuristic_drift

    docs = tmp_path / "docs"
    _write(
        docs / "api.md",
        "# API Reference\n\n"
        "## Endpoints\n\n"
        "The library exposes `batch_processor` for high-throughput ingestion.\n",
    )
    _write(tmp_path / "lib.py", "def helper(): pass\n")

    report = _heuristic_drift(docs_root=docs, project_root=tmp_path, config={})
    findings = {f.path: f for f in report.findings if f.kind == "missing_code"}
    assert "batch_processor" in findings, "the symbol must be reported as missing"
    assert findings["batch_processor"].severity == "high", (
        f"a present-tense code-shaped promise must be 'high', got {findings['batch_processor'].severity}"
    )

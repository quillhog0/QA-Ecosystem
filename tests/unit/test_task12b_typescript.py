"""Task 12b - Lexi must treat TypeScript / JavaScript as real project code.

A symbol defined in a ``.ts`` / ``.tsx`` / ``.mjs`` / ``.cjs`` / ``.jsx`` file is
not missing code, and the code index must not be widened so far that a
dependency's or a build product's identifier can silence a real drift. The same
rule applies to the doc scan: ``node_modules/**/README.md`` or a
``README.md.bak`` is not a live project doc. The tests below pin those three
behaviours and the shared inventory helpers that enforce them.
"""

from __future__ import annotations

from pathlib import Path

from agents.inventory import (
    CODE_SKIP,
    SOURCE_SUFFIXES,
    is_backup_doc,
    list_docs,
    list_sources,
    public_names,
    split_code_and_tests,
)
from agents.lexi import _heuristic_drift


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# --- inventory extensions ---------------------------------------------------

def test_source_suffixes_advertised_by_inventory() -> None:
    # The set Lexi indexes must be visible here too, so a regression that drops
    # a language is caught at the shared source of truth, not just at Lexi.
    assert {".py", ".ts", ".tsx", ".mjs", ".cjs", ".jsx"} <= set(SOURCE_SUFFIXES)
    # Vendored / generated directories are always skipped by the code index;
    # a missing entry means a bundled dependency could silence a real drift.
    assert {"node_modules", "dist", "build", ".venv", "venv"} <= CODE_SKIP


def test_list_sources_picks_up_web_extensions_and_skips_vendored(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "core.ts", "export function hello() {}\n")
    _write(tmp_path / "src" / "view.jsx", "export default function View() {}\n")
    _write(tmp_path / "src" / "legacy.cjs", "module.exports = {};\n")
    _write(tmp_path / "lib.py", "def helper():\n    return 1\n")
    _write(tmp_path / "node_modules" / "left-pad" / "index.ts", "export function hello() {}\n")
    _write(tmp_path / "dist" / "bundle.js", "var hello = 1;\n")
    _write(tmp_path / ".venv" / "site-packages" / "pkg" / "mod.py", "def helper(): pass\n")
    files = list_sources(tmp_path)
    rels = {f.relative_to(tmp_path).as_posix() for f in files}
    assert rels == {
        "src/core.ts",
        "src/view.jsx",
        "src/legacy.cjs",
        "lib.py",
    }, f"vendored paths leaked into the code index: {rels}"


def test_public_names_ts_extracts_declarations(tmp_path: Path) -> None:
    ts = tmp_path / "types.ts"
    _write(
        ts,
        "export interface Conversation { id: string }\n"
        "export class OtterAdapter {\n"
        "    async listConversations() { return []; }\n"
        "}\n"
        "export const serviceId = 'otter';\n",
    )
    names = set(public_names(ts))
    assert {"Conversation", "OtterAdapter", "listConversations", "serviceId"} <= names


def test_split_code_and_tests_recognises_js_test_layout(tmp_path: Path) -> None:
    _write(tmp_path / "src" / "app.ts", "export function run() {}\n")
    _write(tmp_path / "src" / "app.test.ts", "it('runs', () => {});\n")
    _write(tmp_path / "__tests__" / "spec.ts", "describe('x', () => {});\n")
    _write(tmp_path / "lib.py", "def f(): pass\n")
    _write(tmp_path / "tests" / "test_lib.py", "def test_f(): pass\n")
    code, tests = split_code_and_tests(list_sources(tmp_path), tmp_path)
    code_rels = {p.relative_to(tmp_path).as_posix() for p in code}
    test_rels = {p.relative_to(tmp_path).as_posix() for p in tests}
    assert code_rels == {"src/app.ts", "lib.py"}
    assert test_rels == {"src/app.test.ts", "__tests__/spec.ts", "tests/test_lib.py"}


def test_backup_doc_suffixes_are_filtered(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs / "README.md", "# Live\n")
    _write(docs / "README.md.bak", "# Old\n")
    _write(docs / "notes.md.orig", "# Merged away\n")
    _write(docs / "scratch.md~", "# Editor swap\n")
    listed = {p.name for p in list_docs(docs)}
    assert listed == {"README.md"}, f"backup doc leaked into the doc scan: {listed}"
    assert is_backup_doc(Path("a/README.md.bak"))
    assert is_backup_doc(Path("a/README.md.orig"))
    assert is_backup_doc(Path("a/notes.md~"))
    assert not is_backup_doc(Path("a/README.md"))


def test_list_docs_skips_vendored_markdown(tmp_path: Path) -> None:
    _write(tmp_path / "docs" / "GUIDE.md", "# Guide\n")
    _write(tmp_path / "node_modules" / "pkg" / "README.md", "# Dep read me\n")
    _write(tmp_path / "dist" / "README.md", "# Generated\n")
    rels = {p.relative_to(tmp_path).as_posix() for p in list_docs(tmp_path)}
    assert rels == {"docs/GUIDE.md"}


# --- Lexi drift behaviour ---------------------------------------------------

def test_ts_symbol_in_code_index_does_not_report_drift(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    _write(docs / "api.md", "The tool exposes `listConversations()` for callers.\n")
    _write(
        tmp_path / "src" / "core" / "types.ts",
        "export interface Conversation { id: string }\n"
        "export function listConversations(): Conversation[] { return []; }\n",
    )
    report = _heuristic_drift(docs_root=docs, project_root=tmp_path, config={})
    missing = [f for f in report.findings if f.kind == "missing_code"]
    assert "listConversations" not in {f.path for f in missing}, (
        "a symbol defined only in TypeScript must not be reported as missing code"
    )


def test_name_only_in_node_modules_still_reports_drift(tmp_path: Path) -> None:
    # The opposite direction: a dependency that happens to use the identifier
    # cannot silence a real drift on the project's own docs promise.
    docs = tmp_path / "docs"
    _write(docs / "api.md", "The tool exposes `phantomHelper()` for callers.\n")
    _write(
        tmp_path / "node_modules" / "some-pkg" / "index.ts",
        "export function phantomHelper() { return 1; }\n",
    )
    _write(tmp_path / "dist" / "bundle.js", "function phantomHelper() {}\n")
    report = _heuristic_drift(docs_root=docs, project_root=tmp_path, config={})
    kinds = {f.path: f.kind for f in report.findings}
    assert kinds.get("phantomHelper") == "missing_code", (
        "an identifier only inside node_modules / dist must remain a real drift"
    )


def test_promise_only_in_backup_doc_is_ignored(tmp_path: Path) -> None:
    # The docs root has only a backup copy of the promise; Lexi must not scan it,
    # so no drift finding is produced for a name the project already deleted.
    docs = tmp_path / "docs"
    _write(docs / "api.md", "# API\n\nStable surface only.\n")
    _write(docs / "api.md.bak", "The tool exposes `deletedHelper()` for callers.\n")
    _write(tmp_path / "lib.py", "VALUE = 1\n")
    report = _heuristic_drift(docs_root=docs, project_root=tmp_path, config={})
    assert "deletedHelper" not in {f.path for f in report.findings}, (
        "a name promised only in a backup doc must not create a drift finding"
    )


# The 12b live_root extra-bundle fallback was removed in 12c: the isolation copy
# now expands untracked directories file by file, so the isolated tree alone is
# the code evidence. The end-to-end replacement (untracked nested repo copied ->
# Lexi finds the symbol without live_root) lives in test_task12c_isolation.py.

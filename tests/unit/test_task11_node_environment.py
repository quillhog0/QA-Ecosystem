"""Task 11 - environment: a suite that needs an absent external tool is not a bug.

Node lives in the developer's ``~/.local/bin``; the sandbox deliberately does not
mount it (widening mounts to hide a missing tool is not acceptable). When a test
reports that an *external program it requires is not installed* - as the Quillhog
``export_public.py --dry-run`` does, by forcing ``CI=true`` and then failing
``Node.js is required ... in CI environment`` - that is an environment precondition,
not a product defect.

The classifier stays honest: it keys on the *shape* of a named tool (capitalised or
carrying a version/extension marker like ``Node.js``), never on a list of tool
names, so ordinary domain assertions ("a password is required", "a valid method is
required") and errors a project forges with ``raise`` are never excused.
"""

from __future__ import annotations

from pathlib import Path

from core.environment import classify_failure


def _cause(message: str, *, root: Path | None = None, traceback: str = ""):
    failure = {"node": "tests/test_x.py::test_case", "message": message, "traceback": traceback}
    return classify_failure(failure, project_root=root)


# --- the real nested shape, measured from the recorded run --------------------


def test_export_public_dry_run_node_required_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    message = (
        "AssertionError: export_public.py failed:\n"
        "[FAIL] Pytest failed in exported tree:\n"
        "========================= FAILURES =========================\n"
        "_______ test_f008_client_side_antidrainer_defense ________\n"
        "    node_bin = shutil.which(\"node\")\n"
        "    if node_bin:\n"
        "        ...\n"
        "    elif str(os.environ.get(\"CI\", \"\")).lower() == \"true\":\n"
        ">       pytest.fail(\"Node.js is required to execute client-side "
        "anti-drainer test suite in CI environment\")\n"
        "E   Failed: Node.js is required to execute client-side anti-drainer test "
        "suite in CI environment\n"
    )
    cause = _cause(message, root=root)
    assert cause is not None and cause.kind == "missing_tool"
    assert "node" in cause.cause


def test_direct_node_required_in_ci_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    cause = _cause("Failed: Node.js is required to build the front-end in CI", root=root)
    assert cause is not None and cause.kind == "missing_tool"


def test_capitalised_tool_not_installed_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    cause = _cause("RuntimeError: Docker is required but was not installed", root=root)
    assert cause is not None and cause.kind == "missing_tool"


# --- the guard side: ordinary domain requirements are NOT excused --------------


def test_password_required_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    cause = _cause("AssertionError: a password is required to sign in on this machine", root=root)
    assert cause is None


def test_valid_method_required_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    cause = _cause("AssertionError: a valid export method is required in CI", root=root)
    assert cause is None


def test_required_without_toolchain_cue_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    # A named tool, but nothing frames the absence as a CI/install precondition.
    cause = _cause("AssertionError: Node.js is required to be present in the response", root=root)
    assert cause is None


def test_tool_shipped_in_tree_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    (root / "node").write_text("# a launcher the project ships itself\n", encoding="utf-8")
    cause = _cause("Failed: Node.js is required to run the suite in CI", root=root)
    assert cause is None


def test_forged_raise_required_stays_product():
    # A project that deliberately raises the "required" error to hide everything
    # is refused by the same guard that stops a forged import error.
    text = (
        "tests/conftest.py:7: in <module>\n"
        '    raise AssertionError("Node.js is required in CI")\n'
        "E   AssertionError: Node.js is required in CI\n"
    )
    cause = _cause(text)
    assert cause is None

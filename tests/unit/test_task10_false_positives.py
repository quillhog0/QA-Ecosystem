"""Task 10 - one test per false-positive fix, all on synthetic input.

Nothing here reads a real repository: every failure text is copied in shape
from what pytest emits (collection ImportError chains, CalledProcessError
reprs, PermissionError lines), and every isolated root is a ``tmp_path``.
Deterministic: no network, no LLM, no bwrap.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.environment import classify_execution, classify_failure

# --- 1: PackageNotFoundError raised inside the runtime, not the project ----

# The frame that raises is importlib.metadata's own (a stdlib path); the last
# project frame merely *calls* ``importlib.metadata.version``. That is a missing
# distribution in a copy that was never installed, not a deliberate failure.
CLICK_COLLECTION_TB = (
    "ImportError while importing test module "
    "'/tmp/x/isolated_copy/tests/test_deprecations.py'.\n"
    "Hint: make sure your test modules/packages have valid Python names.\n"
    "Traceback:\n"
    "/usr/lib64/python3.14/importlib/metadata/__init__.py:407: in from_name\n"
    "    return next(iter(cls.discover(name=name)))\n"
    "E   StopIteration\n"
    "\n"
    "During handling of the above exception, another exception occurred:\n"
    "/usr/lib64/python3.14/importlib/__init__.py:88: in import_module\n"
    "    return _bootstrap._gcd_import(name[level:], package, level)\n"
    "tests/test_deprecations.py:43: in <module>\n"
    '    (click, "__version__", importlib.metadata.version("click")),\n'
    "/usr/lib64/python3.14/importlib/metadata/__init__.py:987: in version\n"
    "    return distribution(distribution_name).version\n"
    "/usr/lib64/python3.14/importlib/metadata/__init__.py:960: in distribution\n"
    "    return Distribution.from_name(distribution_name)\n"
    "/usr/lib64/python3.14/importlib/metadata/__init__.py:409: in from_name\n"
    "    raise PackageNotFoundError(name)\n"
    "E   importlib.metadata.PackageNotFoundError: No package metadata was found "
    "for click"
)


def test_1_package_not_found_at_collection_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    (root / "tests").mkdir(parents=True)
    failure = {
        "node": "tests/test_deprecations.py",
        "kind": "error",
        "message": "collection failure",
        "traceback": CLICK_COLLECTION_TB,
    }
    cause = classify_failure(failure, project_root=root)
    assert cause is not None and cause.kind == "missing_metadata"
    assert "missing package metadata: click" in cause.cause


def test_1_forged_package_not_found_stays_product(tmp_path: Path):
    # A project that raises the very same exception on purpose is still refused.
    root = tmp_path / "isolated_copy"
    (root / "tests").mkdir(parents=True)
    tb = (
        "tests/conftest.py:7: in <module>\n"
        '    raise PackageNotFoundError("No package metadata was found for click")\n'
        "E   PackageNotFoundError: No package metadata was found for click"
    )
    failure = {"node": "tests/conftest.py", "kind": "error", "message": "", "traceback": tb}
    assert classify_failure(failure, project_root=root) is None


def test_1_execution_whole_run_click_is_blocked_env(tmp_path: Path):
    # The whole-run path must agree with the per-failure path: with the failure
    # classified environmental the execution dict is an environmental stop.
    from core.environment import annotate_failures

    root = tmp_path / "isolated_copy"
    (root / "tests").mkdir(parents=True)
    failures = [{"node": "tests/test_deprecations.py", "kind": "error",
                 "message": "collection failure", "traceback": CLICK_COLLECTION_TB}]
    product, environment = annotate_failures(failures, project_root=root)
    assert (product, environment) == (0, 1)
    execution = {
        "collected": 33221,
        "passed": 0,
        "failed": 0,
        "errors": 1,
        "product_failed": product,
        "environment_failed": environment,
        "failures": failures,
        "exit_code": 2,
        "install_status": "no dependency manifest",
        "stdout": CLICK_COLLECTION_TB,
        "stderr": "",
    }
    cause = classify_execution(execution, project_root=root)
    assert cause is not None and cause.kind == "missing_metadata"


# --- 2: classify_failure environment extensions ----------------------------

GIT_CPE_TB = (
    "tests/test_versions.py:20: in get_tags\n"
    '    subprocess.check_output(["git", "ls-files"])\n'
    "subprocess.CalledProcessError: Command '['git', 'ls-files']' returned "
    "non-zero exit status 128."
)


def test_2_git_called_process_error_without_git_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    cause = classify_failure(
        {"node": "tests/test_versions.py", "message": "", "traceback": GIT_CPE_TB},
        project_root=root,
    )
    assert cause is not None and cause.kind == "missing_vcs_metadata"


def test_2_git_called_process_error_shell_string_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = (
        "tests/test_versions.py:22: in get_tags\n"
        "    _run_via_shell('git ls-files -z')\n"
        "subprocess.CalledProcessError: Command 'git ls-files -z' returned "
        "non-zero exit status 128."
    )
    cause = classify_failure({"node": "t", "message": "", "traceback": tb}, project_root=root)
    assert cause is not None and cause.kind == "missing_vcs_metadata"


def test_2_git_called_process_error_with_real_git_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    (root / ".git").mkdir(parents=True)
    cause = classify_failure(
        {"node": "tests/test_versions.py", "message": "", "traceback": GIT_CPE_TB},
        project_root=root,
    )
    assert cause is None


def test_2_git_called_process_error_needs_known_root(tmp_path: Path):
    # Without the tested tree nothing proves the copy has no .git.
    assert classify_failure({"node": "t", "message": "", "traceback": GIT_CPE_TB}) is None


def test_2_non_git_called_process_error_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = (
        "tests/test_build.py:9: in <module>\n"
        '    subprocess.check_output(["make", "all"])\n'
        "subprocess.CalledProcessError: Command '['make', 'all']' returned "
        "non-zero exit status 2."
    )
    assert classify_failure({"node": "t", "message": "", "traceback": tb}, project_root=root) is None


def test_2_forged_git_called_process_error_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = (
        "tests/conftest.py:9: in <module>\n"
        "    raise CalledProcessError(128, ['git', 'ls-files'])\n"
        "subprocess.CalledProcessError: Command '['git', 'ls-files']' returned "
        "non-zero exit status 128."
    )
    assert classify_failure({"node": "t", "message": "", "traceback": tb}, project_root=root) is None


def test_2_permission_denied_absolute_outside_root_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = (
        "worker.py:5: in acquire\n"
        '    open("/var/run/myservice.pid", "w")\n'
        "PermissionError: [Errno 13] Permission denied: '/var/run/myservice.pid'"
    )
    cause = classify_failure({"node": "worker.py", "message": "", "traceback": tb}, project_root=root)
    assert cause is not None and cause.kind == "sandbox_denied_write"
    assert "/var/run/myservice.pid" in cause.cause


def test_2_permission_denied_cwd_relative_pid_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = (
        "worker.py:5: in acquire\n"
        '    open("worker.pid", "w")\n'
        "PermissionError: [Errno 13] Permission denied: 'worker.pid'"
    )
    cause = classify_failure({"node": "worker.py", "message": "", "traceback": tb}, project_root=root)
    assert cause is not None and cause.kind == "sandbox_denied_write"


def test_2_permission_denied_inside_root_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    (root / "data").mkdir(parents=True)
    tb = (
        "app.py:8: in save\n"
        f"    open({str(root / 'data' / 'x.txt')!r}, 'w')\n"
        "PermissionError: [Errno 13] Permission denied: "
        f"'{root / 'data' / 'x.txt'}'"
    )
    assert classify_failure({"node": "app.py", "message": "", "traceback": tb}, project_root=root) is None


def test_2_permission_denied_relative_data_file_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = "PermissionError: [Errno 13] Permission denied: 'data.csv'"
    assert classify_failure({"node": "t", "message": "", "traceback": tb}, project_root=root) is None


def test_2_permission_denied_without_root_stays_product(tmp_path: Path):
    tb = "PermissionError: [Errno 13] Permission denied: '/var/run/x.pid'"
    assert classify_failure({"node": "t", "message": "", "traceback": tb}) is None


def test_2_node_not_found_launcher_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = (
        "tests/test_build.py:12: in <module>\n"
        "    _run_via_shell('node build.js')\n"
        "subprocess.CalledProcessError: Command 'node build.js' returned "
        "non-zero exit status 127.\n"
        "bash: line 1: node: not found"
    )
    cause = classify_failure({"node": "tests/test_build.py", "message": "", "traceback": tb}, project_root=root)
    assert cause is not None and cause.kind == "missing_tool"
    assert "node" in cause.cause


def test_2_node_not_found_bare_oserror_is_environment(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = "tests/test_build.py:12: in run\n    _launch()\nOSError: node not found"
    cause = classify_failure({"node": "tests/test_build.py", "message": "", "traceback": tb}, project_root=root)
    assert cause is not None and cause.kind == "missing_tool"


def test_2_not_found_in_assertion_prose_stays_product(tmp_path: Path):
    # A failed assertion that merely quotes a "not found" string accuses the
    # project; the rule must not swallow it.
    failure = {"node": "t", "message": "assert 'plugin not found' in output", "traceback": ""}
    assert classify_failure(failure) is None


def test_2_error_class_message_not_found_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    tb = "tests/test_registry.py:9: in lookup\n    _resolve()\nRuntimeError: handler not found"
    assert classify_failure({"node": "t", "message": "", "traceback": tb}, project_root=root) is None


def test_2_program_named_like_project_file_stays_product(tmp_path: Path):
    root = tmp_path / "isolated_copy"
    root.mkdir()
    (root / "node").write_text("# launcher script the project ships\n", encoding="utf-8")
    tb = "bash: line 1: node: not found"
    assert classify_failure({"node": "t", "message": "", "traceback": tb}, project_root=root) is None

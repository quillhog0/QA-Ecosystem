from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.packets import Verdict
from policies.sable import (
    evaluate_preflight,
    valid_allow_entries,
    scan_tests_for_risks,
    _all_python_files,
    _arg_looks_tmp,
    _command_is_pytest_only,
    install_runtime_guards,
)


def _pytest_cmd(target: Path) -> list[str]:
    return [sys.executable, "-m", "pytest", str(target), "-q"]


def test_all_python_files_returns_empty_list_for_non_directory(tmp_path: Path):
    """sable.py:251 - a missing/non-directory root yields an empty list, not None
    (callers iterate the result)."""
    assert _all_python_files(tmp_path / "does_not_exist") == []


def test_sable_blocks_rm_rf(tmp_path: Path):
    iso = tmp_path / "iso"
    iso.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    decision = evaluate_preflight(
        command=["rm", "-rf", "/tmp/x"],
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
    )
    assert decision.verdict == Verdict.BLOCK.value


def test_sable_blocks_shutil_rmtree_in_command(tmp_path: Path):
    iso = tmp_path / "iso"
    iso.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    cmd = _pytest_cmd(iso) + ["shutil.rmtree"]
    decision = evaluate_preflight(
        command=cmd,
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
    )
    assert decision.verdict == Verdict.BLOCK.value


@pytest.mark.parametrize("binary", ["curl", "wget"])
def test_sable_blocks_network_binaries(tmp_path: Path, binary: str):
    iso = tmp_path / "iso"
    iso.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    decision = evaluate_preflight(
        command=[binary, "https://example.invalid"],
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
    )
    assert decision.verdict == Verdict.BLOCK.value


def test_sable_blocks_secret_env(tmp_path: Path):
    iso = tmp_path / "iso"
    iso.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    decision = evaluate_preflight(
        command=_pytest_cmd(iso),
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={"MY_TOKEN": "abc", "PATH": "/usr/bin"},
    )
    assert decision.verdict == Verdict.BLOCK.value
    assert any("TOKEN" in r.upper() or "secret" in r.lower() for r in decision.blocked_reasons)


def test_sable_blocks_dangerous_ast_in_tests(tmp_path: Path):
    iso = tmp_path / "iso"
    live = tmp_path / "live"
    iso.mkdir()
    live.mkdir()
    tests = iso / "tests"
    tests.mkdir()
    (tests / "test_bad.py").write_text(
        "import shutil\nimport urllib.request\n"
        "def test_x():\n    shutil.rmtree('scratch_dir')\n",
        encoding="utf-8",
    )
    decision = evaluate_preflight(
        command=_pytest_cmd(iso),
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
    )
    assert decision.verdict == Verdict.BLOCK.value
    joined = " ".join(decision.blocked_reasons)
    assert "rmtree" in joined or "urllib" in joined


def _tree_with_subprocess_run(tmp_path: Path) -> tuple[Path, Path]:
    iso = tmp_path / "iso"
    live = tmp_path / "live"
    iso.mkdir()
    live.mkdir()
    tests = iso / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_isolation.py").write_text(
        "import subprocess\n\n"
        "def test_x():\n"
        "    subprocess.run(['true'])\n",
        encoding="utf-8",
    )
    return iso, live


def test_sable_allow_in_tests_requires_path_call_and_reason(tmp_path: Path):
    iso, live = _tree_with_subprocess_run(tmp_path)
    cmd = _pytest_cmd(iso)
    complete = {
        "path": "tests/unit/test_isolation.py",
        "call": "subprocess.run",
        "reason": "git worktree fixture",
    }
    ok = evaluate_preflight(
        command=cmd,
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
        allow_in_tests=[complete],
    )
    assert ok.verdict == Verdict.CLEAR.value
    assert ok.allowed
    assert all(item.get("status") == "ALLOWED" for item in ok.allowed)
    assert any(
        item.get("path") == "tests/unit/test_isolation.py"
        and item.get("call") == "subprocess.run"
        and item.get("reason") == "git worktree fixture"
        for item in ok.allowed
    )

    assert valid_allow_entries([{**complete, "reason": ""}]) == []
    assert valid_allow_entries([{**complete, "path": ""}]) == []
    assert valid_allow_entries([{**complete, "call": ""}]) == []

    missing_reason = evaluate_preflight(
        command=cmd,
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
        allow_in_tests=[{k: v for k, v in complete.items() if k != "reason"}],
    )
    assert missing_reason.verdict == Verdict.BLOCK.value

    wrong_path = evaluate_preflight(
        command=cmd,
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
        allow_in_tests=[{**complete, "path": "tests/unit/other.py"}],
    )
    assert wrong_path.verdict == Verdict.BLOCK.value

    wrong_call = evaluate_preflight(
        command=cmd,
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
        allow_in_tests=[{**complete, "call": "subprocess.Popen"}],
    )
    assert wrong_call.verdict == Verdict.BLOCK.value


def test_sable_substring_nc_in_path_is_false_positive(tmp_path: Path):
    iso = tmp_path / "bench_copy"
    iso.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    decision = evaluate_preflight(
        command=_pytest_cmd(iso),
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
    )
    assert decision.verdict == Verdict.CLEAR.value
    assert decision.checks.get("network_denied") is True


def test_sable_blocks_command_outside_isolated_root(tmp_path: Path):
    iso = tmp_path / "iso"
    iso.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    decision = evaluate_preflight(
        command=_pytest_cmd(elsewhere),
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
    )
    assert decision.verdict == Verdict.BLOCK.value
    assert any("outside isolated_root" in r for r in decision.blocked_reasons)


def test_sable_blocks_rootdir_and_cwd_outside_isolated_root(tmp_path: Path):
    iso = tmp_path / "iso"
    iso.mkdir()
    live = tmp_path / "live"
    live.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    cmd = [sys.executable, "-m", "pytest", str(iso), "--rootdir", str(elsewhere)]
    decision = evaluate_preflight(
        command=cmd,
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
    )
    assert decision.verdict == Verdict.BLOCK.value
    cmd_eq = [sys.executable, "-m", "pytest", str(iso), f"--rootdir={elsewhere}"]
    decision_eq = evaluate_preflight(
        command=cmd_eq,
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
    )
    assert decision_eq.verdict == Verdict.BLOCK.value
    cwd_decision = evaluate_preflight(
        command=_pytest_cmd(iso),
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=elsewhere,
    )
    assert cwd_decision.verdict == Verdict.BLOCK.value


def test_sable_scans_conftest_py(tmp_path: Path):
    iso = tmp_path / "iso"
    live = tmp_path / "live"
    iso.mkdir()
    live.mkdir()
    (iso / "conftest.py").write_text("import subprocess\n", encoding="utf-8")
    decision = evaluate_preflight(
        command=_pytest_cmd(iso),
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=iso,
    )
    assert decision.verdict == Verdict.BLOCK.value
    assert any("subprocess" in r for r in decision.blocked_reasons)


def test_sable_arg_looks_tmp_checks_segments_not_substring(tmp_path: Path):
    iso = tmp_path / "iso"
    iso.mkdir()
    # Call on template.py must NOT be treated as tmp
    (iso / "test_tmp.py").write_text(
        "import os\n\ndef test_fn():\n    os.remove('template.py')\n",
        encoding="utf-8",
    )
    hits, _ = scan_tests_for_risks(iso)
    assert any("os.remove" in h for h in hits)

    # Call on /tmp/foo.txt is treated as tmp and allowed
    (iso / "test_tmp.py").write_text(
        "import os\n\ndef test_fn():\n    os.remove('/tmp/foo.txt')\n",
        encoding="utf-8",
    )
    hits, _ = scan_tests_for_risks(iso)
    assert not any("os.remove" in h for h in hits)


def test_sable_runtime_guard_blocks_socket_only_when_no_network(tmp_path: Path):
    import ast
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    guard = install_runtime_guards(work_dir)
    assert guard.is_file()
    assert guard.name == "_sable_guard.py"

    content = guard.read_text(encoding="utf-8")
    assert 'os.environ.get("NO_NETWORK") == "1"' in content
    assert "socket.socket.connect = _blocked" in content
    assert "Sable: socket.connect is blocked in the isolated copy" in content

    tree = ast.parse(content)
    assert any(
        isinstance(node, ast.FunctionDef) and node.name == "_blocked"
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize(
    "code,expected_token",
    [
        ("from shutil import rmtree\n", "from shutil import rmtree"),
        ("from os import system\n", "from os import system"),
        ("from os import popen\n", "from os import popen"),
        ("from os import remove\n", "from os import remove"),
        ("from os import unlink\n", "from os import unlink"),
        ("def test_x(): eval('1+1')\n", "call eval"),
        ("def test_x(): exec('x=1')\n", "call exec"),
        ("def test_x(): __import__('os')\n", "call __import__"),
    ],
)
def test_sable_blocks_direct_imports_and_eval_exec(tmp_path: Path, code: str, expected_token: str):
    iso = tmp_path / "iso"
    iso.mkdir(exist_ok=True)
    test_file = iso / "test_sample.py"
    test_file.write_text(code, encoding="utf-8")
    hits, _ = scan_tests_for_risks(iso)
    assert any(expected_token in h for h in hits), f"Expected {expected_token} in hits: {hits}"


def test_sable_scans_project_files_and_nested_conftest(tmp_path: Path):
    iso = tmp_path / "iso"
    iso.mkdir(exist_ok=True)
    (iso / "src").mkdir(exist_ok=True)
    (iso / "src" / "worker.py").write_text("import os\ndef run(): eval('2+2')\n", encoding="utf-8")
    (iso / "tests" / "nested").mkdir(parents=True, exist_ok=True)
    (iso / "tests" / "nested" / "conftest.py").write_text("import shutil\nshutil.rmtree('/tmp/evil')\n", encoding="utf-8")

    hits, _ = scan_tests_for_risks(iso)
    assert any("src/worker.py: call eval" in h for h in hits)
    assert any("nested/conftest.py: call shutil.rmtree" in h for h in hits)


def test_command_is_pytest_only_accepts_pytest_invocations():
    """Plain pytest and python -m pytest forms pass, including absolute interpreter paths."""
    assert _command_is_pytest_only(["pytest", "tests/"]) == (True, "pytest-only")
    assert _command_is_pytest_only(["python3", "-m", "pytest", "tests"]) == (True, "pytest-only")
    assert _command_is_pytest_only(["/usr/bin/python", "-m", "pytest", "-q"]) == (True, "pytest-only")


def test_command_is_pytest_only_rejects_empty_and_non_pytest_commands():
    assert _command_is_pytest_only([]) == (False, "empty command")
    assert _command_is_pytest_only(["ls", "-la"]) == (False, "command is not pytest")
    assert _command_is_pytest_only(["python", "-m", "unittest"]) == (False, "command is not pytest")


def test_command_is_pytest_only_rejects_forbidden_network_and_destructive_tokens():
    assert _command_is_pytest_only(["pytest", "rm", "file"]) == (False, "forbidden token: rm")
    assert _command_is_pytest_only(["pytest", "nc"]) == (False, "network binary: nc")
    ok, reason = _command_is_pytest_only(["python -m pytest ; rm -rf /"])
    assert ok is False
    assert reason.startswith("destructive pattern in command:")



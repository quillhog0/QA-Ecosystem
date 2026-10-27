from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.packets import Verdict
from policies.sable import evaluate_preflight, valid_allow_entries


def _pytest_cmd(target: Path) -> list[str]:
    return [sys.executable, "-m", "pytest", str(target), "-q"]


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
    cwd_decision = evaluate_preflight(
        command=_pytest_cmd(iso),
        isolated_root=iso,
        live_root=live,
        allow_network=False,
        env={},
        cwd=elsewhere,
    )
    assert cwd_decision.verdict == Verdict.BLOCK.value

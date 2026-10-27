from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.isolation import (
    create_isolation,
    is_secret_path,
    isolation_destination,
    release_isolation,
)
from tests.support import is_readonly_file, mini_project, tree_sha256


@pytest.mark.parametrize(
    "path,secret",
    [
        (Path(".env"), True),
        (Path("subdir/.env"), True),
        (Path("cert.pem"), True),
        (Path("id_rsa"), True),
        (Path("id_rsa.pub"), True),
        (Path("credentials.json"), True),
        (Path("calculator.py"), False),
        (Path("readme.md"), False),
        (Path("tests/test_calculator.py"), False),
    ],
)
def test_is_secret_path(path: Path, secret: bool):
    assert is_secret_path(path) is secret


def test_copytree_skips_secrets_readonly_and_preserves_live_hash(tmp_path: Path):
    live = mini_project(tmp_path / "live", secret=True)
    (live / "ok.txt").write_text("plain", encoding="utf-8")
    before = tree_sha256(live)
    dest = tmp_path / "iso"
    handle = create_isolation(live, dest, method="copy")
    after = tree_sha256(live)
    assert after == before
    assert not (dest / ".env").exists()
    assert ".env" in handle.skipped_secrets
    assert (dest / "calculator.py").is_file()
    assert is_readonly_file(dest / "calculator.py")
    assert handle.method == "copy"


def test_isolation_destination_avoids_nested_reports(tmp_path: Path):
    live = tmp_path / "proj"
    live.mkdir()
    reports = live / "reports"
    dest = isolation_destination(live_root=live, reports_dir=reports, run_id="r1")
    assert "reports" not in dest.resolve().parts or not str(dest.resolve()).startswith(
        str(live.resolve())
    )
    try:
        dest.resolve().relative_to(live.resolve() / "reports")
        nested_in_reports = True
    except ValueError:
        nested_in_reports = False
    assert nested_in_reports is False


def test_isolation_destination_with_custom_reports_dir(tmp_path: Path):
    live = tmp_path / "proj"
    custom_reports = tmp_path / "custom_reports_dir"
    dest = isolation_destination(live_root=live, reports_dir=custom_reports, run_id="r_custom")
    assert str(dest.resolve()).startswith("/tmp/qa-agent-ecosystem-isolations/")


def test_git_worktree_leak(tmp_path: Path):
    live = mini_project(tmp_path / "repo")
    subprocess.run(["git", "init"], cwd=live, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=live, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@test",
            "commit",
            "-m",
            "init",
        ],
        cwd=live,
        check=True,
        capture_output=True,
    )
    dest = tmp_path / "worktree-iso"
    before_list = subprocess.run(
        ["git", "-C", str(live), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    before = tree_sha256(live)
    handle = None
    try:
        handle = create_isolation(live, dest)
        after = tree_sha256(live)
        assert after == before
    finally:
        release_isolation(handle)
    after_list = subprocess.run(
        ["git", "-C", str(live), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after_list == before_list
    assert str(dest.resolve()) not in after_list

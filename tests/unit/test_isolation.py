from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.isolation import (
    IsolationHandle,
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
        (Path(".env.staging"), True),
        (Path(".env.production"), True),
        (Path(".env.test"), True),
        (Path(".netrc"), True),
        (Path(".npmrc"), True),
        (Path(".pypirc"), True),
        (Path("kubeconfig"), True),
        (Path(".env.example"), False),
        (Path(".env.sample"), False),
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
    dest = isolation_destination(live_root=live, reports_dir=live / "reports", run_id="worktree_leak")
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


def test_isolation_secrets_and_symlinks(tmp_path: Path):
    live = mini_project(tmp_path / "live")
    (live / ".env.staging").write_text("SECRET=1", encoding="utf-8")
    (live / ".netrc").write_text("password 123", encoding="utf-8")
    (live / ".env.example").write_text("EXAMPLE=1", encoding="utf-8")

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("outside data", encoding="utf-8")
    outside_mode_before = outside_file.stat().st_mode

    symlink_file = live / "link_outside.py"
    symlink_file.symlink_to(outside_file)

    dest = tmp_path / "iso"
    handle = create_isolation(live, dest, method="copy")

    assert not (dest / ".env.staging").exists()
    assert not (dest / ".netrc").exists()
    assert (dest / ".env.example").is_file()
    assert not (dest / "link_outside.py").exists()
    assert outside_file.stat().st_mode == outside_mode_before


def test_locked_tree_prevents_file_creation_and_deletion(tmp_path: Path):
    live = mini_project(tmp_path / "live")
    dest = tmp_path / "iso"
    create_isolation(live, dest, method="copy", read_only=True)

    calc = dest / "calculator.py"
    assert calc.is_file()

    with pytest.raises(OSError):
        calc.unlink()

    with pytest.raises(OSError):
        (dest / "new_file.py").write_text("bad", encoding="utf-8")


def test_release_isolation_deletes_copy_and_rejects_outside_path(tmp_path: Path):
    live = mini_project(tmp_path / "live")
    iso_dest = isolation_destination(live_root=live, run_id="test_cleanup_run")
    handle = create_isolation(live, iso_dest, method="copy")
    assert iso_dest.is_dir()

    release_isolation(handle, keep=False)
    assert not iso_dest.exists()

    outside_dest = tmp_path / "outside_dir"
    outside_dest.mkdir()
    outside_handle = IsolationHandle(
        live_root=live,
        isolated_root=outside_dest,
        method="copy",
        read_only=True,
        skipped_secrets=[],
    )
    with pytest.raises(ValueError, match="outside isolations root"):
        release_isolation(outside_handle, keep=False)


def test_isolation_copies_uncommitted_working_tree(tmp_path: Path):
    live = mini_project(tmp_path / "repo")
    subprocess.run(["git", "init"], cwd=live, check=True, capture_output=True)
    subprocess.run(["git", "-C", str(live), "config", "user.name", "test"], check=True)
    subprocess.run(["git", "-C", str(live), "config", "user.email", "test@test"], check=True)
    subprocess.run(["git", "-C", str(live), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(live), "commit", "-m", "init"], check=True, capture_output=True)

    (live / "calculator.py").write_text("def add(a, b): return a - b\n", encoding="utf-8")
    (live / "untracked.py").write_text("UNTRACKED = True\n", encoding="utf-8")

    dest = isolation_destination(live_root=live, run_id="test_uncommitted")
    handle = None
    try:
        handle = create_isolation(live, dest)
        assert handle.method == "git-working-tree"
        assert "working (HEAD" in handle.tested_tree
        assert "2 uncommitted files)" in handle.tested_tree
        assert (dest / "calculator.py").read_text(encoding="utf-8") == "def add(a, b): return a - b\n"
        assert (dest / "untracked.py").is_file()
    finally:
        release_isolation(handle, keep=False)


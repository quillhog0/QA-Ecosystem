from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.isolation import (
    IsolationHandle,
    _copy_tree,
    _get_tested_tree_info,
    _is_git_toplevel,
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


def test_release_isolation_git_worktree_captures_output(tmp_path: Path):
    from unittest.mock import patch

    live = tmp_path / "live"
    live.mkdir()
    iso_dest = isolation_destination(live_root=live, run_id="test_wt_release")
    handle = IsolationHandle(
        live_root=live,
        isolated_root=iso_dest,
        method="git-worktree",
        read_only=True,
        skipped_secrets=[],
        git_worktree=True,
    )
    with patch("core.isolation.subprocess.run") as mock_run:
        release_isolation(handle, keep=False)

    assert mock_run.call_count == 2
    remove_call, prune_call = mock_run.call_args_list
    assert "remove" in remove_call.args[0]
    assert remove_call.kwargs.get("capture_output") is True
    assert remove_call.kwargs.get("text") is True
    assert "prune" in prune_call.args[0]
    assert prune_call.kwargs.get("capture_output") is True
    assert prune_call.kwargs.get("text") is True


def test_is_git_toplevel_false_when_git_binary_missing(tmp_path: Path, monkeypatch):
    """isolation.py:80 - the OSError fallback must return exactly False."""
    def fake_run(*args, **kwargs):
        raise OSError("git not installed")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _is_git_toplevel(tmp_path) is False


def test_is_git_toplevel_false_when_toplevel_resolve_fails(tmp_path: Path, monkeypatch):
    """isolation.py:87 - an unresolvable toplevel path must return exactly False."""
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=str(tmp_path), stderr="")

    def boom(self, *args, **kwargs):
        raise OSError("cannot resolve")

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(Path, "resolve", boom)
    assert _is_git_toplevel(tmp_path) is False


def test_tested_tree_info_head_lookup_runs_unchecked(tmp_path: Path, monkeypatch):
    """isolation.py:97 - rev-parse HEAD must use check=False: an unborn HEAD
    yields 'unknown' instead of escaping as CalledProcessError."""
    Err = subprocess.CalledProcessError
    seen_checks: list[object] = []

    def fake_run(cmd, **kwargs):
        check = kwargs.get("check")
        seen_checks.append(check)
        joined = " ".join(str(part) for part in cmd)
        if "--show-toplevel" in joined:
            return SimpleNamespace(returncode=0, stdout=str(tmp_path), stderr="")
        if "--short" in joined:
            if check:
                raise Err(returncode=128, cmd=cmd, stderr="fatal: ambiguous argument 'HEAD'")
            return SimpleNamespace(returncode=128, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    info = _get_tested_tree_info(tmp_path)
    assert info == "working (HEAD unknown + 0 uncommitted files)"
    assert False in seen_checks


def test_tested_tree_info_non_git_project_is_plain_copy(tmp_path: Path, monkeypatch):
    """isolation.py:110 - non-git projects must report the exact copy label."""
    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _get_tested_tree_info(tmp_path) == "working (copy)"


def test_copy_tree_preserves_internal_symlinks(tmp_path: Path):
    """isolation.py:202 - copytree must run with symlinks=True (links stay links)."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "real.txt").write_text("payload", encoding="utf-8")
    os.symlink("real.txt", src / "link.txt")
    dst = tmp_path / "dst"
    _copy_tree(src, dst)
    assert (dst / "link.txt").is_symlink()
    assert (dst / "real.txt").read_text(encoding="utf-8") == "payload"


def test_clean_symlinks_removes_link_pointing_to_in_copy_secret(tmp_path: Path):
    """isolation.py:168 - a symlink whose resolved target is a secret file inside
    the copy must be removed even when the link's own name is not secret."""
    from core.isolation import _clean_symlinks_and_secrets

    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / ".env").write_text("SECRET=x\n", encoding="utf-8")
    os.symlink(".env", dest / "notes.txt")  # link name is benign; target is secret

    skipped: list[str] = []
    _clean_symlinks_and_secrets(dest, skipped)

    assert not (dest / "notes.txt").exists()
    assert "notes.txt" in skipped


def test_copy_tree_refuses_existing_destination(tmp_path: Path):
    """isolation.py:202 - dirs_exist_ok must stay False: never merge into an existing copy."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("x", encoding="utf-8")
    dst = tmp_path / "dst"
    dst.mkdir()
    with pytest.raises(FileExistsError):
        _copy_tree(src, dst)



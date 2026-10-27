"""Workspace isolation: copy or git worktree. Never mutates the live tree."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "dist",
    "build",
    ".eggs",
    ".idea",
    ".cursor",
    "reports",
}

SECRET_NAME_MARKERS = (
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.json",
    "id_rsa",
    "id_ed25519",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)


@dataclass
class IsolationHandle:
    live_root: Path
    isolated_root: Path
    method: str
    read_only: bool
    skipped_secrets: list[str]
    git_worktree: bool = False

    def to_dict(self) -> dict:
        return {
            "live_root": str(self.live_root),
            "isolated_root": str(self.isolated_root),
            "method": self.method,
            "read_only": self.read_only,
            "skipped_secrets": self.skipped_secrets,
            "git_worktree": self.git_worktree,
        }


def is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name in {".env", ".env.local", ".env.production", ".env.development"}:
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    if name in {"credentials.json", "secrets.json", "service-account.json"}:
        return True
    if name.startswith("id_rsa") or name.startswith("id_ed25519"):
        return True
    return False


def _is_git_toplevel(root: Path) -> bool:
    """True only when root is the repository root. Nested dirs are copied instead."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    if proc.returncode != 0:
        return False
    top = Path(proc.stdout.strip())
    try:
        return top.resolve() == root.resolve()
    except OSError:
        return False


def _copy_tree(src: Path, dst: Path) -> list[str]:
    skipped: list[str] = []

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        base = Path(directory)
        for name in names:
            child = base / name
            if name in SKIP_DIR_NAMES:
                ignored.add(name)
                continue
            if is_secret_path(child):
                ignored.add(name)
                skipped.append(str(child.relative_to(src)))
        return ignored

    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=False, symlinks=False)
    return skipped


def _make_sources_readonly(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.exists() or path.is_symlink():
            continue
        mode = path.stat().st_mode
        if path.is_dir():
            os.chmod(path, mode | stat.S_IRUSR | stat.S_IXUSR)
            continue
        # Keep sources readable; strip write bits.
        os.chmod(path, mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)


def isolation_destination(
    *,
    live_root: Path,
    reports_dir: Path | None = None,
    run_id: str,
) -> Path:
    """Return an isolated copy path under /tmp/qa-agent-ecosystem-isolations/."""
    base = Path(tempfile.gettempdir()) / "qa-agent-ecosystem-isolations" / run_id
    return base / "isolated_copy"


def create_isolation(
    live_root: Path,
    dest: Path,
    *,
    method: str | None = None,
) -> IsolationHandle:
    """Create an isolated copy. Prefer detached git worktree; else copytree.

    The live working tree is never checked out or modified.
    Secret-looking files are not copied.
    """
    live_root = live_root.resolve()
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"isolation destination already exists: {dest}")

    skipped: list[str] = []
    requested = (method or "auto").lower()
    method = "copy"
    git_worktree = False

    if requested != "copy" and _is_git_toplevel(live_root):
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(live_root),
                    "worktree",
                    "add",
                    "--detach",
                    str(dest),
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and dest.exists():
                method = "git-worktree"
                git_worktree = True
                # Remove secret files from the worktree copy (do not touch live tree).
                for path in list(dest.rglob("*")):
                    if path.is_file() and is_secret_path(path):
                        rel = str(path.relative_to(dest))
                        skipped.append(rel)
                        path.chmod(stat.S_IWUSR | stat.S_IRUSR)
                        path.unlink()
            else:
                dest_partial = dest if dest.exists() else None
                if dest_partial:
                    shutil.rmtree(dest, ignore_errors=True)
        except OSError:
            git_worktree = False
            method = "copy"

    if method == "copy" and not dest.exists():
        skipped = _copy_tree(live_root, dest)

    _make_sources_readonly(dest)
    return IsolationHandle(
        live_root=live_root,
        isolated_root=dest,
        method=method,
        read_only=True,
        skipped_secrets=skipped,
        git_worktree=git_worktree,
    )


def release_isolation(handle: IsolationHandle | None) -> None:
    """Detach a git worktree created for this run. Never touches the live files."""
    if handle is None or not handle.git_worktree:
        return
    live = str(handle.live_root)
    dest = str(handle.isolated_root)
    try:
        subprocess.run(
            ["git", "-C", live, "worktree", "remove", "--force", dest],
            capture_output=True,
            text=True,
            check=False,
        )
        subprocess.run(
            ["git", "-C", live, "worktree", "prune"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return


def archive_note(handle: IsolationHandle, log_dir: Path) -> Path:
    """Record isolation metadata. Does not delete the live tree or secrets."""
    log_dir.mkdir(parents=True, exist_ok=True)
    note = log_dir / "isolation.txt"
    lines = [
        f"method={handle.method}",
        f"live_root={handle.live_root}",
        f"isolated_root={handle.isolated_root}",
        f"read_only={handle.read_only}",
        f"git_worktree={handle.git_worktree}",
        "skipped_secrets:",
    ]
    if handle.skipped_secrets:
        lines.extend(f"  - {item}" for item in handle.skipped_secrets)
    else:
        lines.append("  - (none)")
    note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return note

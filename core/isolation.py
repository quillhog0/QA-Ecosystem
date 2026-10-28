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

@dataclass
class IsolationHandle:
    live_root: Path
    isolated_root: Path
    method: str
    read_only: bool
    skipped_secrets: list[str]
    git_worktree: bool = False
    tested_tree: str = ""

    def to_dict(self) -> dict:
        return {
            "live_root": str(self.live_root),
            "isolated_root": str(self.isolated_root),
            "method": self.method,
            "read_only": self.read_only,
            "skipped_secrets": self.skipped_secrets,
            "git_worktree": self.git_worktree,
            "tested_tree": self.tested_tree,
        }


def is_secret_path(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample"}):
        return True
    if name in {".netrc", ".npmrc", ".pypirc", "kubeconfig"}:
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    if name in {"credentials.json", "secrets.json", "service-account.json"}:
        return True
    if name.startswith("id_rsa") or name.startswith("id_ed25519"):
        return True
    return False


def _is_git_toplevel(root: Path) -> bool:
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


def _get_tested_tree_info(root: Path) -> str:
    if _is_git_toplevel(root):
        try:
            proc_hash = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            head_hash = proc_hash.stdout.strip() or "unknown"
            proc_status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
            )
            uncommitted = [l for l in proc_status.stdout.splitlines() if l.strip()]
            return f"working (HEAD {head_hash} + {len(uncommitted)} uncommitted files)"
        except OSError:
            pass
    return "working (copy)"


def _copy_git_working_tree(src: Path, dst: Path) -> list[str]:
    skipped: list[str] = []
    proc = subprocess.run(
        ["git", "-C", str(src), "ls-files", "-co", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return _copy_tree(src, dst)

    dst.mkdir(parents=True, exist_ok=True)
    for entry in proc.stdout.split(b"\0"):
        if not entry:
            continue
        try:
            rel_str = entry.decode("utf-8")
        except UnicodeDecodeError:
            rel_str = entry.decode("utf-8", errors="replace")
        rel_path = Path(rel_str)
        if any(part in SKIP_DIR_NAMES for part in rel_path.parts):
            continue
        src_file = src / rel_path
        if not src_file.exists() and not src_file.is_symlink():
            continue
        if is_secret_path(src_file):
            skipped.append(str(rel_path))
            continue
        dst_file = dst / rel_path
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if src_file.is_symlink():
            try:
                target = os.readlink(src_file)
                os.symlink(target, dst_file)
            except OSError:
                pass
        else:
            try:
                shutil.copy2(src_file, dst_file, follow_symlinks=False)
            except OSError:
                pass

    _clean_symlinks_and_secrets(dst, skipped)
    return skipped


def _clean_symlinks_and_secrets(dest: Path, skipped: list[str]) -> None:
    dest_resolved = dest.resolve()
    for path in list(dest.rglob("*")):
        if path.is_symlink():
            should_remove = False
            try:
                target = path.resolve()
                if not target.is_relative_to(dest_resolved):
                    should_remove = True
                elif is_secret_path(target):
                    should_remove = True
            except OSError:
                should_remove = True
            if is_secret_path(path):
                should_remove = True
            if should_remove:
                rel = str(path.relative_to(dest))
                if rel not in skipped:
                    skipped.append(rel)
                path.unlink()
        elif path.is_file() and is_secret_path(path):
            rel = str(path.relative_to(dest))
            if rel not in skipped:
                skipped.append(rel)
            path.chmod(stat.S_IWUSR | stat.S_IRUSR)
            path.unlink()


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

    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=False, symlinks=True)
    _clean_symlinks_and_secrets(dst, skipped)
    return skipped


def _make_sources_readonly(root: Path) -> None:
    for path in root.rglob("*"):
        if not path.exists() or path.is_symlink():
            continue
        if path.is_file():
            mode = path.stat().st_mode
            os.chmod(path, mode & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)

    dirs = [d for d in root.rglob("*") if d.is_dir() and not d.is_symlink()]
    dirs.sort(key=lambda p: len(p.parts), reverse=True)
    dirs.append(root)
    for d in dirs:
        mode = d.stat().st_mode
        os.chmod(d, (mode | stat.S_IRUSR | stat.S_IXUSR) & ~stat.S_IWUSR & ~stat.S_IWGRP & ~stat.S_IWOTH)


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
    read_only: bool = True,
) -> IsolationHandle:
    """Create an isolated copy of the working tree including uncommitted changes.

    The live working tree is never checked out or modified.
    Secret-looking files are not copied.
    """
    live_root = live_root.resolve()
    dest = dest.resolve()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise FileExistsError(f"isolation destination already exists: {dest}")

    requested = (method or "auto").lower()
    method = "copy"
    git_worktree = False
    tested_tree = _get_tested_tree_info(live_root)

    if requested != "copy" and _is_git_toplevel(live_root):
        method = "git-working-tree"
        skipped = _copy_git_working_tree(live_root, dest)
    else:
        skipped = _copy_tree(live_root, dest)

    if read_only:
        _make_sources_readonly(dest)

    return IsolationHandle(
        live_root=live_root,
        isolated_root=dest,
        method=method,
        read_only=read_only,
        skipped_secrets=skipped,
        git_worktree=git_worktree,
        tested_tree=tested_tree,
    )


def _validate_isolation_path(path: Path) -> Path:
    isolations_root = Path(tempfile.gettempdir()).resolve() / "qa-agent-ecosystem-isolations"
    resolved = path.resolve()
    try:
        if not resolved.is_relative_to(isolations_root) or resolved == isolations_root:
            raise ValueError(f"Refusing to delete path outside isolations root: {resolved}")
    except AttributeError:
        if isolations_root not in resolved.parents or resolved == isolations_root:
            raise ValueError(f"Refusing to delete path outside isolations root: {resolved}")
    return resolved


def _force_rmtree(path: Path) -> None:
    path = _validate_isolation_path(path)
    if not path.exists():
        return

    def _on_error(func, p, exc_info):
        try:
            target = Path(p)
            if target.exists():
                os.chmod(target, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            if target.parent.exists():
                os.chmod(target.parent, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
            func(p)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onexc=lambda fn, p, exc: _on_error(fn, p, exc))
    except TypeError:
        shutil.rmtree(path, onerror=_on_error)


def release_isolation(
    handle: IsolationHandle | None,
    *,
    keep: bool = False,
) -> None:
    """Detach a git worktree or remove isolated copy for this run."""
    if handle is None:
        return
    if keep:
        return

    isolations_root = Path(tempfile.gettempdir()).resolve() / "qa-agent-ecosystem-isolations"
    dest = handle.isolated_root.resolve()

    if handle.git_worktree:
        live = str(handle.live_root)
        try:
            subprocess.run(
                ["git", "-C", live, "worktree", "remove", "--force", str(dest)],
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
            pass
    else:
        _validate_isolation_path(dest)
        _force_rmtree(dest)

    parent = dest.parent
    try:
        if parent.exists() and parent.is_relative_to(isolations_root) and parent != isolations_root:
            parent.rmdir()
    except OSError:
        pass


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

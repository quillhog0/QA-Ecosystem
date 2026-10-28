"""Bubblewrap (bwrap) sandbox for isolated test execution.

Wraps execution in a Linux container with:
- unshared network, PID, IPC, mount, UTS namespaces (--unshare-all)
- read-only system mounts (/usr, /etc, /bin, /lib, /lib64)
- isolated tmpfs for /tmp and read-only /home (no access to host $HOME)
- read-write mounts only for isolated project copy and run work directory
- process killed automatically with parent (--die-with-parent)
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any


def is_bwrap_available() -> bool:
    """Return True if running on Linux and bubblewrap (bwrap) executable is available."""
    if not sys.platform.startswith("linux"):
        return False
    return shutil.which("bwrap") is not None


def get_bwrap_path() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    return shutil.which("bwrap")


def resolve_sandbox_mode(config: dict[str, Any] | None) -> str:
    """Resolve sandbox mode: auto (default), required, or off."""
    if not config:
        return "auto"
    sb = config.get("sandbox")
    if isinstance(sb, dict):
        mode = str(sb.get("mode") or "auto").strip().lower()
    elif isinstance(sb, str):
        mode = sb.strip().lower()
    else:
        mode = "auto"
    if mode not in {"auto", "required", "off"}:
        return "auto"
    return mode


def get_sandbox_status(config: dict[str, Any] | None) -> tuple[bool, str, str | None]:
    """Return (enabled, label, failure_reason).

    Labels:
    - 'bwrap': bwrap sandbox is active
    - 'unavailable': bwrap is not available / not Linux
    - 'off': sandbox explicitly disabled
    """
    mode = resolve_sandbox_mode(config)
    if mode == "off":
        return False, "off", None

    available = is_bwrap_available()
    if available:
        return True, "bwrap", None

    if mode == "required":
        return (
            False,
            "unavailable",
            "sandbox mode is required but bubblewrap (bwrap) is not available or not on Linux",
        )
    return False, "unavailable", None


def wrap_command(
    cmd: list[str],
    *,
    isolated_root: Path,
    work_dir: Path,
    python: Path | None = None,
    allow_network: bool = False,
    bwrap_path: str | None = None,
) -> list[str]:
    """Wrap command in bwrap sandbox arguments."""
    bwrap_bin = bwrap_path or get_bwrap_path() or "bwrap"
    iso = isolated_root.resolve()
    work = work_dir.resolve()

    args = [
        bwrap_bin,
        "--ro-bind", "/usr", "/usr",
    ]

    # Standard system binary/library paths or symlinks
    for name in ["bin", "lib", "lib64", "sbin"]:
        p = Path(f"/{name}")
        if p.is_symlink():
            try:
                target = os.readlink(p)
                args.extend(["--symlink", target, f"/{name}"])
            except OSError:
                args.extend(["--symlink", f"usr/{name}", f"/{name}"])
        elif p.is_dir():
            args.extend(["--ro-bind-try", str(p), str(p)])

    args.extend([
        "--ro-bind-try", "/etc", "/etc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/home",
    ])

    if Path("/opt").is_dir():
        args.extend(["--ro-bind-try", "/opt", "/opt"])

    # Locate virtualenvs or external python trees that need to be mounted read-only
    extra_ro_binds: set[Path] = set()

    # 1. Host virtualenv if running inside one
    if sys.prefix != sys.base_prefix:
        extra_ro_binds.add(Path(sys.prefix).resolve())

    # 2. Python executable from argument
    if python:
        py_arg = Path(python).resolve()
        if (py_arg.parent.parent / "pyvenv.cfg").is_file():
            extra_ro_binds.add(py_arg.parent.parent.resolve())
        elif not str(py_arg).startswith("/usr") and not str(py_arg).startswith("/bin"):
            extra_ro_binds.add(py_arg.parent.resolve())

    # 3. Path from sys.executable
    raw_exe = Path(sys.executable)
    if (raw_exe.parent.parent / "pyvenv.cfg").is_file():
        extra_ro_binds.add(raw_exe.parent.parent.resolve())

    for root_path in sorted(extra_ro_binds):
        if root_path != iso and root_path != work:
            try:
                root_path.relative_to(iso)
                continue
            except ValueError:
                pass
            try:
                root_path.relative_to(work)
                continue
            except ValueError:
                pass
            args.extend(["--ro-bind", str(root_path), str(root_path)])

    # Read-write bindings strictly for isolated copy and work directory
    args.extend(["--bind", str(iso), str(iso)])
    args.extend(["--bind", str(work), str(work)])

    # Remount /home read-only to ensure test process cannot touch host or virtual home
    args.extend(["--remount-ro", "/home"])

    # Network isolation
    args.append("--unshare-all")
    if allow_network:
        args.append("--share-net")

    args.extend([
        "--die-with-parent",
        "--chdir", str(iso),
        "--setenv", "HOME", "/home/sandbox",
    ])
    args.extend(cmd)
    return args

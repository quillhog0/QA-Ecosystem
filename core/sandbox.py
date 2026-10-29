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
import subprocess
import sys
from pathlib import Path
from typing import Any

_PROBE_CACHE: dict[tuple[str, Any], tuple[bool, str | None]] = {}


def clear_probe_cache() -> None:
    """Clear internal bwrap probe cache (primarily for unit tests)."""
    _PROBE_CACHE.clear()


def _probe_bwrap(bwrap_bin: str | None = None) -> tuple[bool, str | None]:
    """Probe whether bwrap can actually create unprivileged namespaces and mount.

    Returns (usable, failure_reason).
    """
    if not sys.platform.startswith("linux"):
        return False, None
    bin_path = bwrap_bin or shutil.which("bwrap")
    if bin_path is None:
        return False, None

    cache_key = (bin_path, subprocess.run)
    if cache_key in _PROBE_CACHE:
        return _PROBE_CACHE[cache_key]

    try:
        proc = subprocess.run(
            [bin_path, "--unshare-all", "--ro-bind", "/", "/", "true"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            res: tuple[bool, str | None] = (True, None)
        else:
            err = (proc.stderr or proc.stdout or "").strip()
            reason = err.splitlines()[-1].strip() if err else f"exit code {proc.returncode}"
            res = (False, reason)
    except subprocess.TimeoutExpired:
        res = (False, "probe timed out")
    except OSError as exc:
        res = (False, str(exc))

    _PROBE_CACHE[cache_key] = res
    return res


def is_bwrap_available() -> bool:
    """Return True if running on Linux and bubblewrap (bwrap) executable is usable."""
    if not sys.platform.startswith("linux"):
        return False
    if shutil.which("bwrap") is None:
        return False
    usable, _ = _probe_bwrap()
    return usable


_REAL_IS_BWRAP_AVAILABLE = is_bwrap_available


def get_bwrap_path() -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    return shutil.which("bwrap")


def resolve_sandbox_mode(config: dict[str, Any] | None) -> str:
    """Resolve sandbox mode: auto (default), required, or off."""
    if not config:
        return "auto"
    sb = config.get("sandbox")
    if sb is None:
        return "auto"
    if isinstance(sb, dict):
        raw = sb.get("mode")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return "auto"
        if not isinstance(raw, str):
            raise ValueError(
                f"Invalid sandbox mode: {raw!r}; expected 'auto', 'required', or 'off'"
            )
        mode = raw.strip().lower()
    elif isinstance(sb, str):
        if not sb.strip():
            return "auto"
        mode = sb.strip().lower()
    else:
        raise ValueError(
            f"Invalid sandbox configuration: {sb!r}; expected mapping or mode string"
        )
    if mode not in {"auto", "required", "off"}:
        raise ValueError(
            f"Unknown sandbox mode: {mode!r}; expected 'auto', 'required', or 'off'"
        )
    return mode


def get_sandbox_status(config: dict[str, Any] | None) -> tuple[bool, str, str | None]:
    """Return (enabled, label, failure_reason).

    Labels:
    - 'bwrap': bwrap sandbox is active
    - 'unavailable': bwrap is not available / not Linux
    - 'unavailable (bwrap present but not usable: <reason>)': bwrap executable exists but probe failed
    - 'off': sandbox explicitly disabled
    """
    mode = resolve_sandbox_mode(config)
    if mode == "off":
        return False, "off", None

    if is_bwrap_available is not _REAL_IS_BWRAP_AVAILABLE:
        available = is_bwrap_available()
        probe_reason = None
    else:
        available, probe_reason = _probe_bwrap()

    if available:
        return True, "bwrap", None

    if probe_reason:
        status_label = f"unavailable (bwrap present but not usable: {probe_reason})"
        if mode == "required":
            return (
                False,
                status_label,
                f"sandbox mode is required but bwrap present but not usable: {probe_reason}",
            )
        return False, status_label, None

    if mode == "required":
        return (
            False,
            "unavailable",
            "sandbox mode is required but bubblewrap (bwrap) is not available or not on Linux",
        )
    return False, "unavailable", None


def _validate_ro_bind_path(path: Path, host_home: Path) -> Path:
    """Reject any path that equals or is an ancestor of the host $HOME or of /."""
    resolved = path.resolve()
    if resolved == Path("/") or host_home.is_relative_to(resolved):
        raise ValueError(
            f"Refusing to bind unsafe read-only path '{resolved}': "
            f"equals or is an ancestor of host HOME ('{host_home}') or '/'"
        )
    return resolved


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
    cache_dir = work / "pytest_cache"
    pytest_tmp_dir = work / "pytest_tmp"
    home_dir = work / "home"
    cache_dir.mkdir(parents=True, exist_ok=True)
    pytest_tmp_dir.mkdir(parents=True, exist_ok=True)
    home_dir.mkdir(parents=True, exist_ok=True)
    host_home = Path.home().resolve()

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
        extra_ro_binds.add(_validate_ro_bind_path(Path(sys.prefix), host_home))

    # 2. Python executable from argument
    if python:
        raw_py = Path(python).expanduser()
        if raw_py.is_absolute() or len(raw_py.parts) > 1:
            raw_parent = raw_py.parent.resolve()
            if not str(raw_parent).startswith(("/usr", "/bin", "/sbin")):
                _validate_ro_bind_path(raw_parent, host_home)
        py_arg = raw_py.resolve()
        # A venv's bin/python is usually a symlink to the base interpreter, so
        # look for pyvenv.cfg next to the unresolved path before resolving it.
        if (raw_py.parent.parent / "pyvenv.cfg").is_file():
            extra_ro_binds.add(_validate_ro_bind_path(raw_py.parent.parent.resolve(), host_home))
        elif (py_arg.parent.parent / "pyvenv.cfg").is_file():
            extra_ro_binds.add(_validate_ro_bind_path(py_arg.parent.parent, host_home))
        elif not str(py_arg).startswith("/usr") and not str(py_arg).startswith("/bin"):
            extra_ro_binds.add(_validate_ro_bind_path(py_arg.parent, host_home))

    # 3. Path from sys.executable
    raw_exe = Path(sys.executable)
    if (raw_exe.parent.parent / "pyvenv.cfg").is_file():
        extra_ro_binds.add(_validate_ro_bind_path(raw_exe.parent.parent, host_home))

    for root_path in sorted(extra_ro_binds):
        _validate_ro_bind_path(root_path, host_home)
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

    # Read-write binding for isolated copy; work_dir is read-only except cache, tmp, and sandbox home
    args.extend(["--bind", str(iso), str(iso)])
    args.extend(["--ro-bind", str(work), str(work)])
    args.extend(["--bind", str(cache_dir), str(cache_dir)])
    args.extend(["--bind", str(pytest_tmp_dir), str(pytest_tmp_dir)])
    args.extend(["--bind", str(home_dir), "/home/sandbox"])

    # Remount /home read-only so HOME exists and is writable only at /home/sandbox
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

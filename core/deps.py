import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from core.runner import sanitized_env


def _check_unsafe_requirement(line: str) -> str | None:
    stripped = line.split("#", 1)[0].strip()
    if not stripped:
        return None
    if any(stripped.startswith(proto) or f"://{proto}" in stripped for proto in ("git+", "http://", "https://", "hg+", "svn+")):
        return stripped
    if "://" in stripped:
        return stripped
    if "@" in stripped:
        return stripped
    if stripped == "-e" or stripped.startswith("-e ") or stripped.startswith("-e="):
        return stripped
    if stripped.startswith("-r") or stripped.startswith("-c"):
        rest = stripped[2:].strip(" =")
        if ".." in rest or rest.startswith("/") or rest.startswith("\\") or (len(rest) > 1 and rest[1] == ":"):
            return stripped
    if ".." in stripped:
        return stripped
    if stripped.startswith("/") or stripped.startswith("./") or stripped.startswith(".\\") or stripped.startswith("~/"):
        return stripped
    return None


def _read_pyproject_deps(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    deps = data.get("project", {}).get("dependencies", [])
    optional_deps = data.get("project", {}).get("optional-dependencies", {})
    deps.extend(optional_deps.get("dev", []))
    deps.extend(optional_deps.get("test", []))
    return deps


def venv_dir(work_dir: Path) -> Path:
    """Single source of truth for the per-run virtualenv location."""
    return work_dir / "venv"


def venv_python(work_dir: Path) -> Path:
    """Python executable inside the per-run virtualenv."""
    return venv_dir(work_dir) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _cache_base_dir() -> Path:
    """Operator-scoped cache root; never inside the tested project or the
    isolated copy. Overridable via QA_VENV_CACHE_DIR (used by tests)."""
    override = os.environ.get("QA_VENV_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "qa-ecosystem" / "venvs"


def _cache_paths(key: str) -> tuple[Path, Path, Path]:
    entry = _cache_base_dir() / key
    return entry, entry / "venv", entry / "valid.ok"


def _venv_cache_key(
    requirement_files: list[Path], pyproject_deps: list[str]
) -> str | None:
    """Content-addressed cache key.

    Keyed on the actual requirement file contents (not the volatile
    ``-r<abs-path>`` install args), plus the interpreter so a reused venv always
    matches the base python that built it. Returns None if a manifest cannot be
    read, which simply disables reuse.
    """
    digest = hashlib.sha256()
    digest.update(f"py:{sys.version}|exe:{sys.executable}|".encode("utf-8", "replace"))
    if requirement_files:
        for f in sorted(requirement_files, key=lambda p: p.name):
            try:
                content = f.read_bytes()
            except OSError:
                return None
            digest.update(f"req:{f.name}:".encode("utf-8", "replace"))
            digest.update(content)
            digest.update(b"\x00")
    else:
        for dep in pyproject_deps:
            digest.update(f"dep:{dep}\n".encode("utf-8", "replace"))
    digest.update(b"pytest\n")
    return digest.hexdigest()


def _reuse_cached_venv(cache_key: str, work_dir: Path) -> bool:
    """Copy a valid cached venv into ``work_dir/venv``. Returns True on hit.

    A cache entry is reusable only when its validity marker and venv tree both
    exist; a failed/partial install never writes the marker, so it is ignored.
    """
    if not cache_key:
        return False
    _, cache_venv, cache_marker = _cache_paths(cache_key)
    if not (cache_marker.is_file() and cache_venv.is_dir()):
        return False
    dest = venv_dir(work_dir)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    try:
        shutil.copytree(cache_venv, dest, symlinks=True)
    except (OSError, shutil.Error):
        shutil.rmtree(dest, ignore_errors=True)
        return False
    python_executable = venv_python(work_dir)
    if python_executable.is_file() or python_executable.is_symlink():
        return True
    shutil.rmtree(dest, ignore_errors=True)
    return False


def _persist_cached_venv(cache_key: str, work_dir: Path) -> None:
    """Best-effort publish of a freshly built venv to the cache.

    The validity marker is written only after a complete copy, so an interrupted
    or failed publish can never be reused. Any error here is swallowed: caching
    must not change the outcome of a successful install.
    """
    if not cache_key:
        return
    entry_dir, cache_venv, cache_marker = _cache_paths(cache_key)
    try:
        entry_dir.mkdir(parents=True, exist_ok=True)
        if cache_marker.exists():
            cache_marker.unlink()
        if cache_venv.exists():
            shutil.rmtree(cache_venv, ignore_errors=True)
        shutil.copytree(venv_dir(work_dir), cache_venv, symlinks=True)
        cache_marker.write_text(cache_key, encoding="utf-8")
    except (OSError, shutil.Error):
        try:
            if cache_marker.exists():
                cache_marker.unlink()
        except OSError:
            pass


def prepare_venv(isolated_root: Path, work_dir: Path, *, timeout: int) -> tuple[Path | None, str]:
    venv_path = venv_dir(work_dir)
    pip_log_path = work_dir / "pip.log"

    # Find dependency manifest
    requirements_files = [
        isolated_root / "requirements.txt",
        isolated_root / "requirements-dev.txt",
        isolated_root / "requirements-test.txt",
    ]
    found_requirements = [f for f in requirements_files if f.is_file()]
    pyproject_toml = isolated_root / "pyproject.toml"

    deps_to_install: list[str] = []
    pyproject_deps: list[str] = []
    if found_requirements:
        for f in found_requirements:
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
                for line in content.splitlines():
                    unsafe = _check_unsafe_requirement(line)
                    if unsafe:
                        return None, f"pip install failed: unsafe requirement: {unsafe}"
                deps_to_install.append(f"-r{f.resolve()}")
            except Exception:
                pass
    elif pyproject_toml.is_file():
        for dep in _read_pyproject_deps(pyproject_toml):
            unsafe = _check_unsafe_requirement(dep)
            if unsafe:
                return None, f"pip install failed: unsafe requirement: {unsafe}"
            pyproject_deps.append(dep)
            deps_to_install.append(dep)

    if not deps_to_install:
        return None, "no dependency manifest"

    # Reuse a previously completed install for this exact manifest, if present.
    cache_key = _venv_cache_key(found_requirements, pyproject_deps)
    if _reuse_cached_venv(cache_key, work_dir):
        return venv_python(work_dir), "ok"

    # Create venv
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(venv_path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return None, f"venv creation failed: {e.stderr.strip()}"

    python_executable = venv_python(work_dir)

    # Install pytest and dependencies
    pip_command = [
        str(python_executable),
        "-m",
        "pip",
        "install",
        "pytest",
    ] + deps_to_install

    env = sanitized_env(allow_network=True)
    env["HOME"] = str(work_dir / "home")

    try:
        result = subprocess.run(
            pip_command,
            env=env,
            cwd=work_dir,
            timeout=timeout,
            check=True,
            capture_output=True,
            text=True,
        )
        pip_log_path.write_text(result.stdout + result.stderr, encoding="utf-8")
        _persist_cached_venv(cache_key, work_dir)
        return python_executable, "ok"
    except subprocess.TimeoutExpired:
        pip_log_path.write_text("pip install timed out", encoding="utf-8")
        return None, "pip install failed: timed out"
    except subprocess.CalledProcessError as e:
        pip_log_path.write_text(e.stdout + e.stderr, encoding="utf-8")
        last_line = e.stderr.strip().splitlines()[-1] if e.stderr else "unknown error"
        return None, f"pip install failed: {last_line}"
    except Exception as e:
        pip_log_path.write_text(str(e), encoding="utf-8")
        return None, f"pip install failed: {e}"

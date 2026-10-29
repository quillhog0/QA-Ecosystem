import os
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
            deps_to_install.append(dep)

    if not deps_to_install:
        return None, "no dependency manifest"

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

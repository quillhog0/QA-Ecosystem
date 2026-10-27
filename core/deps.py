from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

from core.runner import sanitized_env


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


def prepare_venv(isolated_root: Path, work_dir: Path, *, timeout: int) -> tuple[Path | None, str]:
    venv_path = work_dir / "venv"
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
                deps_to_install.append(f"-r{f.resolve()}")
            except Exception:
                pass
    elif pyproject_toml.is_file():
        deps_to_install.extend(_read_pyproject_deps(pyproject_toml))

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

    python_executable = venv_path / "bin" / "python"

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
            cwd=isolated_root,
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

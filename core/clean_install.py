"""Clean install verification from scratch."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agents.inventory import list_docs
from core.runner import sanitized_env
from core.toolchain import detect_languages


def find_readme_install_command(project_root: Path, docs_root: Path | None = None) -> str | None:
    """Extract the first installation command mentioned in README/docs."""
    search_dirs = [project_root]
    if docs_root and docs_root.is_dir() and docs_root != project_root:
        search_dirs.append(docs_root)

    install_pattern = re.compile(
        r"(?:^\s*[$>]\s*|`)(pip install [^`\n]+|npm install[^`\n]*|pnpm install[^`\n]*|yarn(?: install)?[^`\n]*|npm i\b[^`\n]*)"
    )

    for sdir in search_dirs:
        for doc in list_docs(sdir):
            try:
                text = doc.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                m = install_pattern.search(line)
                if m:
                    cmd = m.group(1).strip()
                    if cmd:
                        return cmd
    return None


def run_clean_install(
    project_root: Path,
    work_dir: Path,
    *,
    timeout: int = 300,
) -> dict[str, Any]:
    """Test installing from scratch in an empty venv or fresh node_modules as README says."""
    clean_dir = work_dir / "clean_install_scratch"
    clean_dir.mkdir(parents=True, exist_ok=True)

    install_cmd_str = find_readme_install_command(project_root)
    languages = detect_languages(project_root)

    if not install_cmd_str:
        if "python" in languages:
            install_cmd_str = f"pip install {project_root}"
        elif "ts" in languages:
            install_cmd_str = "npm install"
        else:
            return {"failed": False, "command": "none", "output": ""}

    # Run the installation in an isolated scratch environment
    env = sanitized_env(allow_network=True)
    env["HOME"] = str(clean_dir / "home")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

    if "pip install" in install_cmd_str:
        # Create empty venv
        venv_dir = clean_dir / "venv"
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir)],
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
            pip_bin = venv_dir / "bin" / "pip"
            parts = install_cmd_str.split()
            # replace "pip" with pip_bin
            run_cmd = [str(pip_bin)] + parts[2:]
            # Replace '.' with project_root if relative
            run_cmd = [str(project_root) if p in (".", ".[test]", ".[dev]", ".[tests]") else p for p in run_cmd]
            proc = subprocess.run(
                run_cmd,
                cwd=str(project_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip()[-500:]
                return {"failed": True, "command": install_cmd_str, "output": tail}
        except Exception as exc:
            return {"failed": True, "command": install_cmd_str, "output": str(exc)}
    elif any(pm in install_cmd_str for pm in ("npm", "pnpm", "yarn")):
        # TS install check
        parts = install_cmd_str.split()
        pm_name = parts[0]
        pm_bin = shutil.which(pm_name)
        if not pm_bin:
            return {"failed": True, "command": install_cmd_str, "output": f"package manager '{pm_name}' not in PATH"}
        run_cmd = [pm_bin] + parts[1:]
        try:
            proc = subprocess.run(
                run_cmd,
                cwd=str(project_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout).strip()[-500:]
                return {"failed": True, "command": install_cmd_str, "output": tail}
        except Exception as exc:
            return {"failed": True, "command": install_cmd_str, "output": str(exc)}

    return {"failed": False, "command": install_cmd_str, "output": ""}


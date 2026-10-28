#!/usr/bin/env python3
"""Flat QA agent baseline: runs pytest directly in the project directory.

Verdict is CLEAR if pytest exit code is 0, else VETO. No LLM, no isolation.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Any


def run_flat_baseline(project_path: Path) -> dict[str, Any]:
    project_path = project_path.resolve()
    cmd = [sys.executable, "-m", "pytest", str(project_path), "-q"]
    proc = subprocess.run(
        cmd,
        cwd=str(project_path),
        capture_output=True,
        text=True,
        check=False,
    )
    verdict = "CLEAR" if proc.returncode == 0 else "VETO"
    return {
        "project": str(project_path),
        "verdict": verdict,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flat pytest-only baseline agent")
    parser.add_argument("project", help="Path to project directory")
    args = parser.parse_args(argv)

    project_dir = Path(args.project)
    if not project_dir.is_dir():
        print(f"Error: {project_dir} is not a directory", file=sys.stderr)
        return 2

    res = run_flat_baseline(project_dir)
    print(f"verdict={res['verdict']}")
    print(f"exit_code={res['exit_code']}")
    return 0 if res["verdict"] == "CLEAR" else 1


if __name__ == "__main__":
    sys.exit(main())

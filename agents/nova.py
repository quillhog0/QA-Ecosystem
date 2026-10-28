"""Nova — executor. Runs pytest in isolation. Never patches code."""

from __future__ import annotations

from pathlib import Path

from typing import Any

from core.packets import ExecutionResult
from core.runner import run_pytest
from core.deps import prepare_venv


def execute(
    isolated_root: Path,
    *,
    work_dir: Path,
    allow_network: bool,
    timeout: int = 120,
    config: dict[str, Any],
) -> ExecutionResult:
    install_deps = bool((config.get("nova") or {}).get("install_deps", False))
    pip_timeout = int((config.get("nova") or {}).get("pip_timeout", 300))

    python_executable: Path | None = None
    install_status = "skipped"
    if install_deps:
        python_executable, install_status = prepare_venv(
            isolated_root, work_dir, timeout=pip_timeout
        )

    result = run_pytest(
        isolated_root,
        work_dir=work_dir,
        allow_network=allow_network,
        timeout=timeout,
        python=python_executable,
        config=config,
    )
    result.install_status = install_status
    return result

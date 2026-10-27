"""Nova — executor. Runs pytest in isolation. Never patches code."""

from __future__ import annotations

from pathlib import Path

from core.packets import ExecutionResult
from core.runner import run_pytest


def execute(
    isolated_root: Path,
    *,
    work_dir: Path,
    allow_network: bool,
    timeout: int = 120,
) -> ExecutionResult:
    return run_pytest(
        isolated_root,
        work_dir=work_dir,
        allow_network=allow_network,
        timeout=timeout,
    )

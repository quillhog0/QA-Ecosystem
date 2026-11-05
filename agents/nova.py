"""Nova — executor. Runs pytest and TS/JS test runners in isolation. Never patches code."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.deps import prepare_venv
from core.packets import ExecutionResult
from core.runner import run_pytest
from core.toolchain import detect_languages
from core.ts_runner import prepare_node_modules, run_ts_tests


def execute(
    isolated_root: Path,
    *,
    work_dir: Path,
    allow_network: bool,
    timeout: int = 120,
    config: dict[str, Any],
    test_targets: list[str] | None = None,
) -> ExecutionResult:
    install_deps = bool((config.get("nova") or {}).get("install_deps", False))
    pip_timeout = int((config.get("nova") or {}).get("pip_timeout", 300))

    languages = detect_languages(isolated_root)
    if not languages:
        languages = ["python"]

    python_executable: Path | None = None
    install_entries: list[tuple[str, str]] = []

    diff_changed = (config.get("_diff_changed") or []) if config else []
    is_diff = "_diff_changed" in (config or {})
    ts_changed = any(
        p.endswith((".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", "tsconfig.json", "package.json"))
        for p in diff_changed
    ) if is_diff else True

    py_changed = any(
        p.endswith((".py", ".toml", ".cfg", ".txt", ".ini"))
        for p in diff_changed
    ) if is_diff else True

    if "python" in languages and install_deps and (py_changed or test_targets):
        python_executable, py_status = prepare_venv(
            isolated_root, work_dir, timeout=pip_timeout
        )
        install_entries.append(("python", py_status))

    if "ts" in languages and install_deps and ts_changed:
        ts_ok, ts_status = prepare_node_modules(
            isolated_root, work_dir, timeout=pip_timeout
        )
        install_entries.append(("ts", ts_status))

    if len(install_entries) == 1:
        install_status = install_entries[0][1]
    elif len(install_entries) > 1:
        install_status = ", ".join(f"{lang}: {status}" for lang, status in install_entries)
    else:
        install_status = "skipped"

    results: list[ExecutionResult] = []

    if "python" in languages and (py_changed or test_targets):
        py_res = run_pytest(
            isolated_root,
            work_dir=work_dir,
            allow_network=allow_network,
            timeout=timeout,
            python=python_executable,
            config=config,
            test_targets=test_targets,
        )
        py_res.language = "python"
        results.append(py_res)

    if "ts" in languages and ts_changed:
        ts_res = run_ts_tests(
            isolated_root,
            work_dir=work_dir,
            allow_network=allow_network,
            timeout=timeout,
            config=config,
            test_targets=test_targets,
        )
        ts_res.language = "ts"
        results.append(ts_res)

    if not results:
        return ExecutionResult(isolated_root=str(isolated_root), install_status=install_status)

    if len(results) == 1:
        res = results[0]
        res.install_status = install_status
        return res

    # Merged result for mixed repo
    merged_failures = []
    merged_flaky_tests = []
    for r in results:
        merged_failures.extend(r.failures)
        merged_flaky_tests.extend(r.flaky_tests)

    merged = ExecutionResult(
        collected=sum(r.collected for r in results),
        passed=sum(r.passed for r in results),
        failed=sum(r.failed for r in results),
        skipped=sum(r.skipped for r in results),
        errors=sum(r.errors for r in results),
        product_failed=sum(r.product_failed for r in results),
        environment_failed=sum(r.environment_failed for r in results),
        flaky=sum(r.flaky for r in results),
        flaky_tests=merged_flaky_tests,
        failures=merged_failures,
        exit_code=max(r.exit_code for r in results),
        duration_seconds=round(sum(r.duration_seconds for r in results), 3),
        stdout="\n".join(r.stdout for r in results if r.stdout),
        stderr="\n".join(r.stderr for r in results if r.stderr),
        command=results[0].command,
        isolated_root=str(isolated_root),
        sandbox_status=results[0].sandbox_status,
        install_status=install_status,
    )
    merged.language_breakdown = {
        getattr(r, "language", "unknown"): {
            "collected": r.collected,
            "passed": r.passed,
            "failed": r.failed,
            "skipped": r.skipped,
        }
        for r in results
        if getattr(r, "language", None)
    }
    return merged

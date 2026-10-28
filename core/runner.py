"""Nova execution helper: run pytest inside the isolated copy."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from core.packets import ExecutionResult

COLLECTED_RE = re.compile(r"collected\s+(\d+)\s+item", re.IGNORECASE)
SUMMARY_RE = re.compile(
    r"(?P<failed>\d+)\s+failed|(?P<passed>\d+)\s+passed|(?P<skipped>\d+)\s+skipped|"
    r"(?P<errors>\d+)\s+error",
    re.IGNORECASE,
)

SECRET_ENV_FRAGMENTS = (
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "PASSWD",
    "API_KEY",
    "PRIVATE_KEY",
    "CREDENTIAL",
    "ACCESS_KEY",
    "AUTH",
)


def sanitized_env(*, allow_network: bool) -> dict[str, str]:
    """Build a minimal environment. Drop secret-looking variables."""
    keep = {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "TEMP",
        "TMP",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "COMSPEC",
    }
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(frag in upper for frag in SECRET_ENV_FRAGMENTS):
            continue
        if key in keep or key.startswith("PYTEST") or key.startswith("PYTHON"):
            if "SECRET" in upper or "TOKEN" in upper or "KEY" in upper:
                continue
            env[key] = value
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if not allow_network:
        env["NO_NETWORK"] = "1"
        env["HTTP_PROXY"] = "http://127.0.0.1:9"
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["ALL_PROXY"] = "http://127.0.0.1:9"
        env["NO_PROXY"] = ""
    return env


def build_pytest_command(
    isolated_root: Path,
    *,
    cache_dir: Path,
    basetemp: Path,
    extra_args: list[str] | None = None,
    python: Path | None = None,
) -> list[str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    basetemp.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(python) if python else sys.executable,
        "-m",
        "pytest",
        str(isolated_root),
        "-v",
        "--tb=short",
        "-o",
        f"cache_dir={cache_dir}",
        "--basetemp",
        str(basetemp),
    ]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _find_summary_line(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return ""
    for line in reversed(lines):
        if line.startswith("=") and line.endswith("=") and re.search(r"\bin\s+[\d\.]+(?:s|ms)?\s*=+$", line):
            return line
    return lines[-1]


def _parse_counts(output: str) -> tuple[int, int, int, int, int]:
    collected = 0
    match = COLLECTED_RE.search(output)
    if match:
        collected = int(match.group(1))
    passed = failed = skipped = errors = 0
    summary_line = _find_summary_line(output)
    for m in SUMMARY_RE.finditer(summary_line):
        if m.group("passed"):
            passed = int(m.group("passed"))
        if m.group("failed"):
            failed = int(m.group("failed"))
        if m.group("skipped"):
            skipped = int(m.group("skipped"))
        if m.group("errors"):
            errors = int(m.group("errors"))
    if not match and (passed > 0 or failed > 0):
        collected = passed + failed + skipped + errors
    return collected, passed, failed, skipped, errors


def run_pytest(
    isolated_root: Path,
    *,
    work_dir: Path,
    allow_network: bool,
    timeout: int = 120,
    extra_args: list[str] | None = None,
    python: Path | None = None,
    config: dict[str, Any] | None = None,
) -> ExecutionResult:
    """Execute pytest against the isolated tree. Does not modify the live project."""
    import subprocess
    from core.sandbox import get_sandbox_status, wrap_command

    sandbox_enabled, sandbox_status, failure_reason = get_sandbox_status(config)
    if failure_reason:
        return ExecutionResult(
            exit_code=1,
            stderr=failure_reason,
            sandbox_status=sandbox_status,
            isolated_root=str(isolated_root),
        )

    cache_dir = work_dir / "pytest_cache"
    basetemp = work_dir / "pytest_tmp"
    pytest_log_path = work_dir / "pytest.log"
    command = build_pytest_command(
        isolated_root, cache_dir=cache_dir, basetemp=basetemp, extra_args=extra_args, python=python
    )
    if (work_dir / "_sable_guard.py").is_file() and "_sable_guard" not in command:
        command.extend(["-p", "_sable_guard"])

    exec_command = command
    if sandbox_enabled:
        exec_command = wrap_command(
            command,
            isolated_root=isolated_root,
            work_dir=work_dir,
            python=python,
            allow_network=allow_network,
        )

    env = sanitized_env(allow_network=allow_network)
    env["HOME"] = str(work_dir / "home")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    extra_path = str(isolated_root)
    work_path = str(work_dir)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        work_path + os.pathsep + extra_path
        if not existing
        else work_path + os.pathsep + extra_path + os.pathsep + existing
    )
    env["QA_AGENT_NOVA"] = "1"

    started = time.monotonic()
    try:
        proc = subprocess.run(
            exec_command,
            cwd=str(isolated_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        pytest_log_path.write_text(stdout + stderr, encoding="utf-8")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        stderr = ((exc.stderr or "") if isinstance(exc.stderr, str) else "") + "\nTIMEOUT"
        exit_code = 124
    duration = time.monotonic() - started
    collected, passed, failed, skipped, errors = _parse_counts(stdout + "\n" + stderr)
    return ExecutionResult(
        collected=collected,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        exit_code=exit_code,
        duration_seconds=round(duration, 3),
        stdout=stdout,
        stderr=stderr,
        command=exec_command,
        isolated_root=str(isolated_root),
        sandbox_status=sandbox_status,
    )

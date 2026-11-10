"""Nova execution helper: run pytest inside the isolated copy."""

from __future__ import annotations

import configparser
import os
import re
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from core.environment import annotate_failures
from core.junit import parse_failures
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


ALLOWED_ENV_KEYS = frozenset({
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "COMSPEC",
})

NEVER_PASS_ENV_KEYS = frozenset({
    "PYTEST_ADDOPTS",
    "PYTHONSTARTUP",
    "PYTHONPATH",
})


def sanitized_env(*, allow_network: bool) -> dict[str, str]:
    """Build a minimal environment from a strict allowlist. Drop secret-looking variables."""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in NEVER_PASS_ENV_KEYS:
            continue
        if key not in ALLOWED_ENV_KEYS:
            continue
        upper = key.upper()
        if any(frag in upper for frag in SECRET_ENV_FRAGMENTS) or "KEY" in upper:
            continue
        env[key] = value
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    if not allow_network:
        env["NO_NETWORK"] = "1"
        env["HTTP_PROXY"] = "http://127.0.0.1:9"
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        env["ALL_PROXY"] = "http://127.0.0.1:9"
        # Keep the dead proxy for off-box URLs, but do not intercept loopback
        # HTTP used by in-process test servers (127.0.0.1/::1/localhost).
        env["NO_PROXY"] = "127.0.0.1,::1,localhost"
        env["no_proxy"] = "127.0.0.1,::1,localhost"
    return env


def _ini_testpaths(isolated_root: Path) -> list[str]:
    """The project's own ``testpaths``, read from whichever ini pytest prefers.

    pytest's ini precedence is honoured (``pytest.ini``, then ``pyproject.toml``,
    then ``tox.ini``, then ``setup.cfg``) and the first file that declares the key
    wins - including one that declares it empty, which means "no declared paths".
    Nothing is executed and nothing outside the copy is read.
    """
    candidates = (
        ("pytest.ini", "pytest", "testpaths"),
        ("pyproject.toml", "tool.pytest.ini_options", "testpaths"),
        ("tox.ini", "pytest", "testpaths"),
        ("setup.cfg", "tool:pytest", "testpaths"),
    )
    for name, section, key in candidates:
        path = isolated_root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if name == "pyproject.toml":
            try:
                data = tomllib.loads(text)
            except (OSError, ValueError):
                continue
            options = ((data or {}).get("tool") or {}).get("pytest") or {}
            ini_opts = options.get("ini_options") if isinstance(options.get("ini_options"), dict) else {}
            value = ini_opts.get(key) if key in ini_opts else options.get(key)
        else:
            parser = configparser.ConfigParser()
            try:
                parser.read_string(text)
            except configparser.Error:
                continue
            if not parser.has_section(section):
                continue
            value = parser.get(section, key, fallback=None)
        if isinstance(value, str):
            # ini files list them whitespace-separated across lines.
            return [part for part in value.split() if part]
        if isinstance(value, list):
            return [str(part) for part in value if str(part).strip()]
        if value is not None:
            return []
    return []


def _collection_args(isolated_root: Path) -> list[str]:
    """What pytest should collect, in the project's own words.

    Passing the copy root as a positional argument is what pytest does when a
    developer runs it from inside the checkout *without* a declared ``testpaths``,
    but it silently overrides ``testpaths`` when the project has them: QA would
    then judge the suite on files the project's own CI never runs (in practice:
    stray root-level ``test_*.py`` scratch files). Reading the declared paths and
    handing them over explicitly keeps QA and CI collecting the same tests, and
    keeps rootdir, ``pythonpath`` and ini discovery anchored in the copy.
    """
    declared = _ini_testpaths(isolated_root)
    if declared:
        existing = [
            str((isolated_root / entry).resolve())
            for entry in declared
            if (isolated_root / entry).exists()
        ]
        if existing:
            return existing
    return [str(isolated_root)]


def _posix_target(target: str) -> str:
    """A copy-relative test target, normalised to posix and stripped of a lead ./.

    Leading slashes and drive separators are dropped so a target from ``git``
    (always repo-relative) resolves inside the isolated copy when joined to it.
    """
    text = str(target).replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def junit_path_for(work_dir: Path) -> Path:
    """Where the JUnit XML of one run lives: inside the sandbox-writable tmp tree.

    ``work_dir`` itself is mounted read-only in the sandbox and only
    ``pytest_cache``, ``pytest_tmp`` and ``home`` are writable, so the XML goes
    under ``pytest_tmp`` - but *beside* the ``--basetemp`` directory, which pytest
    wipes at session start.
    """
    return work_dir / "pytest_tmp" / "junit" / "report.xml"


def build_pytest_command(
    isolated_root: Path,
    *,
    cache_dir: Path,
    basetemp: Path,
    extra_args: list[str] | None = None,
    python: Path | None = None,
    junit_path: Path | None = None,
    test_targets: list[str] | None = None,
) -> list[str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    basetemp.mkdir(parents=True, exist_ok=True)
    if test_targets:
        # Diff mode: collect only the tests related to the changed files. The
        # targets are copy-relative paths resolved to the isolated copy so they
        # stay inside the sandbox, never the live tree.
        collection = [
            str((isolated_root / _posix_target(target)).resolve())
            for target in test_targets
            if (isolated_root / _posix_target(target)).exists()
        ]
        if not collection:
            collection = [str(isolated_root)]
    else:
        collection = _collection_args(isolated_root)
    cmd = [
        str(python) if python else sys.executable,
        "-m",
        "pytest",
        *collection,
        "-v",
        # ``-rfE`` prints one line per failure and per error at the end, whatever
        # the length of the log above it, so a failure is never only visible in a
        # truncated window of the run.
        "-rfE",
        "--tb=short",
        "-o",
        f"cache_dir={cache_dir}",
        "--basetemp",
        str(basetemp / "run"),
    ]
    if junit_path is not None:
        cmd.extend(["--junitxml", str(junit_path)])
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


def _import_roots(isolated_root: Path) -> list[str]:
    """Directories to search before the interpreter's ``site-packages``.

    A src-layout project keeps its importable package under ``<copy>/src``; the
    copy root (already on PYTHONPATH) only covers a flat layout. Prepending
    ``<copy>/src`` lets ``import pkg`` resolve to the isolated copy and, because
    PYTHONPATH precedes ``site-packages``, the copy shadows any same-named
    package leaked from the runner interpreter (e.g. a host-installed ``pluggy``).
    """
    roots: list[str] = []
    src = isolated_root / "src"
    if src.is_dir():
        roots.append(str(src))
    return roots


def run_pytest(
    isolated_root: Path,
    *,
    work_dir: Path,
    allow_network: bool,
    timeout: int = 120,
    extra_args: list[str] | None = None,
    python: Path | None = None,
    config: dict[str, Any] | None = None,
    test_targets: list[str] | None = None,
) -> ExecutionResult:
    """Execute pytest against the isolated tree. Does not modify the live project."""
    import subprocess
    from core.sandbox import get_sandbox_status, wrap_command

    if os.pathsep in str(isolated_root) or os.pathsep in str(work_dir):
        raise ValueError(
            f"isolated_root and work_dir must not contain path separator {os.pathsep!r}"
        )
    isolated_root = Path(isolated_root).resolve()
    work_dir = Path(work_dir).resolve()
    if os.pathsep in str(isolated_root) or os.pathsep in str(work_dir):
        raise ValueError(
            f"Resolved isolated_root and work_dir must not contain path separator {os.pathsep!r}"
        )

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
    junit_path = junit_path_for(work_dir)
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    # A stale XML would report another run's failures, so it is removed before
    # this one starts. Only this exact path, inside this run's own work directory.
    try:
        junit_path.unlink(missing_ok=True)
    except OSError:
        pass
    command = build_pytest_command(
        isolated_root,
        cache_dir=cache_dir,
        basetemp=basetemp,
        extra_args=extra_args,
        python=python,
        junit_path=junit_path,
        test_targets=test_targets,
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
    if (isolated_root / "core" / "toolchain.py").is_file():
        for fname in ("typescript.js", "typescript.sha256"):
            host_f = Path.home() / ".cache" / "qa-ecosystem" / "tooling" / fname
            if host_f.is_file():
                dst_f = Path(env["HOME"]) / ".cache" / "qa-ecosystem" / "tooling" / fname
                os.makedirs(dst_f.parent, exist_ok=bool(dst_f))
                dst_f.write_bytes(host_f.read_bytes())
    extra_path = str(isolated_root)
    work_path = str(work_dir)
    # work, then the copy root, then the copy's own import roots (src-layout).
    # Order matters: every entry here precedes the interpreter's site-packages.
    paths = [work_path, extra_path, *_import_roots(isolated_root)]
    env["PYTHONPATH"] = os.pathsep.join(paths)
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
    # Structured failures outrank the console log: a long verbose run hides its
    # summary block from any clipped view, but every failure is in the XML.
    failures = [failure.to_dict() for failure in parse_failures(junit_path)]
    if failures:
        product_failed, environment_failed = annotate_failures(
            failures, project_root=isolated_root
        )
    else:
        # Nothing proved any failure environmental, so every one of them counts
        # against the project - the conservative default from Task 8b.
        product_failed, environment_failed = failed, 0

    flaky_count = 0
    flaky_tests: list[dict[str, Any]] = []
    is_mutation_run = bool(extra_args and "-x" in extra_args)
    if all((product_failed, errors == 0, exit_code not in (0, 124), not is_mutation_run, failures)):
        product_nodes = [
            str(f.get("node"))
            for f in failures
            if f.get("classification", "product") == "product"
        ]
        if product_nodes:
            rerun_junit = work_dir / "pytest_tmp" / "junit" / "rerun_report.xml"
            if rerun_junit.is_file():
                rerun_junit.unlink()
            rerun_cmd = [
                str(python) if python else sys.executable,
                "-m",
                "pytest",
                *product_nodes,
                "-v",
                "-rfE",
                "--tb=short",
                "-o",
                f"cache_dir={cache_dir}",
                "--basetemp",
                str(basetemp / "rerun"),
                "--junitxml",
                str(rerun_junit),
            ]
            if extra_args:
                rerun_cmd.extend(extra_args)
            if (work_dir / "_sable_guard.py").is_file() and "_sable_guard" not in rerun_cmd:
                rerun_cmd.extend(["-p", "_sable_guard"])
            rerun_exec = (
                wrap_command(
                    rerun_cmd,
                    isolated_root=isolated_root,
                    work_dir=work_dir,
                    python=python,
                    allow_network=allow_network,
                )
                if sandbox_enabled
                else rerun_cmd
            )
            try:
                rerun_kwargs: dict[str, Any] = {
                    "cwd": str(isolated_root),
                    "env": env,
                    "capture_output": bool(rerun_exec),
                    "text": bool(rerun_exec),
                    "timeout": timeout,
                    "check": not bool(rerun_exec),
                }
                rerun_proc = subprocess.run(rerun_exec, **rerun_kwargs)
                still_failing_nodes: set[str] | None = (
                    set()
                    if not rerun_proc.returncode
                    else ({rf.node for rf in parse_failures(rerun_junit)} if rerun_junit.is_file() else None)
                )
                if still_failing_nodes is not None:
                    remaining_failures: list[dict[str, Any]] = []
                    for f in failures:
                        node = str(f.get("node", ""))
                        if node not in still_failing_nodes:
                            flaky_entry = dict(f)
                            flaky_entry["classification"] = "flaky"
                            flaky_tests.append(flaky_entry)
                            flaky_count += 1
                        else:
                            remaining_failures.append(f)
                    if flaky_count:
                        failures = remaining_failures
                        failed = max(0, failed - flaky_count)
                        product_failed = max(0, product_failed - flaky_count)
                        passed += flaky_count
                        if not failed:
                            exit_code = 0
            except subprocess.TimeoutExpired:
                pass

    return ExecutionResult(
        collected=collected,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errors,
        product_failed=product_failed,
        environment_failed=environment_failed,
        flaky=flaky_count,
        flaky_tests=flaky_tests,
        failures=failures,
        exit_code=exit_code,
        duration_seconds=round(duration, 3),
        stdout=stdout,
        stderr=stderr,
        command=exec_command,
        isolated_root=str(isolated_root),
        sandbox_status=sandbox_status,
    )

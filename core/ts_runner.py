"""TypeScript/JavaScript test runner helper."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core.packets import ExecutionResult
from core.runner import sanitized_env
from core.toolchain import detect_package_manager, detect_ts_runner, typecheck_project


def _node_cache_dir() -> Path:
    override = os.environ.get("QA_NODE_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "qa-ecosystem" / "node_modules"


def _lockfile_hash(isolated_root: Path) -> str | None:
    for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "package.json"):
        f = isolated_root / name
        if f.is_file():
            try:
                digest = hashlib.sha256()
                digest.update(f"{name}:".encode("utf-8"))
                digest.update(f.read_bytes())
                return digest.hexdigest()
            except OSError:
                pass
    return None


def _fix_node_modules_symlinks(node_modules: Path) -> None:
    """Ensure all symlinks in node_modules (especially .bin) are relative or valid within node_modules."""
    if not node_modules.is_dir():
        return
    bin_dir = node_modules / ".bin"
    if bin_dir.is_dir():
        for link in bin_dir.iterdir():
            if link.is_symlink():
                try:
                    target = os.readlink(link)
                    target_p = Path(target)
                    if target_p.is_absolute():
                        if "node_modules" in target_p.parts:
                            idx = target_p.parts.index("node_modules")
                            rel_target_parts = target_p.parts[idx + 1:]
                            dest = node_modules.joinpath(*rel_target_parts)
                            if dest.exists():
                                link.unlink()
                                rel_link = os.path.relpath(dest, link.parent)
                                link.symlink_to(rel_link)
                except OSError:
                    pass
            try:
                if link.is_file():
                    mode = link.stat().st_mode
                    link.chmod(mode | 0o755)
            except OSError:
                pass


def prepare_node_modules(
    isolated_root: Path,
    work_dir: Path,
    *,
    timeout: int = 300,
) -> tuple[bool, str]:
    """Install or restore node_modules for the isolated copy before sandbox cuts network."""
    node_modules = isolated_root / "node_modules"
    cache_key = _lockfile_hash(isolated_root)

    if node_modules.is_dir() and (node_modules / ".bin").is_dir():
        if cache_key:
            try:
                cache_entry = _node_cache_dir() / cache_key
                cache_marker = cache_entry / "valid.ok"
                cache_tree = cache_entry / "node_modules"
                if not cache_marker.is_file() or not cache_tree.is_dir():
                    cache_entry.mkdir(parents=True, exist_ok=True)
                    if cache_tree.exists():
                        shutil.rmtree(cache_tree, ignore_errors=True)
                    shutil.copytree(node_modules, cache_tree, symlinks=True)
                    _fix_node_modules_symlinks(cache_tree)
                    cache_marker.write_text(cache_key, encoding="utf-8")
            except Exception:
                pass
        return True, "existing node_modules"
    if cache_key:
        cache_entry = _node_cache_dir() / cache_key
        cache_marker = cache_entry / "valid.ok"
        cache_tree = cache_entry / "node_modules"
        if cache_marker.is_file() and cache_tree.is_dir():
            try:
                if node_modules.exists():
                    shutil.rmtree(node_modules, ignore_errors=True)
                shutil.copytree(cache_tree, node_modules, symlinks=True)
                _fix_node_modules_symlinks(node_modules)
                return True, "restored from cache"
            except (OSError, shutil.Error):
                pass

    pm_name, install_cmd, warn = detect_package_manager(isolated_root)
    npm_bin = shutil.which(install_cmd[0])
    if not npm_bin:
        return False, f"package manager '{install_cmd[0]}' not found in PATH"

    cmd = [npm_bin, *install_cmd[1:]]
    env = sanitized_env(allow_network=True)
    env["PATH"] = f"/tmp/node:/opt/node:{env.get('PATH', '')}"
    env["HOME"] = str(work_dir / "home")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(isolated_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            last_line = err.splitlines()[-1] if err else f"exit code {proc.returncode}"
            return False, f"package install failed: {last_line}"

        # Persist to cache
        if cache_key and node_modules.is_dir():
            _fix_node_modules_symlinks(node_modules)
            try:
                cache_entry = _node_cache_dir() / cache_key
                cache_entry.mkdir(parents=True, exist_ok=True)
                cache_marker = cache_entry / "valid.ok"
                cache_tree = cache_entry / "node_modules"
                if cache_marker.exists():
                    cache_marker.unlink()
                if cache_tree.exists():
                    shutil.rmtree(cache_tree, ignore_errors=True)
                shutil.copytree(node_modules, cache_tree, symlinks=True)
                cache_marker.write_text(cache_key, encoding="utf-8")
            except Exception:
                pass

        return True, "installed"
    except subprocess.TimeoutExpired:
        return False, "package install timed out"
    except Exception as exc:
        return False, f"package install error: {exc}"


def _find_node_binary() -> str:
    """Find node binary path."""
    b = shutil.which("node")
    if b:
        return b
    if Path("/tmp/node/node").is_file():
        return "/tmp/node/node"
    return "node"


def _parse_vitest_json(report_data: dict[str, Any], project_root: Path) -> tuple[int, int, int, int, list[dict[str, Any]]]:
    collected = int(report_data.get("numTotalTests") or 0)
    passed = int(report_data.get("numPassedTests") or 0)
    failed = int(report_data.get("numFailedTests") or 0)
    skipped = int(report_data.get("numPendingTests") or 0)
    failures: list[dict[str, Any]] = []

    for test_result in report_data.get("testResults") or []:
        file_path = str(test_result.get("name") or "")
        try:
            rel_file = str(Path(file_path).relative_to(project_root)).replace("\\", "/")
        except ValueError:
            rel_file = file_path.replace("\\", "/")

        for assertion in test_result.get("assertionResults") or []:
            if assertion.get("status") == "failed":
                title = assertion.get("title") or "unnamed"
                ancestors = assertion.get("ancestorTitles") or []
                full_name = " > ".join([*ancestors, title])
                msgs = assertion.get("failureMessages") or []
                msg_str = "\n".join(msgs)
                failures.append(
                    {
                        "node": f"{rel_file}::{full_name}",
                        "message": msgs[0] if msgs else "test failed",
                        "traceback": msg_str,
                        "file": rel_file,
                    }
                )

    if not collected and (passed > 0 or failed > 0):
        collected = passed + failed + skipped
    return collected, passed, failed, skipped, failures


def _parse_mocha_json(report_data: dict[str, Any], project_root: Path) -> tuple[int, int, int, int, list[dict[str, Any]]]:
    stats = report_data.get("stats") or {}
    collected = int(stats.get("tests") or 0)
    passed = int(stats.get("passes") or 0)
    failed = int(stats.get("failures") or 0)
    skipped = int(stats.get("pending") or 0)
    failures: list[dict[str, Any]] = []

    for item in report_data.get("failures") or []:
        title = item.get("fullTitle") or item.get("title") or "unnamed"
        file_path = item.get("file") or ""
        try:
            rel_file = str(Path(file_path).relative_to(project_root)).replace("\\", "/") if file_path else "tests"
        except ValueError:
            rel_file = file_path
        err = item.get("err") or {}
        failures.append(
            {
                "node": f"{rel_file}::{title}",
                "message": str(err.get("message") or "failed"),
                "traceback": str(err.get("stack") or ""),
                "file": rel_file,
            }
        )
    return collected, passed, failed, skipped, failures


def run_ts_tests(
    isolated_root: Path,
    *,
    work_dir: Path,
    allow_network: bool = False,
    timeout: int = 120,
    config: dict[str, Any] | None = None,
    test_targets: list[str] | None = None,
) -> ExecutionResult:
    """Run TypeScript/JavaScript test suite in isolated copy."""
    from core.sandbox import get_sandbox_status, wrap_command

    isolated_root = Path(isolated_root).resolve()
    work_dir = Path(work_dir).resolve()

    sandbox_enabled, sandbox_status, failure_reason = get_sandbox_status(config)
    if failure_reason:
        return ExecutionResult(
            exit_code=1,
            stderr=failure_reason,
            sandbox_status=sandbox_status,
            isolated_root=str(isolated_root),
        )

    # 1. Typecheck
    diff_changed = (config or {}).get("_diff_changed")
    if diff_changed is not None:
        ts_changed = any(
            p.endswith((".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", "tsconfig.json", "package.json"))
            for p in diff_changed
        )
    else:
        ts_changed = True
    type_errors = typecheck_project(isolated_root) if ts_changed else []

    # 2. Test runner
    runner_info = detect_ts_runner(isolated_root)
    runner_kind = runner_info["runner"]

    tmp_dir = work_dir / "pytest_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    results_json = tmp_dir / "ts-results.json"
    if results_json.exists():
        results_json.unlink()

    node_bin = _find_node_binary()
    local_bin_dir = isolated_root / "node_modules" / ".bin"
    raw_cmd: list[str] = []

    if runner_kind == "vitest":
        vitest_bin = local_bin_dir / "vitest"
        if vitest_bin.is_file():
            raw_cmd = [node_bin, str(vitest_bin), "run", "--reporter=json", f"--outputFile={results_json}"]
        else:
            raw_cmd = [node_bin, "-e", "console.log('vitest not installed'); process.exit(1)"]
    elif runner_kind == "jest":
        jest_bin = local_bin_dir / "jest"
        if jest_bin.is_file():
            raw_cmd = [node_bin, str(jest_bin), "--json", f"--outputFile={results_json}"]
        else:
            raw_cmd = [node_bin, "-e", "console.log('jest not installed'); process.exit(1)"]
    elif runner_kind == "mocha":
        mocha_bin = local_bin_dir / "mocha"
        if mocha_bin.is_file():
            raw_cmd = [node_bin, str(mocha_bin), "--reporter=json"]
        else:
            raw_cmd = [node_bin, "-e", "console.log('mocha not installed'); process.exit(1)"]
    elif runner_kind == "node_test":
        raw_cmd = [node_bin, "--test"]
    else:
        npm_bin = shutil.which("npm") or "npm"
        raw_cmd = [npm_bin, "test"]

    # Filter targets if passed
    if test_targets and runner_kind in ("vitest", "jest"):
        raw_cmd.extend(test_targets)

    exec_cmd = raw_cmd
    if sandbox_enabled:
        exec_cmd = wrap_command(
            raw_cmd,
            isolated_root=isolated_root,
            work_dir=work_dir,
            allow_network=allow_network,
        )

    env = sanitized_env(allow_network=allow_network)
    env["HOME"] = str(work_dir / "home")
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True)
    env["PATH"] = f"/tmp/node:/opt/node:{local_bin_dir}" + os.pathsep + env.get("PATH", "")

    started = time.monotonic()
    try:
        proc = subprocess.run(
            exec_cmd,
            cwd=str(isolated_root),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = (exc.stderr or "") + "\nTIMEOUT"
        exit_code = 124
    duration = time.monotonic() - started

    collected = passed = failed = skipped = errors = 0
    failures: list[dict[str, Any]] = []

    # Parse results
    if results_json.is_file():
        try:
            report_data = json.loads(results_json.read_text(encoding="utf-8"))
            if runner_kind in ("vitest", "jest"):
                collected, passed, failed, skipped, failures = _parse_vitest_json(report_data, isolated_root)
        except Exception:
            pass
    elif runner_kind == "mocha" and stdout.strip().startswith("{"):
        try:
            report_data = json.loads(stdout)
            collected, passed, failed, skipped, failures = _parse_mocha_json(report_data, isolated_root)
        except Exception:
            pass

    if collected == 0:
        # Fall back to exit code and console parsing
        if exit_code == 0:
            collected = passed = 1
        else:
            collected = failed = 1
            failures.append(
                {
                    "node": f"{runner_kind}::execution",
                    "message": f"{runner_kind} exited with non-zero code {exit_code}",
                    "traceback": (stderr or stdout)[-2000:],
                }
            )

    flaky_count = 0
    flaky_tests: list[dict[str, Any]] = []
    if all((failed, exit_code not in (0, 124), "-e" not in raw_cmd, failures)):
        failed_files = sorted({str(f["file"]) for f in failures if f.get("file")})
        rerun_json = tmp_dir / "ts-results-rerun.json"
        if rerun_json.exists():
            rerun_json.unlink()
        rerun_raw = [
            arg.replace(str(results_json), str(rerun_json)) for arg in raw_cmd
        ]
        if not test_targets:
            rerun_raw.extend(failed_files)
        rerun_exec = (
            wrap_command(
                rerun_raw,
                isolated_root=isolated_root,
                work_dir=work_dir,
                allow_network=allow_network,
            )
            if sandbox_enabled
            else rerun_raw
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
            still_failing_nodes: set[str] | None = None
            if not rerun_proc.returncode:
                still_failing_nodes = set()
            elif rerun_json.is_file():
                try:
                    rdata = json.loads(rerun_json.read_text(encoding="utf-8"))
                    _, _, _, _, rfailures = _parse_vitest_json(rdata, isolated_root)
                    still_failing_nodes = {str(rf.get("node")) for rf in rfailures}
                except Exception:
                    still_failing_nodes = None
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
                    passed += flaky_count
                    if not failed:
                        exit_code = 0
        except subprocess.TimeoutExpired:
            pass

    # Attach type errors to failures if any
    for te in type_errors:
        failed += 1
        collected += 1
        failures.append(
            {
                "node": f"{te['file']}::{te['line']}",
                "message": f"TypeScript type error: {te['message']}",
                "traceback": te["evidence"],
                "file": te["file"],
                "line": te["line"],
                "is_type_error": True,
            }
        )

    product_failed = failed
    environment_failed = 0

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
        exit_code=exit_code if not type_errors else 1,
        duration_seconds=round(duration, 3),
        stdout=stdout,
        stderr=stderr,
        command=exec_cmd,
        isolated_root=str(isolated_root),
        sandbox_status=sandbox_status,
    )

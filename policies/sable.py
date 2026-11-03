"""Sable — policy-as-code. BLOCK on risk. No LLM required."""

from __future__ import annotations

import ast
import os
import re
import shlex
from pathlib import Path
from typing import Any

from core.isolation import is_secret_path
from core.packets import PolicyDecision, Verdict
from core.runner import SECRET_ENV_FRAGMENTS, build_pytest_command

DESTRUCTIVE_PATTERNS = (
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*|--force)"),
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bshutil\.rmtree\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),  # fork bomb
)

NETWORK_BINARIES = ("curl", "wget", "nc", "ncat", "ssh", "scp", "sftp", "ftp")
FORBIDDEN_COMMAND_TOKENS = (
    "rm",
    "sudo",
    "chmod",
    "chown",
    "mkfs",
    "dd",
    "curl",
    "wget",
    "ssh",
    "scp",
    "eval",
    "bash",
    "sh",
    "/bin/sh",
)


def _command_tokens(command: list[str] | str) -> list[str]:
    if isinstance(command, str):
        raw = [command]
    else:
        raw = list(command)
    tokens: list[str] = []
    for item in raw:
        try:
            pieces = shlex.split(item, posix=True)
        except ValueError:
            pieces = [item]
        tokens.extend(pieces or [item])
    return tokens


def _token_basename(token: str) -> str:
    return Path(token).name.lower()


def _command_is_pytest_only(command: list[str]) -> tuple[bool, str]:
    if not command:
        return False, "empty command"
    tokens = _command_tokens(command)
    joined = " ".join(tokens)
    for pat in DESTRUCTIVE_PATTERNS:
        if pat.search(joined):
            return False, f"destructive pattern in command: {pat.pattern}"
    lowered = [t.lower() for t in tokens]
    looks_like_python_pytest = (
        len(tokens) >= 3
        and tokens[1] == "-m"
        and tokens[2] == "pytest"
        and Path(tokens[0]).name.lower().startswith("python")
    )
    if "pytest" not in lowered and not looks_like_python_pytest:
        return False, "command is not pytest"
    for token in tokens:
        base = _token_basename(token)
        if base in FORBIDDEN_COMMAND_TOKENS:
            return False, f"forbidden token: {base}"
        if base in NETWORK_BINARIES:
            return False, f"network binary: {base}"
    return True, "pytest-only"


def _network_binaries_in_command(command: list[str]) -> list[str]:
    found: list[str] = []
    for token in _command_tokens(command):
        base = _token_basename(token)
        if base in NETWORK_BINARIES:
            found.append(base)
    return found


def _env_has_secrets(env: dict[str, str] | None = None) -> list[str]:
    source = env if env is not None else os.environ
    hits: list[str] = []
    for key in source:
        upper = key.upper()
        if any(frag in upper for frag in SECRET_ENV_FRAGMENTS):
            hits.append(key)
    return hits


def _isolation_contains_secrets(isolated_root: Path) -> list[str]:
    found: list[str] = []
    if not isolated_root.exists():
        return found
    for path in isolated_root.rglob("*"):
        if path.is_file() and is_secret_path(path):
            found.append(str(path.relative_to(isolated_root)))
    return found


def _looks_like_live_root(isolated_root: Path, live_root: Path) -> bool:
    try:
        return isolated_root.resolve() == live_root.resolve()
    except OSError:
        return False


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _flag_value(tokens: list[str], index: int) -> tuple[str | None, int]:
    tok = tokens[index]
    if "=" in tok and not tok.startswith("-o"):
        return tok.split("=", 1)[1], index + 1
    if index + 1 < len(tokens):
        return tokens[index + 1], index + 2
    return None, index + 1


def _pytest_path_violations(
    command: list[str],
    isolated_root: Path,
    cwd: Path | None,
) -> list[str]:
    """Block pytest targets, --rootdir, and cwd outside the isolated tree.

    Compare resolved paths, not string prefixes. cache_dir / --basetemp may
    live beside the copy.
    """
    iso = isolated_root.resolve()
    work = (cwd or isolated_root).resolve()
    blocked: list[str] = []
    if not _is_inside(work, iso):
        blocked.append(f"cwd is outside isolated_root: {work}")

    skip_value_flags = {
        "--basetemp",
        "--override-ini",
        "-o",
        "-c",
        "-p",
        "-k",
        "-m",
        "-n",
        "--maxfail",
        "--timeout",
        "--tb",
        "-o",
    }
    confine_flags = {"--rootdir", "--confcutdir", "--pyargs"}

    i = 0
    while i < len(command) and command[i] != "pytest" and Path(command[i]).name != "pytest":
        i += 1
    if i < len(command):
        i += 1

    while i < len(command):
        tok = command[i]
        name = tok.split("=", 1)[0]
        if name in confine_flags or tok.startswith("--rootdir=") or tok.startswith("--confcutdir="):
            value, i = _flag_value(command, i)
            if value:
                target = Path(value)
                if not target.is_absolute():
                    target = work / value
                if not _is_inside(target, iso):
                    blocked.append(f"pytest rootdir/target is outside isolated_root: {target.resolve()}")
            continue
        if name in skip_value_flags or tok.startswith("--basetemp=") or tok.startswith("-o"):
            if tok == "-o" or tok in skip_value_flags and "=" not in tok:
                i += 2
            else:
                i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        target = Path(tok)
        if not target.is_absolute():
            target = work / tok
        if not _is_inside(target, iso):
            blocked.append(f"pytest target is outside isolated_root: {target.resolve()}")
        i += 1
    return blocked


FORBIDDEN_IMPORT_ROOTS = ("subprocess", "socket", "httpx", "requests", "urllib")
FORBIDDEN_ATTR_CALLS = {
    ("shutil", "rmtree"),
    ("os", "system"),
    ("os", "popen"),
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "removedirs"),
    ("pathlib.Path", "unlink"),
    ("Path", "unlink"),
}
NETWORK_NAME_FRAGMENTS = ("urlopen", "request", "get", "post", "connect")
# Roots whose only reason to be blocked is that they reach the network. When a
# project's own top-level package shares one of these names (e.g. the httpx
# test suite importing httpx), the import is a self-import, not network access,
# so it is exempted. subprocess is never exempted (it is not a network root).
NETWORK_FORBIDDEN_ROOTS = {"socket", "httpx", "requests", "urllib"}
# Builtins that are dangerous in any file but can only ever be a sandbox WARN
# (never BLOCK) in non-test source when the bwrap sandbox is active.
BUILTIN_TRIO = {"eval", "exec", "__import__"}


def _own_package_names(isolated_root: Path) -> set[str]:
    """Import names that resolve to a real package directory inside the copy.

    Only actual packages count - a directory holding ``__init__.py`` under
    ``src/`` or at the copy root. The declared ``[project].name`` in
    pyproject.toml is deliberately **not** consulted: a distribution that
    merely calls itself ``requests`` or ``httpx`` would otherwise exempt every
    real ``import requests`` in its test suite from the network guard, which is
    the bypass this exemption exists to close.
    """
    names: set[str] = set()
    for base in (isolated_root / "src", isolated_root):
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and (child / "__init__.py").is_file():
                names.add(child.name)
    return names


def _is_self_network_import(name: str, own: set[str]) -> bool:
    root = name.split(".")[0]
    return root in NETWORK_FORBIDDEN_ROOTS and root in own

def _is_test_file(path: Path, isolated_root: Path) -> bool:
    rel = path.relative_to(isolated_root) if path.is_relative_to(isolated_root) else path
    parts = {p.lower() for p in Path(rel).parts}
    name = path.name.lower()
    return (
        name.startswith("test_")
        or name.endswith("_test.py")
        or "tests" in parts
        or name == "conftest.py"
    )


def _pytest_norecursedirs(isolated_root: Path) -> set[str]:
    ini = isolated_root / "pytest.ini"
    ignored = {".venv", "venv", ".git", "__pycache__", ".pytest_cache", "reports"}
    if ini.is_file():
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip().startswith("norecursedirs"):
                _, _, rest = line.partition("=")
                ignored |= set(rest.strip().split())
                break
    return ignored


def _all_python_files(isolated_root: Path) -> list[Path]:
    files: list[Path] = []
    if not isolated_root.is_dir():
        return files
    ignored = _pytest_norecursedirs(isolated_root)
    for path in isolated_root.rglob("*.py"):
        rel = path.relative_to(isolated_root) if path.is_relative_to(isolated_root) else path
        if any(part in ignored for part in Path(rel).parts):
            continue
        files.append(path)
    return files


def _test_python_files(isolated_root: Path) -> list[Path]:
    return [p for p in _all_python_files(isolated_root) if _is_test_file(p, isolated_root)]


def _call_qualname(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return _call_qualname(node.value) + (node.attr,)
    return ()


def _const_str(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _arg_looks_tmp(call: ast.Call) -> bool:
    if not call.args:
        return False
    text = _const_str(call.args[0])
    if text is None:
        return False
    try:
        parts = [p.lower() for p in Path(text).parts]
    except Exception:
        return False
    if ".." in parts:
        return False
    return any(p in {"tmp", "temp"} for p in parts)


def _normalize_rel(path: str) -> str:
    return Path(path).as_posix().lstrip("./")


def valid_allow_entries(raw: Any) -> list[dict[str, str]]:
    """Keep only exceptions that name path, call, and a non-empty reason."""
    entries: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return entries
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = _normalize_rel(str(item.get("path") or "").strip())
        call = str(item.get("call") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if path and call and reason:
            entries.append({"path": path, "call": call, "reason": reason})
    return entries


def _match_allow(rel: str, call: str, allows: list[dict[str, str]]) -> dict[str, str] | None:
    rel_n = _normalize_rel(rel)
    for entry in allows:
        if entry["path"] != rel_n:
            continue
        if entry["call"] == call:
            return entry
        # import subprocess is covered by an allowed subprocess.run in the same file
        if call == "subprocess" and (
            entry["call"] == "subprocess" or entry["call"].startswith("subprocess.")
        ):
            return entry
    return None


DELETE_CALLS = {"remove", "unlink", "rmtree", "removedirs"}
SANDBOX_DELETE_REASON = "file deletion is confined to the isolated copy by the Bubblewrap sandbox"


def scan_tests_for_risks(
    isolated_root: Path,
    allow_in_tests: Any = None,
    sandboxed: bool = False,
    only_paths: set[str] | None = None,
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """Static AST pass over the isolated copy.

    Returns ``(hits, allowed, warnings)``. ``hits`` block the run; ``allowed``
    lists operator- or sandbox-permitted calls; ``warnings`` are dangerous
    operations that are only reported (never vetoed) because the bwrap sandbox
    is active and confines them to the isolated copy:

    - test files: subprocess/socket/httpx/requests/urllib imports & calls,
      network-name calls, and eval/exec/__import__ become WARN when sandboxed;
    - non-test source: only eval/exec/__import__ can become WARN when sandboxed
      (everything else in source still blocks).

    Without an active sandbox the behavior is unchanged: these are blocking
    hits. Each message is tagged with the file's role (``in tests``/``in
    source``) so a non-test finding is no longer mislabeled as "in tests".
    """
    allows = valid_allow_entries(allow_in_tests)
    own = _own_package_names(isolated_root)
    hits: list[str] = []
    allowed: list[dict[str, str]] = []
    warnings: list[str] = []
    seen_allowed: set[tuple[str, str, str]] = set()
    for path in _all_python_files(isolated_root):
        rel = str(path.relative_to(isolated_root)).replace("\\", "/")
        if only_paths is not None and rel not in only_paths and path.name != "conftest.py":
            # Diff mode: only the changed files are re-scanned, but every
            # conftest.py stays in scope - it can change how tests elsewhere
            # are collected, so skipping it would hide a real risk.
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError) as exc:
            hits.append(f"{path.name}: cannot parse ({exc})")
            continue
        is_test = _is_test_file(path, isolated_root)
        role_tag = f" [in {'tests' if is_test else 'source'}]"

        def _record(
            call: str,
            message: str,
            *,
            kind: str = "dangerous",
            source_warnable: bool = False,
        ) -> None:
            match = _match_allow(rel, call, allows)
            if match:
                key = (match["path"], match["call"], match["reason"])
                if key not in seen_allowed:
                    seen_allowed.add(key)
                    allowed.append(
                        {
                            "status": "ALLOWED",
                            "path": match["path"],
                            "call": match["call"],
                            "reason": match["reason"],
                        }
                    )
                return
            if sandboxed and kind == "delete":
                key = (rel, call, SANDBOX_DELETE_REASON)
                if key not in seen_allowed:
                    seen_allowed.add(key)
                    allowed.append(
                        {"status": "ALLOWED", "path": rel, "call": call, "reason": SANDBOX_DELETE_REASON}
                    )
                return
            if sandboxed and kind == "dangerous" and (is_test or source_warnable):
                warnings.append(message + role_tag)
                return
            hits.append(message + role_tag)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if is_test:
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if alias.name.startswith(FORBIDDEN_IMPORT_ROOTS) or root in FORBIDDEN_IMPORT_ROOTS:
                            if _is_self_network_import(alias.name, own):
                                continue
                            _record(alias.name, f"{rel}: import {alias.name}", kind="dangerous")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                root = mod.split(".")[0]
                if is_test and (mod.startswith(FORBIDDEN_IMPORT_ROOTS) or root in FORBIDDEN_IMPORT_ROOTS):
                    if not _is_self_network_import(mod, own):
                        _record(mod or root, f"{rel}: from {mod} import ...", kind="dangerous")
                if root == "shutil":
                    for alias in node.names:
                        if alias.name == "rmtree":
                            _record("shutil.rmtree", f"{rel}: from shutil import rmtree", kind="delete")
                elif root == "os":
                    for alias in node.names:
                        if alias.name in {"system", "popen", "remove", "unlink"}:
                            _record(
                                f"os.{alias.name}",
                                f"{rel}: from os import {alias.name}",
                                kind="delete" if alias.name in DELETE_CALLS else "dangerous",
                            )
            elif isinstance(node, ast.Call):
                parts = _call_qualname(node.func)
                if parts and parts[-1] in BUILTIN_TRIO and (len(parts) == 1 or parts[0] in {"builtins", "__builtin__"}):
                    _record(parts[-1], f"{rel}: call {'.'.join(parts)}", kind="dangerous", source_warnable=True)
                elif len(parts) >= 2:
                    pair = (parts[0], parts[-1])
                    if pair in FORBIDDEN_ATTR_CALLS or (parts[0], parts[1]) in FORBIDDEN_ATTR_CALLS:
                        if parts[-1] in {"remove", "unlink"} and _arg_looks_tmp(node):
                            continue
                        _record(
                            ".".join(parts),
                            f"{rel}: call {'.'.join(parts)}",
                            kind="delete" if parts[-1] in DELETE_CALLS else "dangerous",
                        )
                elif len(parts) == 1 and parts[0] in {"rmtree", "system", "popen"}:
                    _record(parts[0], f"{rel}: call {parts[0]}", kind="delete" if parts[0] in DELETE_CALLS else "dangerous")
                elif len(parts) == 1 and parts[0] in {"remove", "unlink"}:
                    if not _arg_looks_tmp(node):
                        _record(parts[0], f"{rel}: call {parts[0]}", kind="delete")
                if is_test:
                    if parts and parts[0] in {"subprocess", "socket", "httpx", "requests"}:
                        if _is_self_network_import(parts[0], own):
                            pass
                        else:
                            _record(".".join(parts), f"{rel}: call {'.'.join(parts)}", kind="dangerous")
                    if (
                        parts
                        and parts[-1] in NETWORK_NAME_FRAGMENTS
                        and parts[0] in {"urllib", "requests", "httpx", "socket"}
                        and not _is_self_network_import(parts[0], own)
                    ):
                        _record(".".join(parts), f"{rel}: network call {'.'.join(parts)}", kind="dangerous")
    return hits, allowed, warnings


RUNTIME_GUARD_MODULE = '''\
"""Injected by Sable. Blocks outbound sockets during isolated pytest."""

from __future__ import annotations

import os
import socket

if os.environ.get("NO_NETWORK") == "1":
    def _blocked(*_args, **_kwargs):
        raise RuntimeError("Sable: socket.connect is blocked in the isolated copy")

    socket.socket.connect = _blocked
    socket.socket.connect_ex = _blocked
'''


def install_runtime_guards(work_dir: Path) -> Path:
    """Write a pytest plugin module into work_dir that monkeypatches socket on load."""
    dest = work_dir / "_sable_guard.py"
    dest.write_text(RUNTIME_GUARD_MODULE, encoding="utf-8")
    return dest


def evaluate_preflight(
    *,
    command: list[str],
    isolated_root: Path,
    live_root: Path,
    allow_network: bool,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    allow_in_tests: Any = None,
    sandboxed: bool = False,
    only_paths: set[str] | None = None,
) -> PolicyDecision:
    """Return CLEAR or BLOCK. Hierarchy: Sable outranks other roles."""
    checks: dict[str, bool] = {}
    blocked: list[str] = []

    pytest_ok, reason = _command_is_pytest_only(command)
    checks["pytest_only"] = pytest_ok
    if not pytest_ok:
        blocked.append(reason)

    secret_keys = _env_has_secrets(env)
    # Passing a fully sanitized env (empty of secrets) is CLEAR even if the
    # parent process has secrets — Sable cares about what Nova would inherit.
    checks["no_secret_env"] = len(secret_keys) == 0
    if secret_keys:
        blocked.append("secret-looking environment keys present: " + ", ".join(secret_keys[:12]))

    leaked = _isolation_contains_secrets(isolated_root)
    checks["no_secret_files"] = len(leaked) == 0
    if leaked:
        blocked.append("secret-looking files in isolated copy: " + ", ".join(leaked[:12]))

    not_live = not _looks_like_live_root(isolated_root, live_root)
    checks["not_live_tree"] = not_live
    if not not_live:
        blocked.append("refusing to run against the live working tree")

    checks["isolated_exists"] = isolated_root.is_dir()
    if not isolated_root.is_dir():
        blocked.append("isolated root is missing")

    path_hits = _pytest_path_violations(command, isolated_root, cwd)
    checks["target_inside_isolated"] = len(path_hits) == 0
    blocked.extend(path_hits)

    ast_hits, allowed, ast_warnings = scan_tests_for_risks(
        isolated_root, allow_in_tests=allow_in_tests, sandboxed=sandboxed, only_paths=only_paths
    )
    checks["tests_ast_clean"] = len(ast_hits) == 0
    if ast_hits:
        blocked.append("dangerous operations in isolated copy: " + "; ".join(ast_hits[:8]))
    if ast_warnings:
        checks["tests_ast_warned"] = True

    if not allow_network:
        net_bins = _network_binaries_in_command(command)
        checks["network_denied"] = len(net_bins) == 0
        if net_bins:
            blocked.append(
                "network utilities present while allow_network is false: "
                + ", ".join(net_bins)
            )
    else:
        checks["network_denied"] = True

    verdict = Verdict.BLOCK.value if blocked else Verdict.CLEAR.value
    return PolicyDecision(
        verdict=verdict,
        blocked_reasons=blocked,
        checks=checks,
        command=list(command),
        allowed=allowed,
        warnings=ast_warnings,
    )


def evaluate_post_execution(*, collected: int, command: list[str]) -> PolicyDecision:
    blocked: list[str] = []
    checks = {"collected_gt_zero": collected > 0}
    if collected <= 0:
        blocked.append("pytest collected 0 tests")
    pytest_ok, reason = _command_is_pytest_only(command)
    checks["pytest_only"] = pytest_ok
    if not pytest_ok:
        blocked.append(reason)
    verdict = Verdict.BLOCK.value if blocked else Verdict.CLEAR.value
    return PolicyDecision(
        verdict=verdict,
        blocked_reasons=blocked,
        checks=checks,
        command=list(command),
    )


def default_command(
    isolated_root: Path,
    work_dir: Path,
    test_targets: list[str] | None = None,
) -> list[str]:
    return build_pytest_command(
        isolated_root,
        cache_dir=work_dir / "pytest_cache",
        basetemp=work_dir / "pytest_tmp",
        extra_args=["-p", "_sable_guard"],
        test_targets=test_targets,
    )

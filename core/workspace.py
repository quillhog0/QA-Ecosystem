"""Monorepo and workspace discovery, per-package engine checks, and finding attribution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from core.toolchain import check_node_engine, detect_ts_runner

_SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    "tests",
    "test",
    "docs",
    "fixtures",
    "seeded",
    "benchmark",
    "examples",
    "reports",
    "scratch",
    "dist",
    "build",
}

_CONTAINER_DIRS = ("packages", "apps", "services", "libs", "modules", "crates")


def _extract_workspace_globs(project_root: Path) -> list[str]:
    globs: list[str] = []
    pkg_json = project_root / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            data = {}
        if isinstance(data, dict):
            ws = data.get("workspaces")
            if isinstance(ws, dict):
                ws = ws.get("packages")
            if isinstance(ws, list):
                globs.extend(str(item).strip() for item in ws if str(item).strip())

    pnpm_yaml = project_root / "pnpm-workspace.yaml"
    if pnpm_yaml.is_file():
        try:
            loaded = yaml.safe_load(pnpm_yaml.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            loaded = {}
        if isinstance(loaded, dict):
            pkgs = loaded.get("packages")
            if isinstance(pkgs, list):
                globs.extend(str(item).strip() for item in pkgs if str(item).strip())
    return list(dict.fromkeys(globs))


def _inspect_package_dir(project_root: Path, pkg_dir: Path) -> dict[str, Any] | None:
    rel_posix = pkg_dir.relative_to(project_root).as_posix()
    if any(part in _SKIP_DIRS for part in Path(rel_posix).parts):
        return None

    pkg_json = pkg_dir / "package.json"
    if pkg_json.is_file():
        try:
            data = json.loads(pkg_json.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        name = str(data.get("name", pkg_dir.name)).strip()
        engines = data.get("engines") if isinstance(data.get("engines"), dict) else {}
        r_info = detect_ts_runner(pkg_dir)
        runner_name = str(r_info.get("runner", "unknown")) if isinstance(r_info, dict) else str(r_info)
        return {
            "name": name,
            "path": rel_posix,
            "language": "ts",
            "runner": runner_name,
            "engines": dict(engines),
        }

    pyproject = pkg_dir / "pyproject.toml"
    if any((pyproject.is_file(), (pkg_dir / "setup.py").is_file())):
        name = pkg_dir.name
        engines: dict[str, str] = {}
        if pyproject.is_file():
            text = pyproject.read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                stripped = line.strip()
                if all((stripped.startswith("name"), "=" in stripped)):
                    val = stripped.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        name = val
                        break
            for line in text.splitlines():
                stripped = line.strip()
                if all((stripped.startswith("requires-python"), "=" in stripped)):
                    val = stripped.split("=", 1)[1].strip().strip("\"'")
                    if val:
                        engines["python"] = val
                        break
        return {
            "name": name,
            "path": rel_posix,
            "language": "python",
            "runner": "pytest",
            "engines": engines,
        }

    return None


def discover_workspaces(project_root: Path) -> list[dict[str, Any]]:
    """Discover monorepo/workspace packages under ``project_root``."""
    project_root = Path(project_root)
    candidate_dirs: list[Path] = []
    for pattern in _extract_workspace_globs(project_root):
        for match in sorted(project_root.glob(pattern.rstrip("/"))):
            if all((match.is_dir(), match != project_root)):
                candidate_dirs.append(match)

    for container in _CONTAINER_DIRS:
        c_dir = project_root / container
        if c_dir.is_dir():
            for child in sorted(c_dir.iterdir()):
                if child.is_dir():
                    candidate_dirs.append(child)

    seen_paths: set[str] = set()
    workspaces: list[dict[str, Any]] = []
    for pkg_dir in candidate_dirs:
        info = _inspect_package_dir(project_root, pkg_dir)
        if info is not None:
            if info["path"] not in seen_paths:
                seen_paths.add(info["path"])
                workspaces.append(info)

    workspaces.sort(key=lambda w: str(w["path"]))
    return workspaces


def check_workspace_engines(
    project_root: Path,
    workspaces: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Verify Node engine constraints for each discovered workspace package."""
    project_root = Path(project_root)
    for ws in workspaces:
        if ws.get("language") == "ts":
            ok, msg = check_node_engine(project_root / str(ws["path"]))
            if not ok:
                return bool(), f"workspace package '{ws['name']}' ({ws['path']}): {msg}"
    return not bool(), ""


def assign_finding_packages(
    findings_list: list[dict[str, Any]],
    workspaces: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Annotate each finding with ``package`` when its file belongs to a workspace package."""
    for finding in findings_list:
        if not isinstance(finding, dict):
            continue
        loc = str(finding.get("file", finding.get("code_file", ""))).replace("\\", "/")
        for ws in workspaces:
            ws_path = str(ws.get("path", "")).rstrip("/")
            if any((loc.startswith(ws_path + "/"), loc == ws_path)):
                finding["package"] = str(ws.get("name", ""))
                break
    return findings_list


def compute_workspace_verdicts(
    findings_list: list[dict[str, Any]],
    workspaces: list[dict[str, Any]],
    overall_verdict: str = "CLEAR",
) -> dict[str, str]:
    """Compute per-package verdict mapping; worst package verdict wins."""
    verdicts: dict[str, str] = {}
    default_v = "BLOCKED_ENV" if overall_verdict == "BLOCKED_ENV" else "CLEAR"
    for ws in workspaces:
        verdicts[str(ws.get("name", ""))] = default_v
    for finding in findings_list:
        if isinstance(finding, dict):
            pkg = finding.get("package")
            if all((pkg in verdicts, finding.get("verdict_effect") == "VETO")):
                verdicts[str(pkg)] = "VETO"
    return verdicts

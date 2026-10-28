#!/usr/bin/env python3
"""Compare QA-Ecosystem vs Flat Baseline agent across the 24 seeded-bug projects."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.flat_baseline import run_flat_baseline
from tests.support import tree_sha256

SEEDED_DIR = ROOT / "benchmark" / "seeded"
MAIN = ROOT / "main.py"
RESULTS_MD = ROOT / "benchmark" / "results.md"


def list_seeded_projects() -> list[Path]:
    projects = []
    for path in sorted(SEEDED_DIR.iterdir()):
        if path.is_dir() and (path / "expected.yaml").is_file():
            projects.append(path)
    return projects


def load_expected(project_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((project_dir / "expected.yaml").read_text(encoding="utf-8")) or {}


def parse_stdout(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, val = line.partition("=")
            if key in {"verdict", "report", "isolated_root"} or key.startswith("ESCALATE"):
                out[key] = val
        if line.startswith("ESCALATE:"):
            out["escalation_line"] = line.strip()
    return out


def run_qa_ecosystem(project_dir: Path, reports_dir: Path, *, strict: bool = False) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(MAIN),
        "--project",
        str(project_dir),
        "--docs",
        str(project_dir / "docs"),
        "--reports-dir",
        str(reports_dir),
    ]
    if strict:
        cmd.append("--strict")
    proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
    parsed = parse_stdout(proc.stdout)
    report_path = Path(parsed.get("report") or "")
    report: dict[str, Any] = {}
    if report_path.with_suffix(".yaml").is_file():
        report = yaml.safe_load(report_path.with_suffix(".yaml").read_text(encoding="utf-8")) or {}
    verdict = report.get("verdict") or parsed.get("verdict") or "UNKNOWN"
    return {
        "verdict": verdict,
        "exit_code": proc.returncode,
        "report": report,
    }


def prepare_uncommitted_git(project_dir: Path) -> bool:
    """Setup git repo for uncommitted_bug projects."""
    git_dir = project_dir / ".git"
    from benchmark.build_seeded import PROJECTS
    spec = PROJECTS.get(project_dir.name)
    if not spec or "uncommitted_code" not in spec:
        return False

    code_file = project_dir / spec["code_name"]
    code_file.write_text(spec["committed_code"], encoding="utf-8")
    if not git_dir.exists():
        subprocess.run(["git", "init"], cwd=project_dir, check=True, capture_output=True)
        subprocess.run(["git", "-C", str(project_dir), "config", "user.name", "test"], check=True)
        subprocess.run(["git", "-C", str(project_dir), "config", "user.email", "test@test"], check=True)
    subprocess.run(["git", "-C", str(project_dir), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(project_dir), "commit", "-m", "init", "--allow-empty"],
        check=True,
        capture_output=True,
    )
    code_file.write_text(spec["uncommitted_code"], encoding="utf-8")
    return True


def cleanup_uncommitted_git(project_dir: Path) -> None:
    git_dir = project_dir / ".git"
    from benchmark.build_seeded import PROJECTS
    spec = PROJECTS.get(project_dir.name)
    if git_dir.exists():
        shutil.rmtree(git_dir, ignore_errors=True)
    if spec and "uncommitted_code" in spec:
        code_file = project_dir / spec["code_name"]
        code_file.write_text(spec["uncommitted_code"], encoding="utf-8")


def compare_all(reports_dir: Path | None = None) -> tuple[list[dict[str, Any]], int, int, int]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rep_dir = reports_dir if reports_dir else ROOT / "reports" / f"compare-{ts}"
    rep_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    default_caught_count = 0
    strict_caught_count = 0
    flat_caught_count = 0

    projects = list_seeded_projects()
    for proj in projects:
        expected = load_expected(proj)
        category = expected.get("category", "")

        is_uncommitted = (category == "uncommitted_bug")
        if is_uncommitted:
            prepare_uncommitted_git(proj)

        try:
            # 1. Default QA-Ecosystem
            def_res = run_qa_ecosystem(proj, rep_dir / "default" / proj.name, strict=False)
            def_verdict = def_res["verdict"]
            def_caught = def_verdict != "CLEAR"
            if def_caught:
                default_caught_count += 1

            # 2. Strict QA-Ecosystem (riley.veto_on_uncovered_claims enabled via CLI/system)
            strict_res = run_qa_ecosystem(proj, rep_dir / "strict" / proj.name, strict=True)
            strict_verdict = strict_res["verdict"]
            strict_caught = strict_verdict != "CLEAR"
            if strict_caught:
                strict_caught_count += 1

            # 3. Flat Baseline
            flat_res = run_flat_baseline(proj)
            flat_verdict = flat_res["verdict"]
            flat_caught = flat_verdict == "VETO"
            if flat_caught:
                flat_caught_count += 1

            rows.append({
                "project": proj.name,
                "category": category,
                "default_verdict": def_verdict,
                "strict_verdict": strict_verdict,
                "flat_verdict": flat_verdict,
                "default_caught": def_caught,
                "strict_caught": strict_caught,
                "flat_caught": flat_caught,
            })
        finally:
            if is_uncommitted:
                cleanup_uncommitted_git(proj)

    return rows, default_caught_count, strict_caught_count, flat_caught_count


def render_markdown(
    rows: list[dict[str, Any]],
    default_count: int,
    strict_count: int,
    flat_count: int,
) -> str:
    total = len(rows)
    def_pct = int(round((default_count / total) * 100)) if total else 0
    strict_pct = int(round((strict_count / total) * 100)) if total else 0
    flat_pct = int(round((flat_count / total) * 100)) if total else 0

    lines = [
        "# Comparison: QA-Ecosystem vs Flat Baseline",
        "",
        f"**Summary:** default {default_count}/{total} ({def_pct} %), strict (riley.veto_on_uncovered_claims enabled via --strict) {strict_count}/{total} ({strict_pct} %), flat {flat_count}/{total} ({flat_pct} %)",
        "",
        "| projekt | druh chyby | default | strict | flat |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['project']} | {r['category']} | {r['default_verdict']} | {r['strict_verdict']} | {r['flat_verdict']} |"
        )
    lines.append("")
    lines.append(
        f"**Result:** default {default_count}/{total}, strict (riley.veto_on_uncovered_claims enabled via --strict) {strict_count}/{total}, flat {flat_count}/{total}."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run comparison on 24 seeded projects")
    parser.add_argument("--reports-dir", default="", help="Reports dir for QA-Ecosystem runs")
    args = parser.parse_args(argv)

    rep_dir = Path(args.reports_dir) if args.reports_dir else None
    rows, default_count, strict_count, flat_count = compare_all(rep_dir)
    total = len(rows)

    md = render_markdown(rows, default_count, strict_count, flat_count)
    RESULTS_MD.write_text(md, encoding="utf-8")
    print(f"default {default_count}/{total}, strict (riley.veto_on_uncovered_claims enabled via --strict) {strict_count}/{total}, flat {flat_count}/{total}")
    print()
    print(md)
    print(f"\nReport written to {RESULTS_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


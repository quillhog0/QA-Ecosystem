#!/usr/bin/env python3
"""Run every fixture through main.py and compare against expected.yaml."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support import tree_sha256  # noqa: E402

FIXTURES = ROOT / "benchmark" / "fixtures"
MAIN = ROOT / "main.py"


def list_fixtures() -> list[Path]:
    rows = []
    for path in sorted(FIXTURES.iterdir()):
        if path.is_dir() and (path / "expected.yaml").is_file():
            rows.append(path)
    return rows


def load_expected(fixture: Path) -> dict:
    data = yaml.safe_load((fixture / "expected.yaml").read_text(encoding="utf-8")) or {}
    return data


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


def run_fixture(fixture: Path, reports_dir: Path) -> dict:
    docs = fixture / "docs"
    cmd = [
        sys.executable,
        str(MAIN),
        "--project",
        str(fixture),
        "--docs",
        str(docs),
        "--reports-dir",
        str(reports_dir),
    ]
    expected_data = load_expected(fixture)
    cmd.extend(expected_data.get("args") or [])
    import shutil
    import subprocess

    cleanup_git = False
    if fixture.name == "uncommitted_bug":
        git_dir = fixture / ".git"
        calc = fixture / "calculator.py"
        calc.write_text("def add(left: int, right: int) -> int:\n    return left + right\n", encoding="utf-8")
        if not git_dir.exists():
            subprocess.run(["git", "init"], cwd=fixture, check=True, capture_output=True)
            subprocess.run(["git", "-C", str(fixture), "config", "user.name", "test"], check=True)
            subprocess.run(["git", "-C", str(fixture), "config", "user.email", "test@test"], check=True)
            cleanup_git = True
        subprocess.run(["git", "-C", str(fixture), "add", "-A"], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(fixture), "commit", "-m", "init", "--allow-empty"],
            check=True,
            capture_output=True,
        )
        calc.write_text("def add(left: int, right: int) -> int:\n    return left - right\n", encoding="utf-8")

    try:
        before = tree_sha256(fixture)
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, check=False)
        after = tree_sha256(fixture)
    finally:
        if cleanup_git and (fixture / ".git").exists():
            shutil.rmtree(fixture / ".git", ignore_errors=True)
            (fixture / "calculator.py").write_text("def add(left: int, right: int) -> int:\n    return left - right\n", encoding="utf-8")
    parsed = parse_stdout(proc.stdout)
    report_path = Path(parsed.get("report") or "")
    report: dict = {}
    if report_path.with_suffix(".yaml").is_file():
        report = yaml.safe_load(report_path.with_suffix(".yaml").read_text(encoding="utf-8")) or {}
    elif report_path.is_file() and report_path.suffix == ".md":
        yml = report_path.with_suffix(".yaml")
        if yml.is_file():
            report = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    isolated = Path(report.get("isolated_root") or parsed.get("isolated_root") or "")
    skipped: list[str] = []
    for item in report.get("role_results") or []:
        details = item.get("details") or {}
        if "skipped_secrets" in details:
            skipped = list(details.get("skipped_secrets") or [])
            break
    install_status = "skipped"
    if "execution" in report and "install_status" in report["execution"]:
        install_status = report["execution"]["install_status"]

    return {
        "name": fixture.name,
        "exit_code": proc.returncode,
        "verdict": report.get("verdict") or parsed.get("verdict") or "",
        "escalation": report.get("escalation") or parsed.get("escalation_line") or "",
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "live_hash_unchanged": before == after,
        "isolated_root": str(isolated),
        "skipped_secrets": skipped,
        "report": report,
        "install_status": install_status,
        "root_cause": str(
            (report.get("post_audit") or {}).get("root_cause")
            or (report.get("post_audit") or {}).get("riley", {}).get("root_cause")
            or report.get("root_cause")
            or ""
        ),
    }


def evaluate(result: dict, expected: dict) -> tuple[bool, str]:
    accepted = expected.get("accept_verdicts") or [expected.get("verdict")]
    accepted = [str(v) for v in accepted if v]
    problems: list[str] = []
    if str(result["verdict"]) not in accepted:
        problems.append(f"verdict {result['verdict']!r} not in {accepted}")
    exp_esc = expected.get("escalation") or ""
    if str(result.get("escalation") or "") != str(exp_esc):
        problems.append(f"escalation {result.get('escalation')!r} != {exp_esc!r}")
    if int(result["exit_code"]) != int(expected.get("exit_code", 0)):
        problems.append(f"exit {result['exit_code']} != {expected.get('exit_code')}")
    exp_install_status = expected.get("install_status")
    if exp_install_status and str(result.get("install_status") or "") != str(exp_install_status):
        problems.append(f"install_status {result.get('install_status')!r} != {exp_install_status!r}")
    exp_root_cause = expected.get("root_cause")
    if exp_root_cause:
        actual_root_cause = str(
            result.get("root_cause")
            or ((result.get("report") or {}).get("post_audit") or {}).get("root_cause")
            or ((result.get("report") or {}).get("post_audit") or {}).get("riley", {}).get("root_cause")
            or (result.get("report") or {}).get("root_cause")
            or ""
        )
        if str(exp_root_cause) not in actual_root_cause:
            problems.append(f"root_cause {exp_root_cause!r} not in {actual_root_cause!r}")
    if expected.get("assert_live_hash") and not result["live_hash_unchanged"]:
        problems.append("live tree hash changed")
    needle = expected.get("assert_secret_absent")
    if needle:
        iso = Path(result["isolated_root"])
        if iso.is_dir() and (iso / needle).exists():
            problems.append(f"{needle} present in isolated copy")
    for item in expected.get("skipped_secrets_contains") or []:
        skipped = result.get("skipped_secrets") or []
        if item not in skipped:
            problems.append(f"{item!r} missing from skipped_secrets={skipped}")
    ok = not problems
    return ok, "; ".join(problems) if problems else "OK"


def render_table(rows: list[dict]) -> str:
    lines = [
        "| fixture | expected | result | status |",
        "|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['expected']} | {row['actual']} | {row['status']} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QA ecosystem fixture benchmark")
    parser.add_argument("--reports-dir", default="", help="Where to write per-fixture reports")
    args = parser.parse_args(argv)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    reports_dir = Path(args.reports_dir) if args.reports_dir else ROOT / "reports" / f"fx-runs-{ts}"
    reports_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    all_ok = True
    for fixture in list_fixtures():
        expected = load_expected(fixture)
        result = run_fixture(fixture, reports_dir / fixture.name)
        ok, detail = evaluate(result, expected)
        status = "OK" if ok else "FAIL"
        if not ok:
            all_ok = False
        exp_verdict = expected.get("verdict")
        if expected.get("accept_verdicts"):
            exp_verdict = "/".join(str(v) for v in expected["accept_verdicts"])
        rows.append(
            {
                "name": fixture.name,
                "expected": f"{exp_verdict} exit={expected.get('exit_code')}",
                "actual": f"{result['verdict']} exit={result['exit_code']}",
                "status": status,
                "detail": detail,
                "ok": ok,
            }
        )

    table = render_table(rows)
    md_lines = [
        f"# Benchmark {ts}",
        "",
        table,
        "",
        "## Details",
        "",
    ]
    for row in rows:
        md_lines.append(f"- **{row['name']}**: {row['status']} — {row['detail']}")
    md_lines.append("")
    out = ROOT / "reports" / f"benchmark-{ts}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md_lines), encoding="utf-8")
    print(table)
    print(f"report={out}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Field test: run main.py against pinned public repos and compare expectations.

Network policy
--------------
The only step that touches the network is ``ensure_repo``: a shallow,
commit-pinned ``git fetch`` into the cache directory. It runs here, in the
harness, *outside* every QA cycle. Each ``main.py`` invocation keeps the default
``allow_network=false`` and never builds or installs the project under test.

``--repos-dir`` reuses existing clones verbatim (they are only read, never
checked out or modified) and ``--offline`` refuses to fetch anything, so a full
survey runs with no network access at all::

    python benchmark/field_test.py --repos-dir ~/qa-field-test --offline

Reports land in ``reports/field-test-<ts>.md``; the per-repo QA reports go to
``reports/field-runs-<ts>/<repo>/``. Row statuses are ``OK``, ``OBSERVED`` (ran,
no expectation recorded), ``REGRESSION`` (a measured verdict disagrees) and
``ERROR`` (nothing was measured: no clone, wrong commit, no docs). Exit code is 0
only when no row regressed and none errored.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.run_benchmark import parse_stdout  # noqa: E402

MAIN = ROOT / "main.py"
MANIFEST = ROOT / "benchmark" / "field_repos.yaml"
EXPECTED = ROOT / "benchmark" / "field_expected.yaml"
DOC_CANDIDATES = ("README.md", "README.rst", "README.txt")
COUNT_KEYS = ("collected", "passed", "failed", "skipped", "errors")
# The manifest is a boundary, not a suggestion: `name` becomes a directory under
# the clone root, `sha` and `url` become git arguments. Each is confined to a
# shape that cannot carry an option or walk out of the clone root.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
URL_PREFIX = "https://"


def load_manifest(path: Path) -> list[dict]:
    """Read and validate the pinned repo list. No network, no subprocess."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise SystemExit(f"manifest must hold a non-empty 'repos' list: {path}")
    rows: list[dict] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise SystemExit(f"manifest entry must be a mapping: {item!r}")
        name = str(item.get("name") or "").strip()
        sha = str(item.get("sha") or "").strip()
        url = str(item.get("url") or "").strip()
        docs = str(item.get("docs") or "").strip()
        if not name or not sha:
            raise SystemExit(f"manifest entry needs name and sha: {item!r}")
        if not NAME_RE.match(name) or ".." in name:
            raise SystemExit(
                f"repo name must match ^[A-Za-z0-9._-]+$ without '..': {name!r}"
            )
        if not SHA_RE.match(sha):
            raise SystemExit(
                f"repo {name}: sha must be a full 40-character lowercase commit hash, "
                f"got {sha!r}"
            )
        if url and not url.startswith(URL_PREFIX):
            raise SystemExit(
                f"repo {name}: url must start with {URL_PREFIX}, got {url!r}"
            )
        if docs and (docs.startswith("/") or ".." in docs):
            raise SystemExit(
                f"repo {name}: docs must be a path inside the clone, got {docs!r}"
            )
        if name in seen:
            raise SystemExit(f"duplicate repo name in manifest: {name}")
        seen.add(name)
        rows.append({"name": name, "url": url, "sha": sha, "docs": docs})
    return rows


def load_expected(path: Path) -> dict[str, dict]:
    """Per-repo expectations. A missing file means "observe only"."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = data.get("expectations") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        raise SystemExit(f"expected file must hold an 'expectations' mapping: {path}")
    out: dict[str, dict] = {}
    for name, item in entries.items():
        if isinstance(item, str):
            item = {"verdict": item}
        if not isinstance(item, dict):
            raise SystemExit(f"expectation must be a mapping or verdict string: {name!r}")
        out[str(name)] = item
    return out


def select_entries(entries: list[dict], only: str) -> list[dict]:
    """Filter by comma-separated repo names; unknown names are an error."""
    if not only.strip():
        return list(entries)
    wanted = [part.strip() for part in only.split(",") if part.strip()]
    known = {entry["name"] for entry in entries}
    unknown = [name for name in wanted if name not in known]
    if unknown:
        raise SystemExit(f"--only names not in manifest: {', '.join(unknown)}")
    order = {name: index for index, name in enumerate(wanted)}
    return sorted((e for e in entries if e["name"] in order), key=lambda e: order[e["name"]])


def resolve_docs(repo: Path, entry: dict) -> Path | None:
    """Documentation file for the run: explicit manifest entry, else README.*."""
    if entry.get("docs"):
        path = repo / entry["docs"]
        return path if path.is_file() else None
    for candidate in DOC_CANDIDATES:
        path = repo / candidate
        if path.is_file():
            return path
    return None


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: float | None = None):
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _one_line(text: str, limit: int = 200) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[:limit] + "..."


def head_sha(repo: Path) -> str:
    proc = _run(["git", "-C", str(repo), "rev-parse", "HEAD"])
    return proc.stdout.strip() if proc.returncode == 0 else ""


def ensure_repo(
    entry: dict,
    *,
    repos_dir: Path,
    cache_dir: Path,
    offline: bool,
) -> tuple[Path | None, str]:
    """Return ``(repo path, note)``; an empty note means the pin is satisfied.

    ``repos_dir`` wins and is never written to. Otherwise the pinned commit is
    fetched shallow into ``cache_dir`` - the single network step of this script.
    With ``offline`` a missing clone is reported instead of fetched.

    The note is informational only: the caller re-checks the pin, so nothing here
    can quietly hand back a clone at another commit for a QA run.
    """
    name, sha = entry["name"], entry["sha"]
    if not SHA_RE.match(sha):
        raise SystemExit(f"refusing to fetch {name}: sha is not a full commit hash: {sha!r}")
    for base in (repos_dir, cache_dir):
        candidate = base / name
        if not candidate.is_dir():
            continue
        actual = head_sha(candidate)
        if actual == sha:
            return candidate, ""
        if base == repos_dir:
            # Reuse the clone but say so: the survey ran a different commit.
            return candidate, f"sha mismatch: HEAD {actual or 'unknown'} != pin {sha}"
    if offline:
        return None, f"no clone at {repos_dir / name} and --offline forbids fetching"
    if not entry["url"]:
        return None, f"no clone at {repos_dir / name} and manifest has no url"
    if not entry["url"].startswith(URL_PREFIX):
        raise SystemExit(f"refusing to fetch {name}: url must start with {URL_PREFIX}: {entry['url']!r}")

    target = cache_dir / name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        steps = [["git", "init", "--quiet", "--", str(target)],
                 ["git", "-C", str(target), "remote", "add", "origin", entry["url"]]]
    else:
        steps = [["git", "-C", str(target), "remote", "set-url", "origin", entry["url"]]]
    steps.append(["git", "-C", str(target), "fetch", "--depth", "1", "origin", sha])
    # No `--` before FETCH_HEAD: for checkout it starts a pathspec list, and the
    # value is a literal here, so there is nothing to separate.
    steps.append(["git", "-C", str(target), "checkout", "--quiet", "--detach", "FETCH_HEAD"])
    for cmd in steps:
        proc = _run(cmd, timeout=600)
        if proc.returncode != 0:
            return None, f"clone failed ({' '.join(cmd[:3])}): {_one_line(proc.stderr)}"
    actual = head_sha(target)
    if actual != sha:
        return target, f"sha mismatch after fetch: HEAD {actual or 'unknown'} != pin {sha}"
    return target, ""


def root_cause_of(report: dict) -> str:
    """Exact blocking reason: post-audit root cause, else first non-CLEAR role."""
    post = report.get("post_audit") or {}
    riley = post.get("riley") or {}
    for candidate in (post.get("root_cause"), riley.get("root_cause"), report.get("root_cause")):
        if candidate:
            return str(candidate)
    for item in report.get("role_results") or []:
        if str(item.get("verdict") or "") not in {"CLEAR", "PENDING", ""}:
            return str(item.get("summary") or "")
    return ""


def run_repo(repo: Path, docs: Path, reports_dir: Path, *, timeout: float) -> dict:
    cmd = [
        sys.executable,
        str(MAIN),
        "--project",
        str(repo),
        "--docs",
        str(docs),
        "--reports-dir",
        str(reports_dir),
    ]
    row: dict = {
        "verdict": "",
        "sandbox": "",
        "exit_code": -1,
        "pytest_exit": "",
        "install_status": "",
        "cause": "",
        "report": "",
        "error": "",
    }
    row.update({key: 0 for key in COUNT_KEYS})
    try:
        proc = _run(cmd, cwd=ROOT, timeout=timeout)
    except subprocess.TimeoutExpired:
        row["verdict"] = "TIMEOUT"
        row["error"] = f"main.py exceeded {timeout:.0f}s"
        return row
    parsed = parse_stdout(proc.stdout)
    # main.py may die before printing a report path; an empty name cannot be
    # given a suffix, so the YAML lookup is skipped entirely in that case.
    report_path = Path(parsed["report"]) if parsed.get("report") else None
    report: dict = {}
    if report_path is not None and report_path.with_suffix(".yaml").is_file():
        yml = report_path.with_suffix(".yaml")
        report = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
    execution = report.get("execution") or {}
    row.update(
        {
            "verdict": str(report.get("verdict") or parsed.get("verdict") or ""),
            "sandbox": str(report.get("sandbox") or ""),
            "exit_code": proc.returncode,
            "pytest_exit": execution.get("exit_code", ""),
            "install_status": str(execution.get("install_status") or ""),
            "cause": root_cause_of(report),
            "report": str(report_path) if report_path is not None else "",
            "error": _one_line(proc.stderr) if not report else "",
        }
    )
    for key in COUNT_KEYS:
        row[key] = int(execution.get(key) or 0)
    return row


def evaluate_row(row: dict, expectation: dict | None) -> tuple[str, str]:
    """Return ``(status, detail)``: OK, REGRESSION, or OBSERVED.

    Only a measured run reaches this function, so REGRESSION always means the QA
    verdict disagreed with the recorded expectation. Conditions that prevented a
    verdict (no clone, wrong commit, no docs) are ERROR and are decided by the
    caller before anything runs.
    """
    if row.get("error"):
        return "REGRESSION", str(row["error"])
    if not expectation:
        return "OBSERVED", "no expectation recorded"
    accepted = expectation.get("accept_verdicts") or [expectation.get("verdict")]
    accepted = [str(v) for v in accepted if v]
    problems: list[str] = []
    if accepted and str(row.get("verdict") or "") not in accepted:
        problems.append(f"verdict {row.get('verdict')!r} not in {accepted}")
    if "exit_code" in expectation and int(row.get("exit_code", -1)) != int(expectation["exit_code"]):
        problems.append(f"exit {row.get('exit_code')} != {expectation['exit_code']}")
    needle = expectation.get("cause")
    if needle and str(needle) not in str(row.get("cause") or ""):
        problems.append(f"cause {needle!r} not in {_one_line(row.get('cause'), 120)!r}")
    for key in COUNT_KEYS:
        minimum = expectation.get(f"min_{key}")
        if minimum is not None and int(row.get(key) or 0) < int(minimum):
            problems.append(f"{key} {row.get(key)} < {minimum}")
    if problems:
        return "REGRESSION", "; ".join(problems)
    return "OK", "matches expectation"


def render_table(rows: list[dict]) -> str:
    lines = [
        "| repo | sha | verdict | sandbox | collected | passed | failed | exit | expected | status | cause |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        expected = row.get("expected") or "-"
        cause = _one_line(row.get("cause") or row.get("note") or "", 160).replace("|", "\\|")
        lines.append(
            f"| {row['name']} | {row['sha'][:12]} | {row.get('verdict') or '-'} "
            f"| {row.get('sandbox') or '-'} | {row.get('collected', 0)} | {row.get('passed', 0)} "
            f"| {row.get('failed', 0)} | {row.get('exit_code', '-')} | {expected} "
            f"| {row['status']} | {cause or '-'} |"
        )
    return "\n".join(lines)


def render_markdown(rows: list[dict], ts: str, *, offline: bool, repos_dir: Path) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("verdict") or "-")
        counts[key] = counts.get(key, 0) + 1
    tally = ", ".join(f"{value} {name}" for name, value in sorted(counts.items()))
    lines = [
        f"# Field test {ts}",
        "",
        f"- manifest: `benchmark/field_repos.yaml` ({len(rows)} repos)",
        f"- repos dir: `{repos_dir}`",
        f"- network: {'offline (clones reused, nothing fetched)' if offline else 'shallow pinned fetch allowed for missing clones'}",
        f"- QA runs: default `config.yaml`, `dry_run=true`, `allow_network=false`, no mutation",
        f"- verdicts: {tally}",
        "",
        render_table(rows),
        "",
        "## Details",
        "",
    ]
    for row in rows:
        lines.append(f"### {row['name']}")
        lines.append("")
        lines.append(f"- pin: `{row['sha']}`")
        lines.append(f"- status: **{row['status']}** — {row.get('detail') or ''}")
        if row.get("note"):
            lines.append(f"- clone note: {row['note']}")
        lines.append(f"- docs: `{row.get('docs') or '-'}`")
        lines.append(
            f"- execution: collected={row.get('collected', 0)} passed={row.get('passed', 0)} "
            f"failed={row.get('failed', 0)} skipped={row.get('skipped', 0)} "
            f"errors={row.get('errors', 0)} pytest_exit={row.get('pytest_exit', '-')}"
        )
        lines.append(f"- install_status: `{row.get('install_status') or '-'}`")
        lines.append(f"- cause: {row.get('cause') or '-'}")
        lines.append(f"- report: `{row.get('report') or '-'}`")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QA ecosystem field test on pinned public repos")
    parser.add_argument("--manifest", default=str(MANIFEST), help="YAML list of pinned repos")
    parser.add_argument("--expected", default=str(EXPECTED), help="YAML expectations per repo")
    parser.add_argument("--repos-dir", default="~/qa-field-test", help="Existing clones to reuse (read-only)")
    parser.add_argument("--cache-dir", default="", help="Where shallow pinned clones are fetched (default reports/field-clones)")
    parser.add_argument("--reports-dir", default="", help="Where per-repo QA reports are written")
    parser.add_argument("--only", default="", help="Comma-separated repo names to run")
    parser.add_argument("--offline", action="store_true", help="Never fetch; a missing or unpinned clone is reported as an error")
    parser.add_argument("--timeout", type=float, default=900.0, help="Per-repo wall clock limit for main.py")
    args = parser.parse_args(argv)

    entries = select_entries(load_manifest(Path(args.manifest).expanduser()), args.only)
    expectations = load_expected(Path(args.expected).expanduser())
    repos_dir = Path(args.repos_dir).expanduser()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cache_dir = Path(args.cache_dir).expanduser() if args.cache_dir else ROOT / "reports" / "field-clones"
    reports_dir = Path(args.reports_dir).expanduser() if args.reports_dir else ROOT / "reports" / f"field-runs-{ts}"
    reports_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for entry in entries:
        # ERROR until a run produces a verdict: a row that never reached main.py
        # must never be counted as a passing or regressing QA result.
        row: dict = {
            "name": entry["name"],
            "sha": entry["sha"],
            "status": "ERROR",
            "expected": "",
            "note": "",
            "docs": "",
        }
        expectation = expectations.get(entry["name"])
        if expectation:
            accepted = expectation.get("accept_verdicts") or [expectation.get("verdict")]
            row["expected"] = "/".join(str(v) for v in accepted if v)
        repo, note = ensure_repo(entry, repos_dir=repos_dir, cache_dir=cache_dir, offline=args.offline)
        row["note"] = note
        if repo is None:
            row["detail"] = note or "no clone available"
            rows.append(row)
            continue
        # The pin is verified here rather than inferred from the note wording, so a
        # clone at any other commit is never handed to main.py.
        actual = head_sha(repo)
        if actual != entry["sha"]:
            row["detail"] = (
                f"pin not satisfied: HEAD {actual or 'unknown'} != {entry['sha']}; "
                "refused to QA a repository at another commit"
            )
            rows.append(row)
            continue
        docs = resolve_docs(repo, entry)
        if docs is None:
            row["detail"] = f"docs file not found in {repo} (manifest docs={entry['docs'] or 'auto'})"
            rows.append(row)
            continue
        row["docs"] = str(docs)
        result = run_repo(repo, docs, reports_dir / entry["name"], timeout=args.timeout)
        row.update(result)
        status, detail = evaluate_row(row, expectation)
        row["status"], row["detail"] = status, detail
        rows.append(row)

    table = render_table(rows)
    out = ROOT / "reports" / f"field-test-{ts}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_markdown(rows, ts, offline=args.offline, repos_dir=repos_dir), encoding="utf-8"
    )
    print(table)
    ok = sum(1 for row in rows if row["status"] == "OK")
    observed = sum(1 for row in rows if row["status"] == "OBSERVED")
    errored = sum(1 for row in rows if row["status"] == "ERROR")
    bad = len(rows) - ok - observed - errored
    print(f"summary={ok} OK / {observed} OBSERVED / {bad} REGRESSION / {errored} ERROR")
    print(f"report={out}")
    return 0 if bad == 0 and errored == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
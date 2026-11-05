#!/usr/bin/env python3
"""Entry point: python main.py --project PATH --docs PATH"""

from __future__ import annotations

import os
import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.orchestrator import Vesper
from core.report import exit_code_for
from core.sarif import write_sarif


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise SystemExit(f"config must be a mapping: {path}")
    return loaded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Deterministic QA control plane. Runs in an isolated copy. Does not patch code."
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run the QA cycle against this repository (README.md + schemas/ as docs)",
    )
    parser.add_argument("--project", default="", help="Path to the project under test")
    parser.add_argument("--docs", default="", help="Path to documentation root")
    parser.add_argument(
        "--config",
        default=str(here / "config.yaml"),
        help="Path to config.yaml",
    )
    parser.add_argument(
        "--languages",
        choices=["auto", "python", "ts", "both"],
        default="auto",
        help="Restrict detected languages: auto, python, ts or both (default auto)",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Permit network for the isolated pytest process (off by default)",
    )
    parser.add_argument(
        "--llm-network",
        action="store_true",
        help="Permit network only for the QA process's own LLM provider call; "
        "the tested project's pytest run still stays offline (off by default)",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Allow optional LLM calls when configured; still never deletes the live tree",
    )
    parser.add_argument(
        "--reports-dir",
        default="",
        help="Override reports directory",
    )
    parser.add_argument(
        "--findings-dir",
        default="",
        help="Where findings.json/findings.md are written (default: same as --reports-dir)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Enable strict verification (riley.veto_on_uncovered_claims=true)",
    )
    parser.add_argument(
        "--claims-strict",
        action="store_true",
        help="Documented claims with no covering test are P2 findings",
    )
    parser.add_argument(
        "--identity-strict",
        action="store_true",
        help="Raise identity leak findings (names, emails, home paths) to P1",
    )
    parser.add_argument(
        "--clean-install",
        action="store_true",
        help="Test installing from scratch in an empty venv / fresh node_modules exactly as README says",
    )
    parser.add_argument(
        "--sandbox",
        choices=["auto", "required", "off"],
        default=None,
        help="Sandbox mode: auto (default), required, or off",
    )
    parser.add_argument(
        "--mutation",
        action="store_true",
        help="Enable mutation testing phase",
    )
    parser.add_argument(
        "--mutation-min-score",
        type=float,
        default=None,
        help="Minimum mutation score threshold (0.0 to 1.0) required to pass",
    )
    parser.add_argument(
        "--mutation-max-mutants",
        type=int,
        default=None,
        help="Maximum number of mutants to evaluate (default 30)",
    )
    parser.add_argument(
        "--mutation-workers",
        type=int,
        default=None,
        help="Parallel mutant workers (operator only; default min(4, cpu_count), cap 16)",
    )
    parser.add_argument(
        "--sarif",
        default="",
        help="Write a SARIF 2.1.0 document describing the run's findings to this path",
    )
    parser.add_argument(
        "--diff",
        default="",
        metavar="BASE_REF",
        help="Fast pre-push mode: scope the run to files changed since BASE_REF "
        "(e.g. origin/master or HEAD~5). Mutation testing is off; if the scope "
        "cannot be computed the full cycle runs instead.",
    )
    parser.add_argument(
        "--baseline",
        default="",
        metavar="PATH",
        help="Path to a baseline findings.json file; recorded findings do not fail the gate",
    )
    parser.add_argument(
        "--profile",
        choices=["default", "factory"],
        default=None,
        help="Configuration profile: default or factory (fail-on=p2, identity-strict=true, mutation=true)",
    )
    parser.add_argument(
        "--fail-on",
        choices=["p0", "p1", "p2", "p3", "P0", "P1", "P2", "P3"],
        default=None,
        help="Minimum finding priority that triggers VETO (p0, p1, p2, p3; default p1)",
    )
    parser.add_argument(
        "--fix-brief",
        choices=["true", "false"],
        default=None,
        help="Generate fix-brief.md (true/false; default true)",
    )
    parser.add_argument(
        "--no-fix-brief",
        action="store_true",
        help="Disable generation of fix-brief.md",
    )
    parser.add_argument(
        "--feedback-export",
        choices=["true", "false"],
        default=None,
        help="Opt-in anonymous feedback export (true/false; default false)",
    )
    return parser.parse_args(argv)


def assemble_self_test_docs(repo: Path) -> Path:
    dest = Path(tempfile.mkdtemp(prefix="qa-self-test-docs-"))
    readme = repo / "README.md"
    if readme.is_file():
        shutil.copy2(readme, dest / "README.md")
    schemas = repo / "schemas"
    if schemas.is_dir():
        shutil.copytree(schemas, dest / "schemas")
    return dest


def main(argv: list[str] | None = None) -> int:
    if os.environ.get("GITHUB_EVENT_NAME", "").strip() == "pull_request_target":
        print(
            "security error: refusing to execute under pull_request_target",
            file=sys.stderr,
        )
        return 2
    args = parse_args(argv)
    here = Path(__file__).resolve().parent

    docs_cleanup: Path | None = None
    if args.self_test:
        project = here
        docs = assemble_self_test_docs(here)
        docs_cleanup = docs
    else:
        if not args.project or not args.docs:
            print("provide --project and --docs, or use --self-test", file=sys.stderr)
            return 1
        project = Path(args.project).expanduser().resolve()
        docs = Path(args.docs).expanduser().resolve()

    if not project.is_dir():
        print(f"project path is not a directory: {project}", file=sys.stderr)
        return 1
    if not docs.exists():
        print(f"docs path does not exist: {docs}", file=sys.stderr)
        return 1

    config = load_config(Path(args.config))
    config.setdefault("dry_run", True)
    config.setdefault("allow_network", False)
    if args.profile:
        config["profile"] = args.profile
        from core.orchestrator import apply_profile

        apply_profile(config)
    if args.fail_on:
        config["fail_on"] = args.fail_on.upper()
    if args.fix_brief is not None:
        config["fix_brief"] = args.fix_brief == "true"
    if args.no_fix_brief:
        config["fix_brief"] = bool()
    if args.feedback_export is not None:
        config["feedback_export"] = args.feedback_export == "true"
    if args.no_dry_run:
        config["dry_run"] = False
    os.environ["QA_LANGUAGES"] = args.languages
    if args.allow_network:
        config["allow_network"] = True
    if args.llm_network:
        config["llm"] = dict(config.get("llm") or {})
        config["llm"]["allow_network"] = True
    if args.strict:
        config.setdefault("riley", {})
        config["riley"] = dict(config.get("riley") or {})
        config["riley"]["veto_on_uncovered_claims"] = True
        config["riley"]["veto_on_weak_tests"] = True
        config["lexi"] = dict(config.get("lexi") or {})
        config["lexi"]["veto_on_missing_code"] = True
    if args.claims_strict:
        config.setdefault("lexi", {})
        config["lexi"] = dict(config.get("lexi") or {})
        config["lexi"]["claims_strict"] = True
    if args.identity_strict:
        config["identity_strict"] = True
    if args.clean_install:
        config["clean_install"] = True
    if args.sandbox:
        config.setdefault("sandbox", {})
        config["sandbox"] = dict(config.get("sandbox") or {})
        config["sandbox"]["mode"] = args.sandbox
    if args.mutation:
        config.setdefault("mutation", {})
        config["mutation"] = dict(config.get("mutation") or {})
        config["mutation"]["enabled"] = True
    if args.mutation_min_score is not None:
        config.setdefault("mutation", {})
        config["mutation"] = dict(config.get("mutation") or {})
        config["mutation"]["min_score"] = float(args.mutation_min_score)
    if args.mutation_max_mutants is not None:
        config.setdefault("mutation", {})
        config["mutation"] = dict(config.get("mutation") or {})
        config["mutation"]["max_mutants"] = int(args.mutation_max_mutants)
    if args.mutation_workers is not None:
        config.setdefault("mutation", {})
        config["mutation"] = dict(config.get("mutation") or {})
        config["mutation"]["workers"] = int(args.mutation_workers)
    if args.reports_dir:
        config["reports_dir"] = args.reports_dir
    else:
        config.setdefault("reports_dir", str(here / "reports"))
    if args.findings_dir:
        config["findings_dir"] = args.findings_dir
    if args.diff:
        config["diff"] = dict(config.get("diff") or {})
        config["diff"]["base_ref"] = args.diff
    if args.self_test:
        # Copy the working tree, not HEAD, so uncommitted tests are included.
        config["isolation_method"] = "copy"
        config.setdefault("pytest", {})
        config["pytest"] = dict(config.get("pytest") or {})
        config["pytest"].setdefault("timeout_seconds", 180)

    # Load operator exceptions at startup. An invalid entry is a hard config
    # error (never a silent ignore), so a typo cannot quietly disable a guard.
    from core.exceptions import (
        ExceptionsConfigError,
        load_baseline,
        load_exceptions,
        load_qa_ignore,
    )

    project_name = project.resolve().name
    try:
        exception_entries = load_exceptions(config, project_name)
        if args.baseline:
            config["baseline_fingerprints"] = sorted(load_baseline(args.baseline))
    except ExceptionsConfigError as exc:
        print(f"exceptions config error: {exc}", file=sys.stderr)
        return 1

    qa_ignore_entries, _ = load_qa_ignore(project)
    exception_entries.extend(qa_ignore_entries)

    started_monotonic = time.monotonic()
    try:
        vesper = Vesper(config)
        report = vesper.run(project, docs, exceptions=exception_entries)
    finally:
        # Docs bundle is synthetic; never delete the live tree.
        if docs_cleanup is not None:
            shutil.rmtree(docs_cleanup, ignore_errors=True)
    elapsed_seconds = time.monotonic() - started_monotonic

    reports_dir = Path(config["reports_dir"])
    print(f"verdict={report.verdict}")
    print(f"elapsed={elapsed_seconds:.1f}s")
    if report.escalation:
        print(report.escalation)
    print(f"sandbox={report.sandbox}")
    for warning in getattr(report, "warnings", []) or []:
        print(f"WARN {warning}")
    if report.mutation and report.mutation.get("status") == "completed":
        print(f"mutation_score={report.mutation.get('score', 0.0):.2f}")
    print(f"report={reports_dir / (report.run_id + '.md')}")
    findings_base = Path(config.get("findings_dir") or reports_dir)
    print(f"findings={findings_base / report.run_id / 'findings.md'}")
    print(f"findings_latest={findings_base / 'latest' / 'findings.md'}")
    print(f"fix_brief={findings_base / report.run_id / 'fix-brief.md'}")
    print(f"fix_brief_latest={findings_base / 'latest' / 'fix-brief.md'}")
    print(f"pr_comment={findings_base / report.run_id / 'pr-comment.md'}")
    print(f"pr_comment_latest={findings_base / 'latest' / 'pr-comment.md'}")
    print(f"summary={findings_base / report.run_id / 'summary.md'}")
    print(f"summary_latest={findings_base / 'latest' / 'summary.md'}")
    if config.get("feedback_export"):
        print(f"feedback_export={findings_base / report.run_id / 'feedback-export.json'}")
    print(f"isolated_root={report.isolated_root}")
    if args.sarif:
        try:
            sarif_path = write_sarif(report, Path(args.sarif).expanduser())
            print(f"sarif={sarif_path}")
        except OSError as exc:
            print(f"sarif write failed: {exc}", file=sys.stderr)
    return exit_code_for(report.verdict)


if __name__ == "__main__":
    sys.exit(main())

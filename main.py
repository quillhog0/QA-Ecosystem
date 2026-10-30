#!/usr/bin/env python3
"""Entry point: python main.py --project PATH --docs PATH"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
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
        "--allow-network",
        action="store_true",
        help="Permit network for the isolated pytest process (off by default)",
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
        "--strict",
        action="store_true",
        help="Enable strict verification (riley.veto_on_uncovered_claims=true)",
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
        "--sarif",
        default="",
        help="Write a SARIF 2.1.0 document describing the run's findings to this path",
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
    if args.no_dry_run:
        config["dry_run"] = False
    if args.allow_network:
        config["allow_network"] = True
    if args.strict:
        config.setdefault("riley", {})
        config["riley"] = dict(config.get("riley") or {})
        config["riley"]["veto_on_uncovered_claims"] = True
        config["lexi"] = dict(config.get("lexi") or {})
        config["lexi"]["veto_on_missing_code"] = True
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
    if args.reports_dir:
        config["reports_dir"] = args.reports_dir
    else:
        config.setdefault("reports_dir", str(here / "reports"))
    if args.self_test:
        # Copy the working tree, not HEAD, so uncommitted tests are included.
        config["isolation_method"] = "copy"
        config.setdefault("pytest", {})
        config["pytest"] = dict(config.get("pytest") or {})
        config["pytest"].setdefault("timeout_seconds", 180)

    try:
        vesper = Vesper(config)
        report = vesper.run(project, docs)
    finally:
        # Docs bundle is synthetic; never delete the live tree.
        if docs_cleanup is not None:
            shutil.rmtree(docs_cleanup, ignore_errors=True)

    reports_dir = Path(config["reports_dir"])
    print(f"verdict={report.verdict}")
    if report.escalation:
        print(report.escalation)
    print(f"sandbox={report.sandbox}")
    if report.mutation and report.mutation.get("status") == "completed":
        print(f"mutation_score={report.mutation.get('score', 0.0):.2f}")
    print(f"report={reports_dir / (report.run_id + '.md')}")
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

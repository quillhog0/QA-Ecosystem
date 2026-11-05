"""IDE-ready findings projection of a finished QA run.

Every run writes ``findings.json`` (machine readable, validated by the schema
in ``docs/findings.schema.json``) and a short ``findings.md`` (about 40 lines:
a per-priority count table plus the top findings) next to the existing
Markdown and YAML reports. This module only *projects* what the roles already
decided - it never changes a verdict, and it imports no agent code, so the
pipeline stays the single authority on CLEAR / VETO / BLOCKED_ENV.

The priority ladder and the type mapping are documented in
``docs/findings-priority.md``; the projection is deterministic: the same
report dictionary always produces byte-identical findings documents.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

PRIORITY_LEVELS = ("P0", "P1", "P2", "P3")
FINDING_TYPES = (
    "drift",
    "claim",
    "weak_test",
    "policy",
    "test_failure",
    "environment",
    "identity_leak",
    "clean_install",
    "deleted_tests",
    "mutation",
)
FINDING_CLASSES = ("hard", "heuristic")
FINDING_CONFIDENCES = ("high", "low")
FINDING_AGENTS = ("lexi", "aria", "riley", "sable", "nova")
VERDICT_EFFECTS = ("VETO", "WARN", "INFO")

MAX_EVIDENCE_CHARS = 200
MD_MAX_LINES = 40
MD_TOP_COUNT = 10

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs" / "findings.schema.json"

# A failing product test whose name or message touches money, secrets or the
# sandbox is a data-loss / security class defect (see docs/findings-priority.md).
_MONEY_SECURITY_TERMS = (
    "payment", "refund", "charge", "payout", "withdraw", "deposit",
    "secret", "credential", "password", "api_key", "private_key",
    "sandbox", "escape", "data_loss", "corrupt", "wipe", "exfil",
)

# Sable checks that protect money, secrets or the sandbox boundary are P0;
# the remaining policy gates are P1 when they block the run.
_POLICY_P0_CHECKS = frozenset(
    {"no_secret_env", "no_secret_files", "not_live_tree", "target_inside_isolated",
     "tests_ast_clean", "network_denied"}
)
_POLICY_P1_CHECKS = frozenset({"pytest_only", "isolated_exists"})

_EXT_PATTERN = r"\.(?:py|ts|tsx|js|jsx|mjs|cjs)"
_NODE_FILE_RE = re.compile(rf"^(?P<file>[^:\s]+?{_EXT_PATTERN})::")
# ``tests/test_x.py:27: in test_y`` or ``File "…/test_x.py", line 27``
_TB_LINE_RE = re.compile(rf"(?:^|\s)(?P<file>[\w./\-]+?{_EXT_PATTERN}):(?P<line>\d+):")
_TB_FILE_LINE_RE = re.compile(rf'File "(?P<file>[^"]+?{_EXT_PATTERN})", line (?P<line>\d+)')
_PY_PATH_RE = re.compile(rf"(?P<file>(?:[\w./-]+/)?[\w.-]+?{_EXT_PATTERN})")

_WEAK_TEST_PREFIX = "weak test: "
_CLAIM_WEAK_FRAGMENT = "covered only by weak test"
_CLAIM_UNCOVERED_PREFIXES = ("claim not covered by tests:", "uncovered claims:")
# Riley gaps that restate a Nova failure or a Lexi signal verbatim; projecting
# them again would only duplicate findings that already carry better evidence.
RILEY_SKIPPED_PREFIXES = (
    "test detail: ",
    "failed=",
    "pytest exit code ",
    "pytest collection/execution errors",
    "environment-blocked failures=",
)


def _one_line(text: Any, limit: int = MAX_EVIDENCE_CHARS) -> str:
    """Collapse whitespace and clip: a short, single-line evidence excerpt."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3] + "..."


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def verdict_effect_for(priority: str, evidence: str, fail_on: str = "P1") -> str:
    """The verdict rule: findings at or above fail_on with non-empty evidence may cause a VETO.

    Everything else reports WARN (still actionable) or INFO (a note). The
    control-plane verdict itself is decided by the roles, not here; this field
    tells an IDE whether the single finding would justify blocking a merge.
    """
    threshold = fail_on.upper() if fail_on.upper() in PRIORITY_LEVELS else "P1"
    if (evidence or "").strip() and PRIORITY_LEVELS.index(priority) <= PRIORITY_LEVELS.index(threshold):
        return "VETO"
    if priority == "P3":
        return "INFO"
    return "WARN"


def fingerprint_for(agent: str, ftype: str, file: str | None, term: str | None) -> str:
    """A stable identity for one finding, deliberately without a line number.

    ``agent:type:file:term`` is what a maintainer copies into the exceptions
    file (see ``docs/exceptions.md``). Line numbers are excluded so the match
    survives small edits that shift a line; a missing part is ``-``.
    """
    return f"{agent}:{ftype}:{file or '-'}:{term or '-'}"


def _stable_term(text: str) -> str:
    """Collapse free text to a short, edit-tolerant term token for a fingerprint.

    Lower-cased, non-alphanumerics folded to single underscores, and any run of
    digits dropped so renumbered scenarios do not silently break a match.
    """
    collapsed = re.sub(r"[^0-9a-zA-Z]+", "_", str(text or "")).strip("_").lower()
    collapsed = re.sub(r"_\d+(?:_|$)", "_", collapsed)
    return collapsed[:80].strip("_")


def _node_location(node: str, traceback: str = "") -> tuple[str | None, int | None]:
    """``(file, line)`` for a pytest nodeid plus its traceback, or (None, None)."""
    node = str(node or "")
    file_name: str | None = None
    match = _NODE_FILE_RE.match(node)
    if match:
        file_name = match.group("file")
    elif node.endswith(".py"):
        file_name = node
    line: int | None = None
    text = f"{traceback}\n{node}"
    for regex in (_TB_LINE_RE, _TB_FILE_LINE_RE):
        for found in regex.finditer(text):
            candidate = found.group("file")
            if file_name and candidate.endswith(file_name) or candidate == file_name:
                line = _as_int(found.group("line")) or None
                break
        if line is not None:
            break
    if line is None:
        for regex in (_TB_LINE_RE, _TB_FILE_LINE_RE):
            for found in regex.finditer(text):
                line = _as_int(found.group("line")) or None
                if not file_name:
                    file_name = found.group("file")
                break
            if line is not None:
                break
    return file_name, line


class _FindingBuilder:
    """Collects raw findings, deduplicates, sorts and assigns stable ids."""

    def __init__(self) -> None:
        self._items: list[dict[str, Any]] = []
        self._seen: set[tuple[Any, ...]] = set()

    def add(
        self,
        *,
        agent: str,
        ftype: str,
        priority: str,
        impact: str,
        evidence: str,
        red_test: str,
        file: str | None = None,
        line: int | None = None,
        code_file: str | None = None,
        code_line: int | None = None,
        term: str | None = None,
        fclass: str = "heuristic",
        confidence: str = "high",
    ) -> None:
        if fclass not in FINDING_CLASSES:
            fclass = "heuristic"
        if confidence not in FINDING_CONFIDENCES:
            confidence = "high"
        # Rule: Low-confidence heuristic findings must be capped at P2 (never P1/P0).
        if fclass == "heuristic" and confidence == "low" and priority in ("P0", "P1"):
            priority = "P2"
        key = (agent, ftype, file, line, _one_line(evidence))
        if key in self._seen:
            return
        self._seen.add(key)
        self._items.append(
            {
                "id": "",
                "agent": agent,
                "file": file,
                "line": line,
                "code_file": code_file,
                "code_line": code_line,
                "priority": priority,
                "type": ftype,
                "class": fclass,
                "confidence": confidence,
                "fingerprint": fingerprint_for(agent, ftype, file, term),
                "customer_impact": impact,
                "evidence": _one_line(evidence),
                "proposed_red_test": red_test,
                "verdict_effect": verdict_effect_for(priority, evidence),
                # sort order, replaced on finalise
                "_p": PRIORITY_LEVELS.index(priority),
                "_s": len(self._items),
            }
        )

    def finalise(self) -> list[dict[str, Any]]:
        self._items.sort(key=lambda f: (f["_p"], f["agent"], f["file"] or "", f["line"] or 0, f["_s"]))
        out: list[dict[str, Any]] = []
        for index, item in enumerate(self._items, start=1):
            finding = {k: v for k, v in item.items() if not k.startswith("_")}
            finding["id"] = f"F-{index:03d}"
            # id first: it is the anchor every consumer quotes
            ordered = {"id": finding.pop("id")}
            ordered.update(finding)
            out.append(ordered)
        return out


def _drift_priority(severity: str) -> str:
    s = (severity or "").lower()
    if s == "high":
        return "P1"
    if s == "medium":
        return "P2"
    return "P3"


def _collect_lexi(builder: _FindingBuilder, drift: dict[str, Any] | None) -> None:
    for finding in (drift or {}).get("findings") or []:
        if not isinstance(finding, dict):
            continue
        kind = str(finding.get("kind") or "")
        ident = str(finding.get("path") or "")
        message = str(finding.get("message") or "")
        severity = str(finding.get("severity") or "medium")
        priority = _drift_priority(severity)
        fclass = str(finding.get("class", "heuristic"))
        default_conf = "high" if severity.lower() == "high" else "low"
        confidence = str(finding.get("confidence", default_conf))
        if kind == "missing_tests":
            file = ident if ident.endswith(".py") or "/" in ident else None
            code_file = finding.get("code_file") or None
            code_line = finding.get("code_line") or None
            builder.add(
                agent="lexi",
                ftype="weak_test",
                priority=priority,
                impact="Nothing exercises this code, so a regression could ship silently.",
                evidence=message,
                red_test="Add a pytest that calls the public surface and asserts its documented result; it is missing today.",
                file=file,
                code_file=code_file,
                code_line=code_line,
                term=ident,
                fclass=fclass,
                confidence=confidence,
            )
            continue
        docs_file = finding.get("docs_file") or None
        docs_line = finding.get("docs_line") or None
        code_file = finding.get("code_file") or None
        code_line = finding.get("code_line") or None
        if kind == "domain_term":
            impact = f"The docs talk about `{ident}` but no callable stands behind it, so readers are misled."
            red_test = f"None needed for a low note; align the docs wording for `{ident}` with the code."
        elif kind == "contradiction":
            impact = "Executed behaviour contradicts what the docs promise, so the docs cannot be trusted."
            red_test = "Keep the failing behaviour test named in the evidence red until the docs and code agree."
        else:
            impact = f"The docs promise `{ident}`, but code following that contract cannot be found, so integrators hit errors."
            red_test = f"Write a pytest that imports the module the docs name and asserts `{ident}` exists; it fails today."
        builder.add(
            agent="lexi",
            ftype="drift",
            priority=priority,
            impact=impact,
            evidence=message,
            red_test=red_test,
            file=docs_file,
            line=docs_line,
            code_file=code_file,
            code_line=code_line,
            term=ident,
            fclass=fclass,
            confidence=confidence,
        )


def _collect_aria(builder: _FindingBuilder, plan: dict[str, Any] | None) -> None:
    for scenario in (plan or {}).get("scenarios") or []:
        if not isinstance(scenario, dict):
            continue
        sid = str(scenario.get("id") or "")
        if not sid.startswith("C") or scenario.get("covered_by"):
            continue
        title = str(scenario.get("title") or sid)
        acceptance = scenario.get("acceptance") or ["the documented outcome holds"]
        builder.add(
            agent="aria",
            ftype="claim",
            priority="P2",
            impact=f"A documented claim is unproven: “{title}” has no covering test.",
            evidence=title,
            red_test=(
                f"Add `test_claim_{sid.lower()}` that exercises “{str(acceptance[0])}” "
                "and asserts the documented outcome; it fails today."
            ),
            term=sid,
            fclass="heuristic",
            confidence="low",
        )


def _collect_riley(builder: _FindingBuilder, review: dict[str, Any] | None, agent: str = "riley") -> None:
    for gap in (review or {}).get("gaps") or []:
        if not isinstance(gap, str) or not gap.strip():
            continue
        if gap.startswith(_WEAK_TEST_PREFIX):
            rest = gap[len(_WEAK_TEST_PREFIX):].strip()
            node = rest.split(" ", 1)[0]
            file_name, line = _node_location(node)
            is_p1 = "[P1]" in gap
            priority = "P1" if is_p1 else "P2"
            builder.add(
                agent=agent,
                ftype="weak_test",
                priority=priority,
                impact="A test passes without checking behaviour, so it can hide a real bug.",
                evidence=gap,
                red_test="Rewrite the test to assert the concrete outcome and confirm it goes red when the behaviour breaks.",
                file=file_name,
                line=line,
                term=node,
                fclass="heuristic",
                confidence="high" if is_p1 else "low",
            )
            continue
        if _CLAIM_WEAK_FRAGMENT in gap or gap.startswith(_CLAIM_UNCOVERED_PREFIXES):
            builder.add(
                agent=agent,
                ftype="claim",
                priority="P2",
                impact="A documented claim is not proven by a strong test, so it may be false.",
                evidence=gap,
                red_test="Add or strengthen a test that proves the claim and fails when the behaviour regresses.",
                term=_stable_term(gap),
                fclass="heuristic",
                confidence="low",
            )
            continue
        if gap.startswith(RILEY_SKIPPED_PREFIXES):
            continue
        builder.add(
            agent=agent,
            ftype="weak_test",
            priority="P2",
            impact="The test plan has a quality gap, so planned behaviour is not fully verified.",
            evidence=gap,
            red_test="Close the plan gap with a concrete failing-then-passing scenario before trusting the suite.",
            term=_stable_term(gap),
            fclass="heuristic",
            confidence="low",
        )


def _extract_policy_file(reason: str) -> str | None:
    found = _PY_PATH_RE.search(reason or "")
    if not found:
        return None
    raw = found.group("file")
    if raw.startswith("/") or ".." in raw:
        return None
    return raw


def _collect_sable(builder: _FindingBuilder, policy: dict[str, Any] | None) -> None:
    if not isinstance(policy, dict):
        return
    phases: list[tuple[str, dict[str, Any]]] = []
    if "checks" in policy and "blocked_reasons" in policy:
        phases.append(("preflight", policy))
    else:
        for key in ("preflight", "post"):
            entry = policy.get(key)
            if isinstance(entry, dict):
                phases.append((key, entry))
    for phase_name, phase in phases:
        checks = phase.get("checks") or {}
        blocked = "; ".join(str(r) for r in (phase.get("blocked_reasons") or []) if r)
        for check_key, passed in (checks if isinstance(checks, dict) else {}).items():
            if passed is not False:
                continue
            if check_key in _POLICY_P0_CHECKS:
                priority = "P0"
                impact = "The project can expose secrets, move money or escape its sandbox, so data or safety is at risk."
            elif check_key in _POLICY_P1_CHECKS:
                priority = "P1"
                impact = "A policy gate failed, so the run cannot safely proceed against this code."
            else:
                priority = "P2"
                impact = "A policy check did not pass; the run result is weaker than it claims."
            evidence = blocked or f"policy check failed: {check_key} ({phase_name})"
            builder.add(
                agent="sable",
                ftype="policy",
                priority=priority,
                impact=impact,
                evidence=evidence,
                red_test=(
                    "Add a pre-flight regression test that runs the policy predicate over the "
                    f"tree and asserts `{check_key}` passes; it fails today."
                ),
                file=_extract_policy_file(evidence) if check_key == "tests_ast_clean" else None,
                term=check_key,
                fclass="hard",
                confidence="high",
            )
        for warning in phase.get("warnings") or []:
            builder.add(
                agent="sable",
                ftype="policy",
                priority="P3",
                impact="A dangerous operation is only tolerated because the sandbox contains it; worth reviewing.",
                evidence=str(warning),
                red_test="None; informational note about a sandbox-conditional relaxation.",
                term=_stable_term(str(warning)),
                fclass="hard",
                confidence="high",
            )


def _product_impact(node: str, message: str) -> tuple[str, str]:
    text = f"{node} {message}".lower()
    if any(term in text for term in _MONEY_SECURITY_TERMS):
        return (
            "A money, secret or safety path is broken, so data, funds or credentials are at risk.",
            "P0",
        )
    return (
        "A behaviour the product promises does not work: a test that should pass is red today.",
        "P1",
    )


def _collect_nova(builder: _FindingBuilder, execution: dict[str, Any] | None) -> None:
    if not isinstance(execution, dict) or not execution:
        return
    failures = execution.get("failures") or []
    emitted_failure = False
    for failure in failures if isinstance(failures, list) else []:
        if not isinstance(failure, dict):
            continue
        node = str(failure.get("node") or "unknown test")
        message = str(failure.get("message") or "")
        traceback = str(failure.get("traceback") or "")
        file_name, line = _node_location(node, traceback)
        classification = str(failure.get("classification") or "product")
        if classification == "environment":
            cause = str(failure.get("environment_cause") or message)
            builder.add(
                agent="nova",
                ftype="environment",
                priority="P2",
                impact="The check could not run on this machine, so it proves nothing about the product either way.",
                evidence=cause or message,
                red_test=f"Restore the precondition ({str(failure.get('environment_kind') or 'environment capability')}) and rerun `{node}`; not a product red test.",
                file=file_name,
                line=line,
                term=node,
                fclass="hard",
                confidence="high",
            )
            emitted_failure = True
            continue
        if classification == "flaky":
            builder.add(
                agent="nova",
                ftype="test_failure",
                priority="P2",
                impact="Test failed on first run and passed on rerun (FLAKY), indicating non-deterministic behaviour.",
                evidence=f"FLAKY: {node} ({message})",
                red_test=f"Stabilize `{node}` so it passes deterministically on every run.",
                file=file_name,
                line=line,
                term=node,
                fclass="hard",
                confidence="high",
            )
            continue
        impact, priority = _product_impact(node, message)
        evidence = " ".join((message or traceback or node).split())
        builder.add(
            agent="nova",
            ftype="test_failure",
            priority=priority,
            impact=impact,
            evidence=evidence,
            red_test=f"`{node}` is red today; keep it as the regression test.",
            file=file_name,
            line=line,
            term=node,
            fclass="hard",
            confidence="high",
        )
        emitted_failure = True
    for flaky_item in execution.get("flaky_tests") or []:
        if not isinstance(flaky_item, dict):
            continue
        node = str(flaky_item.get("node") or "unknown test")
        message = str(flaky_item.get("message", ""))
        traceback = str(flaky_item.get("traceback", ""))
        file_name, line = _node_location(node, traceback)
        builder.add(
            agent="nova",
            ftype="test_failure",
            priority="P2",
            impact="Test failed on first run and passed on rerun (FLAKY), indicating non-deterministic behaviour.",
            evidence=f"FLAKY: {node} ({message})",
            red_test=f"Stabilize `{node}` so it passes deterministically on every run.",
            file=file_name,
            line=line,
            term=node,
            fclass="hard",
            confidence="high",
        )
    failed = _as_int(execution.get("failed"))
    errors = _as_int(execution.get("errors"))
    if failed and not emitted_failure:
        # Failures existed but no structured entry survived (clipped log): one
        # aggregate finding keeps the veto-eligible signal visible.
        impact, priority = _product_impact("", str(execution.get("stdout") or ""))
        builder.add(
            agent="nova",
            ftype="test_failure",
            priority=priority,
            impact=impact,
            evidence=f"pytest exit={execution.get('exit_code')} failed={failed} errors={errors}",
            red_test="Rerun pytest and keep the first failing test red as the regression test.",
            term="__aggregate_failures__",
            fclass="hard",
            confidence="high",
        )
    install_status = str(execution.get("install_status") or "")
    if install_status.startswith("pip install failed"):
        builder.add(
            agent="nova",
            ftype="environment",
            priority="P2",
            impact="Declared dependencies could not be installed here, so the suite never ran.",
            evidence=install_status,
            red_test="Restore the dependency set and rerun the suite; not a product red test.",
            term="__pip_install_failed__",
            fclass="hard",
            confidence="high",
        )


def _collect_blocked_env(builder: _FindingBuilder, report: dict[str, Any]) -> None:
    """A BLOCKED_ENV run with no failure entries still needs one visible cause."""
    if str(report.get("verdict") or "") != "BLOCKED_ENV":
        return
    execution = report.get("execution")
    if isinstance(execution, dict) and (execution.get("failures") or []):
        return
    cause = str(((report.get("post_audit") or {}).get("root_cause")) or "").strip()
    if not cause:
        for entry in report.get("role_results") or []:
            if isinstance(entry, dict) and str(entry.get("verdict") or "") == "BLOCKED_ENV":
                cause = str(entry.get("summary") or "").strip()
                if cause:
                    break
    if cause:
        builder.add(
            agent="nova",
            ftype="environment",
            priority="P2",
            impact="The run was blocked before tests could judge the product, so nothing is proven either way.",
            evidence=cause,
            red_test="Restore the environment precondition and rerun; not a product red test.",
            term="__blocked_env__",
            fclass="hard",
            confidence="high",
        )


def build_findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a finished report dictionary into the findings list.

    Deterministic: identical input yields identical output, ids included.
    """
    builder = _FindingBuilder()
    _collect_lexi(builder, report.get("drift"))
    post = report.get("post_audit") if isinstance(report.get("post_audit"), dict) else {}
    # The post-audit Lexi pass re-scans the same docs and adds contradictions;
    # prefer it, falling back to the pre-execution drift block when absent.
    if isinstance(post.get("lexi"), dict):
        _collect_lexi(builder, post.get("lexi"))
    _collect_aria(builder, report.get("plan"))
    if isinstance(post.get("riley"), dict):
        _collect_riley(builder, post.get("riley"))
    for entry in report.get("role_results") or []:
        if (
            isinstance(entry, dict)
            and entry.get("role") == "riley"
            and entry.get("stage") == "adversarial_plan"
            and isinstance(entry.get("details"), dict)
        ):
            _collect_riley(builder, entry["details"])
    _collect_sable(builder, report.get("policy"))
    _collect_nova(builder, report.get("execution"))
    _collect_blocked_env(builder, report)
    _collect_identity(builder, report.get("identity_leaks") or post.get("identity_leaks"))
    _collect_clean_install(builder, report.get("clean_install") or post.get("clean_install"))
    _collect_deleted_tests(builder, report.get("deleted_tests") or post.get("deleted_tests"))
    _collect_weakened_tests(builder, report.get("weakened_tests") or post.get("weakened_tests"))
    _collect_diff_mutants(builder, report.get("mutation"))
    return builder.finalise()


def _collect_identity(builder: _FindingBuilder, leaks: list[dict[str, Any]] | None) -> None:
    for leak in leaks or []:
        if not isinstance(leak, dict):
            continue
        leak_type = leak.get("type", "identity_leak")
        file = leak.get("file")
        line = leak.get("line")
        priority = leak.get("priority", "P2")
        builder.add(
            agent="sable",
            ftype="identity_leak",
            priority=priority,
            impact=(
                "Critical secret token or private key detected in shipped tree."
                if priority == "P1"
                else "Sensitive identity or environment path detected in shipped tree."
            ),
            evidence=leak.get("evidence") or f"{file}:{line} {leak_type}",
            red_test="Remove the sensitive identity data or secret token from the file.",
            file=file,
            line=line,
            term=str(leak_type),
            fclass="hard",
            confidence="high",
        )


def _collect_clean_install(builder: _FindingBuilder, clean_install: dict[str, Any] | None) -> None:
    if not clean_install or not clean_install.get("failed"):
        return
    cmd = clean_install.get("command", "")
    output = clean_install.get("output", "")
    builder.add(
        agent="nova",
        ftype="clean_install",
        priority="P1",
        impact="README install instructions do not work on a fresh environment.",
        evidence=f"Install command '{cmd}' failed: {output[:200]}",
        red_test="Fix README installation instructions to match required packages and dependencies.",
        term="clean_install_failure",
        fclass="hard",
        confidence="high",
    )


def _collect_deleted_tests(builder: _FindingBuilder, deleted_tests: list[str] | None) -> None:
    for test_id in deleted_tests or []:
        builder.add(
            agent="riley",
            ftype="deleted_tests",
            priority="P1",
            impact=f"Number of tests dropped versus base ref in --diff mode: test '{test_id}' was removed.",
            evidence=f"deleted test: {test_id}",
            red_test="Restore removed tests or ensure test count does not decrease in PR/diff.",
            term=str(test_id),
            fclass="hard",
            confidence="high",
        )


def _collect_weakened_tests(builder: _FindingBuilder, weakened_tests: list[str] | None) -> None:
    for entry in weakened_tests or []:
        text = str(entry)
        file_part = text.split("::", 1)[0] if "::" in text else None
        term_part = text.split(" ", 1)[0]
        builder.add(
            agent="riley",
            ftype="weak_test",
            priority="P1",
            impact=f"Test assertion strength decreased versus base ref in --diff mode: {text}.",
            evidence=f"weakened test: {text}",
            red_test="Restore removed assertions so the test guards the original behaviour.",
            file=file_part,
            term=term_part,
            fclass="heuristic",
            confidence="high",
        )


def _collect_diff_mutants(builder: _FindingBuilder, mutation: dict[str, Any] | None) -> None:
    if isinstance(mutation, dict):
        if mutation.get("diff_scoped"):
            for surv in mutation.get("survived_mutants", ()):
                if isinstance(surv, dict):
                    loc = str(surv.get("location", ""))
                    file_part, _, line_str = loc.partition(":")
                    orig = str(surv.get("original", ""))
                    mut = str(surv.get("mutated", ""))
                    builder.add(
                        agent="riley",
                        ftype="mutation",
                        priority="P2",
                        impact=f"No test guards this change at {loc} ({surv.get('type')}: {orig} -> {mut}).",
                        evidence=f"surviving mutant {surv.get('id')} at {loc}: {orig} -> {mut}",
                        red_test=f"Add a test assertion that fails when {loc} is mutated from {orig} to {mut}.",
                        file=file_part if file_part else None,
                        line=_as_int(line_str) if line_str else None,
                        term=loc,
                        fclass="heuristic",
                        confidence="high",
                    )


def count_by_priority(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {level: 0 for level in PRIORITY_LEVELS}
    for finding in findings:
        counts[finding["priority"]] = counts.get(finding["priority"], 0) + 1
    counts["total"] = len(findings)
    return counts


def _finding_matches_entry(finding: dict[str, Any], entry: Any) -> bool:
    fp = str(finding.get("fingerprint", ""))
    match_val = str(getattr(entry, "match", ""))
    rule_val = str(getattr(entry, "rule", "")).strip()
    agent_type = f"{finding.get('agent')}.{finding.get('type')}"
    rule_prefix = rule_val.split(".")[0]
    term = fp.split(":", 3)[-1]
    return all((match_val in (fp, term, str(finding.get("file", ""))), agent_type.startswith(rule_prefix)))


def apply_exceptions(
    findings: list[dict[str, Any]],
    entries: list[Any] | None,
    today: Any,
) -> dict[str, Any]:
    """Downgrade findings covered by an unexpired operator exception.

    ``entries`` are :class:`core.exceptions.ExceptionEntry` objects (duck-typed:
    ``match``, ``expires`` a ``date``, ``reason`` and ``allow_p0``). A matching,
    unexpired exception turns the finding into a P3 / INFO note carrying
    ``excepted: <reason>``; a P0 is only excepted when the entry sets
    ``allow_p0``. Expired entries do nothing but are reported so the operator
    renews or removes them. The control-plane verdict is never touched here -
    this only re-labels the projected findings.
    """
    entries = entries or []
    warnings: list[str] = []
    active_list: list[Any] = []
    seen_keys: dict[tuple[str, str], int] = {}
    for entry in entries:
        expires = getattr(entry, "expires", None)
        if expires is not None and expires < today:
            warnings.append(f"exception expired: {entry.match}")
            continue
        key = (str(getattr(entry, "rule", "")), str(entry.match))
        if key in seen_keys:
            active_list[seen_keys[key]] = entry
        else:
            seen_keys[key] = len(active_list)
            active_list.append(entry)

    excepted_p0: list[dict[str, Any]] = []
    false_positive_counts: dict[str, int] = {}
    for finding in findings:
        matched_entry = None
        for candidate in reversed(active_list):
            if _finding_matches_entry(finding, candidate):
                matched_entry = candidate
                break
        if matched_entry is None:
            continue
        if getattr(matched_entry, "is_in_repo", None) and finding["priority"] == "P0":
            warnings.append(f".qa-ignore cannot silence P0: {finding['fingerprint']}")
            continue
        if finding["priority"] == "P0" and not getattr(matched_entry, "allow_p0", None):
            # A P0 is never silently excepted: without allow_p0 the finding stands.
            continue
        was_p0 = finding["priority"] == "P0"
        finding["priority"] = "P3"
        finding["verdict_effect"] = "INFO"
        finding["excepted"] = matched_entry.reason
        kind = str(getattr(matched_entry, "kind", "accepted-risk"))
        finding["exception_kind"] = kind
        if kind == "false-positive":
            entry_rule = str(getattr(matched_entry, "rule", ""))
            rule_key = entry_rule if entry_rule else f"{finding.get('agent')}.{finding.get('type')}"
            false_positive_counts[rule_key] = false_positive_counts.get(rule_key, 0) + 1
        if was_p0:
            excepted_p0.append(
                {
                    "id": finding["id"],
                    "fingerprint": finding["fingerprint"],
                    "reason": matched_entry.reason,
                    "expires": matched_entry.expires.isoformat() if getattr(matched_entry, "expires", None) else "",
                    "added_by": str(getattr(matched_entry, "added_by", "")),
                }
            )
    downgraded_rules = _apply_precision_downgrade(findings, false_positive_counts)
    return {
        "findings": findings,
        "warnings": sorted(set(warnings)),
        "excepted_p0": excepted_p0,
        "false_positive_counts": false_positive_counts,
        "downgraded_rules": downgraded_rules,
    }


def _apply_precision_downgrade(
    findings: list[dict[str, Any]],
    false_positive_counts: dict[str, int],
) -> list[str]:
    """Downgrade unexcepted heuristic findings when rule FP rate exceeds 20%."""
    if not false_positive_counts:
        return []
    accepted_counts: dict[str, int] = {}
    for f in findings:
        if f.get("exception_kind") == "accepted-risk":
            rk = f"{f.get('agent')}.{f.get('type')}"
            accepted_counts[rk] = accepted_counts.get(rk, 0) + 1
    downgraded: list[str] = []
    for rk, fp_cnt in sorted(false_positive_counts.items()):
        ar_cnt = accepted_counts.get(rk, 0)
        if fp_cnt * 4 > (fp_cnt + ar_cnt):
            downgraded.append(rk)
            for f in findings:
                f_rk = f"{f.get('agent')}.{f.get('type')}"
                if all(
                    (
                        f_rk == rk,
                        not f.get("excepted"),
                        f.get("class") == "heuristic",
                    )
                ):
                    f["confidence"] = "low"
                    if f.get("priority") in ("P0", "P1"):
                        f["priority"] = "P2"
                        f["verdict_effect"] = "WARN"
    return downgraded


def apply_baseline(
    findings: list[dict[str, Any]],
    baseline_fingerprints: set[str] | list[str] | None,
) -> list[dict[str, Any]]:
    """Downgrade non-P0 findings whose fingerprints are recorded in the baseline."""
    if not baseline_fingerprints:
        return findings
    fp_set = set(baseline_fingerprints)
    for finding in findings:
        if finding.get("priority") == "P0":
            continue
        if str(finding.get("fingerprint") or "") in fp_set:
            finding["baselined"] = True
            finding["priority"] = "P3"
            finding["verdict_effect"] = "INFO"
    return findings


def build_document(
    report: dict[str, Any],
    exceptions: list[Any] | None = None,
    today: Any = None,
    baseline: set[str] | list[str] | None = None,
    fail_on: str = "P1",
) -> dict[str, Any]:
    findings = build_findings(report)
    document: dict[str, Any] = {
        "schema_version": "1",
        "run_id": str(report.get("run_id") or ""),
        "verdict": str(report.get("verdict") or ""),
        "generated_at": str(report.get("finished_at") or ""),
    }
    if exceptions:
        from datetime import date as _date

        today = today or _date.today()
        applied = apply_exceptions(findings, exceptions, today)
        document["warnings"] = applied["warnings"]
        document["excepted_p0"] = applied["excepted_p0"]
        if applied.get("false_positive_counts"):
            document["false_positive_counts"] = applied["false_positive_counts"]
        if applied.get("downgraded_rules"):
            document["downgraded_rules"] = applied["downgraded_rules"]
    effective_baseline = baseline if baseline is not None else report.get("baseline_fingerprints")
    if effective_baseline:
        apply_baseline(findings, effective_baseline)
    if fail_on.upper() != "P1":
        for f in findings:
            if not any((f.get("excepted"), f.get("baselined"))):
                f["verdict_effect"] = verdict_effect_for(str(f["priority"]), str(f["evidence"]), fail_on=fail_on)
    post = report.get("post_audit") if isinstance(report.get("post_audit"), dict) else {}
    workspaces = report.get("workspaces", post.get("workspaces"))
    if isinstance(workspaces, list):
        if workspaces:
            from core.workspace import assign_finding_packages, compute_workspace_verdicts

            assign_finding_packages(findings, workspaces)
            document["workspaces"] = workspaces
            document["workspace_verdicts"] = compute_workspace_verdicts(
                findings, workspaces, str(document["verdict"])
            )
    document["counts"] = count_by_priority(findings)
    document["findings"] = findings
    return document


def _finding_md_line(finding: dict[str, Any]) -> str:
    location = "-"
    if finding.get("file"):
        location = f"`{finding['file']}"
        if finding.get("line"):
            location += f":{finding['line']}"
        location += "`"
    impact = _one_line(finding.get("customer_impact"), 120)
    tail = ""
    if finding.get("excepted"):
        tail = f" [excepted: {_one_line(finding['excepted'], 60)}]"
    elif finding.get("baselined"):
        tail = " [baselined]"
    return (
        f"- [{finding['id']}] {finding['priority']} {finding['type']} "
        f"{finding['agent']} {location}: {impact} ({finding['verdict_effect']}){tail}"
    )


def render_markdown(document: dict[str, Any]) -> str:
    """The short human index: counts table + top findings, capped at ~40 lines."""
    counts = document.get("counts") or {}
    findings = document.get("findings") or []
    lines = [
        f"# Findings - run {document.get('run_id') or '?'}",
        "",
        f"- run verdict: **{document.get('verdict') or '?'}**",
        f"- findings: {counts.get('total', len(findings))} total",
    ]
    fp_counts = document.get("false_positive_counts") or {}
    if fp_counts:
        fp_str = ", ".join(f"{k}={v}" for k, v in sorted(fp_counts.items()))
        lines.append(f"- false-positive counts by rule: {fp_str}")
    lines.append("")
    excepted_p0 = document.get("excepted_p0") or []
    if excepted_p0:
        lines.append("## Excepted P0 findings (top of report)")
        for item in excepted_p0:
            reason = _one_line(item.get("reason"), 100)
            lines.append(
                f"- [{item.get('id')}] `{item.get('fingerprint')}`: excepted: {reason} "
                f"(expires {item.get('expires')}, added_by {item.get('added_by') or '-'})"
            )
        lines.append("")
    warnings = document.get("warnings") or []
    if warnings:
        for warning in warnings:
            lines.append(f"- WARN {warning}")
        lines.append("")
    lines += [
        "| priority | count |",
        "|---|---|",
    ]
    for level in PRIORITY_LEVELS:
        lines.append(f"| {level} | {counts.get(level, 0)} |")
    lines.append("")
    if not findings:
        lines.append("No findings.")
        return "\n".join(lines) + "\n"
    top = findings[:MD_TOP_COUNT]
    lines.append(f"## Top {len(top)} findings")
    for finding in top:
        lines.append(_finding_md_line(finding))
    if len(findings) > len(top):
        lines.append(f"- …{len(findings) - len(top)} more in findings.json")
    if len(lines) > MD_MAX_LINES:
        # Absolute bound: keep the header + table, truncate the list.
        lines = lines[: MD_MAX_LINES - 1] + ["- …remaining findings in findings.json"]
    return "\n".join(lines) + "\n"


# --- minimal JSON Schema subset validator -----------------------------------
# Supports exactly the keywords the findings schema uses: type (string or
# list), required, properties, items, enum. No dependencies, offline-safe.
_TYPE_CHECKS = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "null": lambda v: v is None,
}


def validate_schema(value: Any, schema: dict[str, Any], path: str = "$", errors: list[str] | None = None) -> list[str]:
    """Return a list of ``path: message`` errors; empty means the document fits."""
    if errors is None:
        errors = []
    declared = schema.get("type")
    if declared is not None:
        types = declared if isinstance(declared, list) else [declared]
        if not any(_TYPE_CHECKS.get(t, lambda v: True)(value) for t in types):
            errors.append(f"{path}: expected type {types}, got {type(value).__name__}")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                errors.append(f"{path}: missing required '{key}'")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                validate_schema(value[key], sub, f"{path}.{key}", errors)
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, entry in enumerate(value):
            validate_schema(entry, schema["items"], f"{path}[{index}]", errors)
    return errors


def load_schema(schema_path: Path | str | None = None) -> dict[str, Any]:
    return json.loads(Path(schema_path or SCHEMA_PATH).read_text(encoding="utf-8"))


def build_feedback_export(document: dict[str, Any]) -> dict[str, Any]:
    """Build an anonymous feedback summary with no code, paths, names, or values."""
    entries: list[dict[str, str]] = []
    for f in document.get("findings", ()):
        if isinstance(f, dict):
            entries.append(
                {
                    "rule": f"{f.get('agent', 'unknown')}.{f.get('type', 'unknown')}",
                    "class": str(f.get("class", "heuristic")),
                    "confidence": str(f.get("confidence", "high")),
                    "label": str(f.get("exception_kind") or "unlabelled"),
                }
            )
    return {
        "schema_version": str(document.get("schema_version", "1")),
        "false_positive_counts": dict(document.get("false_positive_counts") or {}),
        "downgraded_rules": list(document.get("downgraded_rules") or []),
        "entries": entries,
    }


def write_feedback_export(document: dict[str, Any], base_dir: Path | str) -> Path:
    """Write ``<base_dir>/<run_id>/feedback-export.json`` and mirror to ``latest/``."""
    base = Path(base_dir)
    run_id = str(document.get("run_id", ""))
    run_dir = base / run_id if run_id else base
    run_dir.mkdir(parents=not bool(), exist_ok=not bool())
    payload = json.dumps(build_feedback_export(document), indent=2, ensure_ascii=bool()) + "\n"
    out_path = run_dir / "feedback-export.json"
    out_path.write_text(payload, encoding="utf-8")
    if run_id:
        latest_dir = base / "latest"
        latest_dir.mkdir(parents=not bool(), exist_ok=not bool())
        shutil.copyfile(out_path, latest_dir / "feedback-export.json")
    return out_path


def write_findings(
    report: Any,
    findings_dir: Path | str,
    exceptions: list[Any] | None = None,
    today: Any = None,
    baseline: set[str] | list[str] | None = None,
    fail_on: str = "P1",
    write_brief: bool = not bool(),
    feedback_export: bool = bool(),
) -> tuple[Path, Path]:
    """Write the run's findings under ``<findings_dir>/<run_id>/`` and mirror
    them into ``<findings_dir>/latest/`` so an IDE can pin one stable path.

    Returns the per-run ``(findings.json, findings.md)`` paths. The ``latest``
    copy is byte-identical to the per-run pair; the copy is not a separate
    report and always reflects the most recent write. ``exceptions`` (operator
    exception entries) and ``today`` are forwarded to :func:`build_document`.
    """
    if isinstance(report, dict):
        data = report
    else:
        to_dict = getattr(report, "to_dict", None)
        if not callable(to_dict):
            raise TypeError("findings builder expects a mapping or an object with to_dict()")
        data = to_dict()
    document = build_document(data, exceptions=exceptions, today=today, baseline=baseline, fail_on=fail_on)
    base = Path(findings_dir)
    run_id = str(document.get("run_id") or "")
    run_dir = base / run_id if run_id else base
    run_dir.mkdir(parents=True, exist_ok=True)
    json_bytes = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    md_text = render_markdown(document)
    json_path = run_dir / "findings.json"
    md_path = run_dir / "findings.md"
    json_path.write_text(json_bytes, encoding="utf-8")
    md_path.write_text(md_text, encoding="utf-8")
    summary_path = run_dir / "summary.md"
    try:
        from core.report import render_summary_markdown
        summary_text = render_summary_markdown(data)
        summary_path.write_text(summary_text, encoding="utf-8")
    except Exception:
        summary_path = None
    try:
        from core.brief import write_fix_brief, write_pr_comment
        if write_brief:
            write_fix_brief(data, document, base, fail_on=fail_on)
        write_pr_comment(data, document, base, fail_on=fail_on)
        if feedback_export:
            write_feedback_export(document, base)
    except Exception:
        pass
    if run_id:
        latest_dir = base / "latest"
        latest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(json_path, latest_dir / "findings.json")
        shutil.copyfile(md_path, latest_dir / "findings.md")
        if summary_path and summary_path.is_file():
            shutil.copyfile(summary_path, latest_dir / "summary.md")
    return json_path, md_path

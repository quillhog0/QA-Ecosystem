"""Structured handoff packets. Roles exchange these objects only — never raw chat."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


class Verdict(str, Enum):
    PENDING = "PENDING"
    CLEAR = "CLEAR"
    VETO = "VETO"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


class Role(str, Enum):
    VESPER = "vesper"
    LEXI = "lexi"
    ARIA = "aria"
    RILEY = "riley"
    SABLE = "sable"
    NOVA = "nova"


class Stage(str, Enum):
    INIT = "init"
    DRIFT_CHECK = "drift_check"
    DESIGN = "design"
    ADVERSARIAL_PLAN = "adversarial_plan"
    PREFLIGHT = "preflight"
    EXECUTE = "execute"
    POST_AUDIT = "post_audit"
    CLOSE = "close"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid4().hex[:8]}"


@dataclass
class RoleResult:
    role: str
    stage: str
    verdict: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    iteration: int = 1
    timestamp: str = field(default_factory=utc_now)
    llm: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DriftFinding:
    kind: str
    path: str
    message: str
    severity: str = "medium"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DriftReport:
    verdict: str
    findings: list[DriftFinding] = field(default_factory=list)
    docs_files: list[str] = field(default_factory=list)
    code_files: list[str] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)
    notes: str = ""
    claims: list[str] = field(default_factory=list)
    llm_status: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "findings": [f.to_dict() for f in self.findings],
            "docs_files": self.docs_files,
            "code_files": self.code_files,
            "test_files": self.test_files,
            "notes": self.notes,
            "claims": self.claims,
            "llm_status": self.llm_status,
        }


@dataclass
class Scenario:
    id: str
    title: str
    steps: list[str]
    acceptance: list[str]
    negative: bool = False
    covered_by: list[str] = field(default_factory=list)
    score: int = 0
    covered_by_weak: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TestPlan:
    scenarios: list[Scenario] = field(default_factory=list)
    coverage_notes: str = ""
    assumptions: list[str] = field(default_factory=list)
    llm_status: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenarios": [s.to_dict() for s in self.scenarios],
            "coverage_notes": self.coverage_notes,
            "assumptions": self.assumptions,
            "llm_status": self.llm_status,
        }


@dataclass
class RedTeamReview:
    verdict: str
    gaps: list[str] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)
    root_cause: str = ""
    notes: str = ""
    llm_status: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyDecision:
    verdict: str
    blocked_reasons: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)
    command: list[str] = field(default_factory=list)
    allowed: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionResult:
    collected: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    exit_code: int = -1
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    command: list[str] = field(default_factory=list)
    isolated_root: str = ""
    install_status: str = "skipped"
    sandbox_status: str = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Keep report size bounded.
        data["stdout"] = _clip(self.stdout, 20_000)
        data["stderr"] = _clip(self.stderr, 8_000)
        return data


@dataclass
class PostAudit:
    lexi: DriftReport | None = None
    riley: RedTeamReview | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "lexi": self.lexi.to_dict() if self.lexi else None,
            "riley": self.riley.to_dict() if self.riley else None,
        }


@dataclass
class HandoffPacket:
    """Canonical packet passed between roles."""

    run_id: str
    stage: str
    from_role: str
    to_role: str
    project_root: str
    docs_root: str
    isolated_root: str = ""
    dry_run: bool = True
    allow_network: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    timestamp: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def next(
        self,
        *,
        stage: str,
        from_role: str,
        to_role: str,
        payload: dict[str, Any] | None = None,
    ) -> "HandoffPacket":
        hist = list(self.history)
        hist.append(
            {
                "stage": self.stage,
                "from": self.from_role,
                "to": self.to_role,
                "timestamp": self.timestamp,
            }
        )
        return HandoffPacket(
            run_id=self.run_id,
            stage=stage,
            from_role=from_role,
            to_role=to_role,
            project_root=self.project_root,
            docs_root=self.docs_root,
            isolated_root=self.isolated_root,
            dry_run=self.dry_run,
            allow_network=self.allow_network,
            payload=payload if payload is not None else dict(self.payload),
            history=hist,
        )


@dataclass
class FinalReport:
    run_id: str
    verdict: str
    escalation: str = ""
    project_root: str = ""
    docs_root: str = ""
    isolated_root: str = ""
    dry_run: bool = True
    sandbox: str = "unavailable"
    role_results: list[RoleResult] = field(default_factory=list)
    drift: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    post_audit: dict[str, Any] = field(default_factory=dict)
    mutation: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    finished_at: str = ""
    tested_tree: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["role_results"] = [r.to_dict() for r in self.role_results]
        return data


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"

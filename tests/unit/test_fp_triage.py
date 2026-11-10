"""LLM triage may only downgrade or annotate, and is a no-op when the LLM is off."""

from __future__ import annotations

from agents.adapters import complete, llm_enabled
from agents.triage import apply_triage, is_triage_candidate


def _finding(**kwargs):
    base = {
        "id": "f1",
        "fingerprint": "fp-1",
        "type": "drift",
        "priority": "P1",
        "verdict_effect": "VETO",
        "term": "rowIDs",
        "evidence": "documented identifier rowIDs is missing from code",
        "impact": "docs mention rowIDs",
        "class": "heuristic",
    }
    base.update(kwargs)
    return base


def test_mock_provider_returns_configured_response():
    cfg = {"llm": {"provider": "mock", "mock_response": '{"ok": true}'}, "dry_run": False}
    assert llm_enabled(cfg) is True
    assert complete(system_prompt="s", user_payload={"x": 1}, config=cfg) == '{"ok": true}'


def test_triage_is_noop_when_llm_is_off():
    findings = [_finding()]
    out, meta = apply_triage(findings, config={"llm": {"provider": "none"}, "dry_run": False})
    assert meta["status"] == "skipped"
    assert out[0]["priority"] == "P1"
    assert out[0].get("triage") is None


def test_triage_downgrades_eligible_finding_via_mock():
    findings = [_finding()]
    cfg = {
        "dry_run": False,
        "llm": {
            "provider": "mock",
            "mock_response": (
                '{"actions": [{"fingerprint": "fp-1", "action": "downgrade",'
                ' "reason": "prose placeholder, not an API"}]}'
            ),
        },
    }
    out, meta = apply_triage(findings, config=cfg)
    assert meta["status"] == "ok"
    assert out[0]["priority"] == "P2"
    assert out[0]["triage"]["action"] == "downgrade"
    assert "placeholder" in out[0]["triage"]["reason"]


def test_triage_never_creates_or_upgrades_and_refuses_evidenced_p0():
    p0 = _finding(priority="P0", type="test_failure", evidence="pytest failed: money path")
    assert is_triage_candidate(p0) is False
    p1 = _finding()
    cfg = {
        "dry_run": False,
        "llm": {
            "provider": "mock",
            "mock_response": (
                '{"actions": ['
                '{"fingerprint": "fp-1", "action": "upgrade", "priority": "P0", "reason": "nope"},'
                '{"fingerprint": "new-one", "action": "create", "reason": "invented"}'
                ']}'
            ),
        },
    }
    out, meta = apply_triage([p1], config=cfg)
    assert len(out) == 1
    assert out[0]["priority"] == "P1"
    # P0 with evidence is not even sent, so it cannot be cleared.
    kept, _ = apply_triage([p0], config=cfg)
    assert kept[0]["priority"] == "P0"


def test_triage_unavailable_leaves_findings_unchanged():
    findings = [_finding()]
    cfg = {"dry_run": False, "llm": {"provider": "mock", "mock_response": ""}}
    out, meta = apply_triage(findings, config=cfg)
    assert meta["status"] == "skipped"
    assert out[0]["priority"] == "P1"

from __future__ import annotations

import pytest

from agents.adapters import llm_enabled, parse_yaml_block


def test_parse_yaml_block_plain_mapping():
    assert parse_yaml_block("verdict: CLEAR\nscore: 3\n") == {"verdict": "CLEAR", "score": 3}


def test_parse_yaml_block_strips_code_fences():
    fenced = "```yaml\nverdict: VETO\nfindings:\n  - kind: drift\n```"
    parsed = parse_yaml_block(fenced)
    assert parsed["verdict"] == "VETO"
    assert parsed["findings"] == [{"kind": "drift"}]

    bare_fence = "```\na: 1\n```"
    assert parse_yaml_block(bare_fence) == {"a": 1}


def test_parse_yaml_block_non_mapping_returns_empty_dict():
    assert parse_yaml_block("") == {}
    assert parse_yaml_block("- 1\n- 2") == {}  # list, not dict
    assert parse_yaml_block("just a scalar") == {}
    assert parse_yaml_block("```\n- 1\n- 2\n```") == {}  # fenced list


def test_llm_enabled_provider_none_or_missing_is_off():
    assert llm_enabled({}) is False
    assert llm_enabled({"llm": {"provider": "none"}}) is False
    assert llm_enabled({"llm": {"provider": "NONE"}}) is False
    assert llm_enabled({"llm": {"provider": ""}}) is False
    assert llm_enabled({"llm": {"provider": "heuristic"}}) is False
    assert llm_enabled({"llm": {"provider": "off"}}) is False
    assert llm_enabled({"llm": {}}) is False


def test_llm_enabled_dry_run_gates_calls_unless_call_in_dry_run():
    local = {"llm": {"provider": "local", "base_url": "http://127.0.0.1:11434/v1"}}
    assert llm_enabled({**local, "dry_run": True}) is False  # dry_run defaults to True
    assert llm_enabled({**local, "dry_run": False}) is True
    assert llm_enabled({**local, "llm": {**local["llm"], "call_in_dry_run": True}}) is True


def test_llm_enabled_local_requires_base_url_and_unknown_provider_is_off():
    assert llm_enabled({"llm": {"provider": "local"}, "dry_run": False}) is False
    assert llm_enabled({"llm": {"provider": "foobearer"}, "dry_run": False}) is False


def test_llm_enabled_openrouter_requires_api_key(monkeypatch: pytest.MonkeyPatch):
    cfg = {"llm": {"provider": "openrouter"}, "dry_run": False}
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert llm_enabled(cfg) is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "placeholder-not-a-real-key")
    assert llm_enabled(cfg) is True


def test_parse_yaml_block_malformed_yaml_returns_empty_dict():
    assert parse_yaml_block("key: [unclosed\n  - : :") == {}
    assert parse_yaml_block("```yaml\na: b: c\n```") == {}

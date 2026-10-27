from __future__ import annotations

from core.runner import sanitized_env


def test_sanitized_env_strips_secrets_and_sets_proxy(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("GH_TOKEN", "t")
    monkeypatch.setenv("DB_PASSWORD", "p")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("CUSTOM_KEY", "x")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = sanitized_env(allow_network=False)
    joined = " ".join(env.keys()).upper()
    assert "SECRET" not in joined
    assert "TOKEN" not in joined
    assert "PASSWORD" not in joined
    assert not any("KEY" in name.upper() for name in env)
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "DB_PASSWORD" not in env
    assert "OPENAI_API_KEY" not in env
    assert "CUSTOM_KEY" not in env
    assert env["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert env["ALL_PROXY"] == "http://127.0.0.1:9"
    assert env.get("NO_NETWORK") == "1"


def test_sanitized_env_allow_network_omits_dead_proxy(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    env = sanitized_env(allow_network=True)
    assert "HTTP_PROXY" not in env
    assert "NO_NETWORK" not in env

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


def test_parse_counts_ignores_error_words_in_body():
    from core.runner import _parse_counts

    output = (
        "collected 2 items\n"
        "test_app.py Handled 3 error states in payload PASSED\n"
        "test_app.py Second test PASSED\n"
        "============================== 2 passed in 0.05s ==============================="
    )
    collected, passed, failed, skipped, errors = _parse_counts(output)
    assert collected == 2
    assert passed == 2
    assert errors == 0


def test_parse_counts_collection_error_leaves_collected_zero():
    from core.runner import _parse_counts

    output = (
        "collected 0 items / 1 error\n"
        "==================================== ERRORS ====================================\n"
        "ERROR collecting test_broken.py\n"
        "=============================== 1 error in 0.08s ==============================="
    )
    collected, passed, failed, skipped, errors = _parse_counts(output)
    assert collected == 0
    assert errors == 1


def test_sanitized_env_strict_allowlist_and_blocks_pytest_python_vars(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-s --tb=long")
    monkeypatch.setenv("PYTHONSTARTUP", "/tmp/evil.py")
    monkeypatch.setenv("PYTHONPATH", "/tmp/evil_path")
    monkeypatch.setenv("PYTEST_DB_URI", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("PYTHON_CUSTOM_FLAG", "1")
    monkeypatch.setenv("USER", "realuser")
    monkeypatch.setenv("LOGNAME", "realuser")
    monkeypatch.setenv("HOME", "/home/realuser")
    monkeypatch.setenv("VIRTUAL_ENV", "/home/realuser/.venv")

    env = sanitized_env(allow_network=False)
    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert "PYTEST_ADDOPTS" not in env
    assert "PYTHONSTARTUP" not in env
    assert "PYTHONPATH" not in env
    assert "PYTEST_DB_URI" not in env
    assert "PYTHON_CUSTOM_FLAG" not in env
    assert "USER" not in env
    assert "LOGNAME" not in env
    assert "HOME" not in env
    assert "VIRTUAL_ENV" not in env


def test_run_pytest_rejects_pathsep_and_resolves_paths(tmp_path, monkeypatch):
    import os
    import pytest
    from core.runner import run_pytest

    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()
    (iso / "test_ok.py").write_text(
        "import os\ndef test_env():\n    assert 'PYTEST_ADDOPTS' not in os.environ\n",
        encoding="utf-8",
    )

    bad_iso = tmp_path / f"bad{os.pathsep}iso"
    with pytest.raises(ValueError, match="path separator"):
        run_pytest(bad_iso, work_dir=work, allow_network=False, config={"sandbox": {"mode": "off"}})

    bad_work = tmp_path / f"bad{os.pathsep}work"
    with pytest.raises(ValueError, match="path separator"):
        run_pytest(iso, work_dir=bad_work, allow_network=False, config={"sandbox": {"mode": "off"}})

    # Symlink resolution and host PYTEST_ADDOPTS isolation
    iso_link = tmp_path / "iso_link"
    work_link = tmp_path / "work_link"
    iso_link.symlink_to(iso)
    work_link.symlink_to(work)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--nonexistent-pytest-option")
    monkeypatch.setenv("PYTHONPATH", "/host/should/not/pass")

    res = run_pytest(iso_link, work_dir=work_link, allow_network=False, config={"sandbox": {"mode": "off"}})
    assert res.exit_code == 0
    assert res.passed == 1
    assert res.isolated_root == str(iso.resolve())



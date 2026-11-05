from __future__ import annotations

from core.runner import _find_summary_line, sanitized_env


def test_find_summary_line_returns_text_when_nothing_was_printed():
    """core/runner.py:106 - the empty-output branch must be the empty string: the
    caller writes this value into the report as the pytest summary line, and None
    would surface as the literal text "None"."""
    assert _find_summary_line("") == ""
    assert _find_summary_line("\n   \n\t\n") == ""
    assert _find_summary_line("collected 3 items") == "collected 3 items"
    assert _find_summary_line("=== 3 passed in 0.12s ===") == "=== 3 passed in 0.12s ==="


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


def test_run_pytest_timeout_preserves_empty_stdout(tmp_path, monkeypatch):
    """On timeout with empty captured output, stdout must stay '' (guards `exc.stdout or \"\"`)."""
    import subprocess
    from core.runner import run_pytest

    iso = tmp_path / "iso"
    work = tmp_path / "work"
    iso.mkdir()
    work.mkdir()

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="pytest", timeout=1, output="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    res = run_pytest(iso, work_dir=work, allow_network=False, timeout=1, config={"sandbox": {"mode": "off"}})
    assert res.exit_code == 124
    assert res.stdout == ""
    assert "TIMEOUT" in res.stderr


def test_run_pytest_injects_src_layout_import_roots(tmp_path, monkeypatch):
    """Cause 3: PYTHONPATH gains the copy's <copy>/src import root, after the
    work dir and copy root, so a src-layout package resolves to the copy."""
    import os
    from core.runner import run_pytest

    iso = tmp_path / "iso"
    work = tmp_path / "work"
    (iso / "src" / "pkg").mkdir(parents=True)
    (iso / "src" / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (iso / "tests").mkdir()
    (iso / "tests" / "test_x.py").write_text(
        "import pkg\n\ndef test_v():\n    assert pkg.VALUE == 1\n", encoding="utf-8"
    )
    work.mkdir()

    captured: dict[str, dict] = {}

    class FakeProc:
        returncode = 0
        stdout = "collected 1 item\n========= 1 passed in 0.01s ========="
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr("subprocess.run", fake_run)
    run_pytest(iso, work_dir=work, allow_network=False, config={"sandbox": {"mode": "off"}})

    entries = captured["env"].get("PYTHONPATH", "").split(os.pathsep)
    iso_root = str(iso.resolve())
    src_root = str(iso.resolve() / "src")
    assert iso_root in entries
    assert src_root in entries
    # copy root precedes its src root; both sit on PYTHONPATH, which the
    # interpreter searches before site-packages, so the copy shadows host pkgs.
    assert entries.index(iso_root) < entries.index(src_root)


def test_run_pytest_src_layout_imports_the_copy(tmp_path):
    """Cause 3 end-to-end: a src-layout test importing its own package resolves
    to the isolated copy instead of failing with ModuleNotFoundError."""
    from core.runner import run_pytest

    iso = tmp_path / "iso"
    work = tmp_path / "work"
    (iso / "src" / "ownpkg").mkdir(parents=True)
    (iso / "src" / "ownpkg" / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    (iso / "tests").mkdir()
    (iso / "tests" / "test_own.py").write_text(
        "import ownpkg\n\n\ndef test_v():\n    assert ownpkg.VALUE == 42\n", encoding="utf-8"
    )

    res = run_pytest(iso, work_dir=work, allow_network=False, config={"sandbox": {"mode": "off"}})
    assert res.collected == 1
    assert res.passed == 1
    assert res.errors == 0


def test_run_pytest_flaky_test_detected_on_rerun(tmp_path):
    """A test that fails on the first run and passes on the rerun is marked FLAKY."""
    from core.runner import run_pytest

    iso = tmp_path / "iso"
    work = tmp_path / "work"
    (iso / "tests").mkdir(parents=True)
    (iso / "tests" / "test_flaky.py").write_text(
        "from pathlib import Path\n\n"
        "def test_sometimes_fails():\n"
        "    marker = Path('.flaky_marker')\n"
        "    if not marker.exists():\n"
        "        marker.write_text('1', encoding='utf-8')\n"
        "        assert False, 'transient first-run failure'\n"
        "    assert True\n",
        encoding="utf-8",
    )

    res = run_pytest(iso, work_dir=work, allow_network=False, config={"sandbox": {"mode": "off"}})
    assert res.collected == 1
    assert res.failed == 0
    assert res.product_failed == 0
    assert res.passed == 1
    assert res.flaky == 1
    assert res.exit_code == 0
    assert res.status == "FLAKY"
    assert len(res.flaky_tests) == 1
    assert res.flaky_tests[0]["classification"] == "flaky"
    assert "test_sometimes_fails" in res.flaky_tests[0]["node"]




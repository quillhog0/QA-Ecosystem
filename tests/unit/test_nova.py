from __future__ import annotations

import agents.nova as nova
from core.packets import ExecutionResult
from tests.support import mini_project


def test_execute_skips_install_when_config_omits_install_deps(tmp_path, monkeypatch):
    """nova.py:22 - install_deps defaults to False when the key is absent, so no
    virtualenv is built and the install status stays 'skipped'."""
    root = mini_project(tmp_path / "proj", with_tests=True, docs="# Doc\n\n`add`.\n")
    work = tmp_path / "work"
    work.mkdir()

    calls = {"venv": 0}

    def fake_prepare(*args, **kwargs):
        calls["venv"] += 1
        return None, "no dependency manifest"

    monkeypatch.setattr(nova, "prepare_venv", fake_prepare)
    monkeypatch.setattr(
        nova, "run_pytest", lambda *a, **k: ExecutionResult(exit_code=0, collected=1, passed=1)
    )

    result = nova.execute(
        root,
        work_dir=work,
        allow_network=False,
        config={"llm": {"provider": "none"}, "dry_run": True},
    )

    assert calls["venv"] == 0
    assert result.install_status == "skipped"

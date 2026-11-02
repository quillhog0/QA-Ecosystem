from __future__ import annotations

import yaml

from core.packets import HandoffPacket, Stage


def test_handoff_next_preserves_run_id_and_sets_roles():
    packet = HandoffPacket(
        run_id="run-1",
        stage=Stage.INIT.value,
        from_role="vesper",
        to_role="lexi",
        project_root="/proj",
        docs_root="/docs",
        isolated_root="/iso",
        payload={"k": "v"},
    )
    nxt = packet.next(
        stage=Stage.DRIFT_CHECK.value,
        from_role="vesper",
        to_role="lexi",
        payload={"drift": True},
    )
    assert nxt.run_id == "run-1"
    assert nxt.from_role == "vesper"
    assert nxt.to_role == "lexi"
    assert nxt.stage == Stage.DRIFT_CHECK.value
    assert nxt.project_root == "/proj"
    assert nxt.payload == {"drift": True}
    assert nxt.history[0]["from"] == "vesper"
    assert nxt.history[0]["to"] == "lexi"
    assert nxt.history[0]["stage"] == Stage.INIT.value


def test_handoff_yaml_roundtrip():
    packet = HandoffPacket(
        run_id="run-yaml",
        stage=Stage.DESIGN.value,
        from_role="aria",
        to_role="riley",
        project_root="/proj",
        docs_root="/docs",
        isolated_root="/iso",
        payload={"n": 1},
        history=[{"stage": "init", "from": "vesper", "to": "lexi", "timestamp": "t"}],
    )
    dumped = yaml.safe_dump(packet.to_dict(), sort_keys=False)
    loaded = yaml.safe_load(dumped)
    restored = HandoffPacket(**loaded)
    assert restored.to_dict() == packet.to_dict()
    assert restored.run_id == "run-yaml"
    assert restored.from_role == "aria"
    assert restored.to_role == "riley"


def test_write_reports_returns_written_paths(tmp_path):
    from core.packets import FinalReport, Verdict
    from core.report import exit_code_for, write_reports

    report = FinalReport(run_id="run-paths", verdict=Verdict.CLEAR.value)
    paths = write_reports(report, tmp_path / "out")
    assert paths is not None
    md_path, yml_path = paths
    assert md_path.is_file()
    assert yml_path.is_file()
    assert exit_code_for(Verdict.CLEAR.value) == 0
    assert exit_code_for(Verdict.VETO.value) == 1


def test_exit_code_for_every_verdict():
    """BLOCKED_ENV is never a pass and never shares the veto exit code, so CI can
    tell an accusation apart from an environment that could not run the tests."""
    from core.report import exit_code_for

    expected = {
        "CLEAR": 0,
        "VETO": 1,
        "ESCALATE": 1,
        "BLOCKED_ENV": 2,
        "PENDING": 1,
        "BLOCK": 1,
        "": 1,
    }
    for verdict, code in expected.items():
        assert exit_code_for(verdict) == code, verdict


def test_blocked_env_is_distinct_from_the_internal_sable_block():
    from core.packets import Verdict

    assert Verdict.BLOCKED_ENV.value == "BLOCKED_ENV"
    assert Verdict.BLOCKED_ENV.value != Verdict.BLOCK.value
    assert Verdict("BLOCKED_ENV") is Verdict.BLOCKED_ENV


def test_blocked_env_report_states_that_it_is_not_a_pass():
    from core.packets import FinalReport, Verdict
    from core.report import render_markdown

    report = FinalReport(run_id="run-blocked", verdict=Verdict.BLOCKED_ENV.value)
    text = render_markdown(report)
    assert "- **verdict:** BLOCKED_ENV" in text
    assert "environment blocked" in text
    assert "not a pass" in text
    assert "no defect is claimed" in text


def test_clear_report_has_no_environment_note():
    from core.packets import FinalReport, Verdict
    from core.report import render_markdown

    text = render_markdown(FinalReport(run_id="run-clear", verdict=Verdict.CLEAR.value))
    assert "environment blocked" not in text


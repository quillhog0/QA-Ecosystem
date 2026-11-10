"""GitHub Action wrapper: outputs and findings must be written before a VETO exit."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FINISH = ROOT / "scripts" / "gha_finish.sh"
PRINTER = ROOT / "scripts" / "gha_print_veto_findings.py"


def _gha_bash(script: str, *, env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the way GitHub Actions invokes bash: ``bash -eo pipefail``."""
    return subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
    )


def _legacy_grep_script(qa_out: Path) -> str:
    # The v0.8.2 wrapper (action.yml after `set -e`): a missing
    # feedback_export= line makes grep fail and aborts before GITHUB_OUTPUT.
    return textwrap.dedent(
        f"""
        set -e
        feedback_export_path=$(grep -m1 '^feedback_export=' {qa_out.as_posix()!s} | cut -d= -f2-)
        echo "verdict=reached" >> "$GITHUB_OUTPUT"
        echo "feedback_export=$feedback_export_path" >> "$GITHUB_OUTPUT"
        exit 1
        """
    ).strip()


def _write_findings(dir_path: Path) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1",
        "verdict": "VETO",
        "findings": [
            {
                "agent": "nova",
                "type": "test_failure",
                "priority": "P1",
                "file": "tests/test_server.py",
                "line": 42,
                "evidence": "RuntimeError: boom on loopback",
                "impact": "A promised behaviour is red.",
            },
            {
                "agent": "sable",
                "type": "policy",
                "priority": "P0",
                "file": "src/pay.py",
                "line": 9,
                "evidence": "secret file copied into the isolated tree",
            },
            {
                "agent": "lexi",
                "type": "drift",
                "priority": "P3",
                "file": "README.md",
                "line": 3,
                "evidence": "docs mention decision",
            },
        ],
    }
    js = dir_path / "findings.json"
    js.write_text(json.dumps(payload), encoding="utf-8")
    (dir_path / "findings.md").write_text("# Findings\n\nP1 and P0 present.\n", encoding="utf-8")
    return js


def _base_out(tmp: Path, *, feedback: str | None, findings: Path | None = None, verdict: str = "VETO") -> Path:
    report = tmp / "qa-reports" / "run.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("# report\n", encoding="utf-8")
    summary = tmp / "qa-reports" / "run" / "summary.md"
    if findings is None:
        (tmp / "qa-reports" / "run").mkdir(parents=True, exist_ok=True)
    else:
        summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("# QA Gate Summary\n\n- **Verdict:** %s\n" % verdict, encoding="utf-8")
    lines = [
        f"verdict={verdict}",
        f"report={report}",
        f"summary={summary}",
        f"fix_brief={tmp / 'qa-reports' / 'run' / 'fix-brief.md'}",
        f"pr_comment={tmp / 'qa-reports' / 'run' / 'pr-comment.md'}",
    ]
    if feedback is not None:
        lines.append(f"feedback_export={feedback}")
    if findings is not None:
        lines.append(f"findings={findings.with_suffix('.md')}")
    qa_out = tmp / "qa-out.txt"
    qa_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return qa_out


def test_legacy_grep_aborts_under_set_e_when_feedback_export_missing(tmp_path: Path) -> None:
    qa_out = _base_out(tmp_path, feedback=None)
    gha_out = tmp_path / "gha_output"
    gha_out.write_text("", encoding="utf-8")
    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = str(gha_out)
    proc = _gha_bash(_legacy_grep_script(qa_out), env=env, cwd=tmp_path)
    assert proc.returncode == 1
    assert "verdict=reached" not in gha_out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "exit_code,verdict,feedback",
    [
        (0, "CLEAR", ""),
        (1, "VETO", None),  # key absent: the factory-gate bug
        (1, "VETO", "/tmp/feedback-export.json"),
        (2, "BLOCKED_ENV", ""),
    ],
)
def test_gha_finish_writes_outputs_then_exits_with_qa_code(
    tmp_path: Path, exit_code: int, verdict: str, feedback: str | None
) -> None:
    findings = _write_findings(tmp_path / "qa-reports" / "run") if verdict == "VETO" else None
    qa_out = _base_out(tmp_path, feedback=feedback, findings=findings, verdict=verdict)
    gha_out = tmp_path / "gha_output"
    gha_out.write_text("", encoding="utf-8")
    summary = tmp_path / "step_summary"
    env = os.environ.copy()
    env["GITHUB_OUTPUT"] = str(gha_out)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    env["QA_SUMMARY"] = "true"
    proc = _gha_bash(
        f'set -e; bash "{FINISH}" "{qa_out}" {exit_code}',
        env=env,
        cwd=tmp_path,
    )
    assert proc.returncode == exit_code, proc.stderr
    written = gha_out.read_text(encoding="utf-8")
    assert f"verdict={verdict}" in written
    assert "report=" in written
    assert "summary=" in written
    assert "fix_brief=" in written
    assert "pr_comment=" in written
    assert "feedback_export=" in written
    assert "sarif=" in written
    if feedback:
        assert f"feedback_export={feedback}" in written
    else:
        # empty or absent key both become an empty value, never a crash
        assert "feedback_export=\n" in written or written.strip().endswith("feedback_export=")
    step = summary.read_text(encoding="utf-8") if summary.exists() else ""
    if verdict == "BLOCKED_ENV":
        assert "QA environment blocked" in proc.stdout
    if verdict == "VETO":
        assert "P1 nova tests/test_server.py:42" in proc.stdout
        assert "RuntimeError: boom on loopback" in proc.stdout
        assert "P0 sable src/pay.py:9" in proc.stdout
        assert "P3 lexi" not in proc.stdout
        assert "QA VETO findings" in step
        assert "tests/test_server.py:42" in step
    assert "Verdict" in step or verdict == "VETO" and "QA VETO findings" in step


def test_print_veto_findings_prefers_json(tmp_path: Path) -> None:
    js = _write_findings(tmp_path)
    proc = subprocess.run(
        ["python3", str(PRINTER), str(js)],
        check=True,
        text=True,
        capture_output=True,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines[0].startswith("P1 nova tests/test_server.py:42")
    assert any(ln.startswith("P0 sable src/pay.py:9") for ln in lines)
    assert all("P3" not in ln for ln in lines)


def test_action_yml_delegates_to_gha_finish_and_uploads_always() -> None:
    text = (ROOT / "action.yml").read_text(encoding="utf-8")
    assert "scripts/gha_finish.sh" in text
    assert "if: always()" in text
    assert "actions/upload-artifact@v4" in text
    # The fatal unguarded grep must not remain in the wrapper.
    assert "feedback_export_path=$(grep -m1 '^feedback_export='" not in text

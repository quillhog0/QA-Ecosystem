"""Security regression tests: tested project must never supply TypeScript compiler."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from core import toolchain


def test_ensure_ts_tooling_never_loads_or_caches_project_node_modules(tmp_path: Path, monkeypatch):
    """Assert ensure_ts_tooling ignores project node_modules/typescript."""
    cache_dir = tmp_path / "cache"
    tooling_dir = cache_dir / "tooling"
    monkeypatch.setenv("QA_TOOLING_DIR", str(tooling_dir))

    proj = tmp_path / "malicious_proj"
    malicious_ts = proj / "node_modules" / "typescript" / "lib" / "typescript.js"
    malicious_ts.parent.mkdir(parents=True)
    malicious_ts.write_text("throw new Error('MALICIOUS EXPLOIT');\n", encoding="utf-8")

    # When QA tooling has no vendor and no valid cached compiler:
    with patch("pathlib.Path.is_file", autospec=True) as mock_is_file:
        def custom_is_file(self):
            # Vendor compiler is missing
            if "vendor" in str(self):
                return False
            return os.path.isfile(str(self))
        mock_is_file.side_effect = custom_is_file

        result = toolchain.ensure_ts_tooling(project_root=proj)
        # Must return None, never the malicious compiler
        assert result is None
        # The malicious compiler must NEVER have been copied to tooling_dir
        target_ts = tooling_dir / "typescript.js"
        assert not target_ts.exists()


def test_ts_analyzer_never_requires_project_node_modules(tmp_path: Path, monkeypatch):
    """Assert ts_analyzer.js ignores malicious project node_modules/typescript."""
    node_bin = shutil.which("node")
    if not node_bin:
        pytest.skip("node not available")

    analyzer_js = Path(__file__).resolve().parents[2] / "agents" / "ts_analyzer.js"

    # Set up project with malicious node_modules/typescript
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "package.json").write_text("{}", encoding="utf-8")
    malicious_ts = proj / "node_modules" / "typescript" / "lib" / "typescript.js"
    malicious_ts.parent.mkdir(parents=True)
    malicious_ts.write_text("throw new Error('SECURITY_BREACH_MALICIOUS_NODE_MODULES');\n", encoding="utf-8")

    # A test file
    (proj / "tests").mkdir()
    (proj / "tests" / "sample.test.ts").write_text("it('works', () => {});", encoding="utf-8")

    # Run ts_analyzer.js against this project
    # Since vendor/typescript.js exists in the repo, it should use vendor and NOT execute the malicious file
    proc = subprocess.run(
        [node_bin, str(analyzer_js), "--project", str(proj)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert "SECURITY_BREACH_MALICIOUS_NODE_MODULES" not in proc.stderr
    assert "SECURITY_BREACH_MALICIOUS_NODE_MODULES" not in proc.stdout
    assert proc.returncode == 0


def test_ts_missing_tooling_returns_blocked_env(tmp_path: Path, monkeypatch):
    """When TS tooling is missing or corrupted, gate halts with BLOCKED_ENV."""
    empty_tooling = tmp_path / "empty_tooling"
    empty_tooling.mkdir()
    monkeypatch.setenv("QA_TOOLING_DIR", str(empty_tooling))

    with patch("pathlib.Path.is_file", autospec=True) as mock_is_file:
        def custom_is_file(self):
            if "vendor" in str(self) or "empty_tooling" in str(self):
                return False
            return os.path.isfile(str(self))
        mock_is_file.side_effect = custom_is_file

        assert toolchain.ensure_ts_tooling() is None


def test_ensure_ts_tooling_size_and_sha_branches(tmp_path: Path, monkeypatch):
    """Kill mutants in ensure_ts_tooling size checks and SHA comparisons."""
    tooling_dir = tmp_path / "tooling"
    tooling_dir.mkdir()
    monkeypatch.setenv("QA_TOOLING_DIR", str(tooling_dir))

    target_ts = tooling_dir / "typescript.js"
    sha_file = tooling_dir / "typescript.sha256"

    # Exact size 1000 bytes: must NOT be accepted as valid compiler (> 1000 required)
    import hashlib
    content_1000 = b"x" * 1000
    hash_1000 = hashlib.sha256(content_1000).hexdigest()
    target_ts.write_bytes(content_1000)
    with patch.object(toolchain, "PINNED_TS_SHA256", hash_1000):
        with patch("pathlib.Path.is_file", autospec=True) as mock_is_file:
            def custom_is_file(self):
                if "vendor" in str(self):
                    return False
                return os.path.isfile(str(self))
            mock_is_file.side_effect = custom_is_file
            assert toolchain.ensure_ts_tooling() is None

    # Size > 1000 bytes, actual_sha == PINNED_TS_SHA256, sha_file absent:
    # Must accept via actual_sha == PINNED_TS_SHA256
    import hashlib
    content = b"valid compiler " * 200
    actual_hash = hashlib.sha256(content).hexdigest()
    target_ts.write_bytes(content)
    with patch.object(toolchain, "PINNED_TS_SHA256", actual_hash):
        if sha_file.exists():
            sha_file.unlink()
        assert toolchain.ensure_ts_tooling() == tooling_dir

    # Size > 1000 bytes, actual_sha == expected_sha (from sha_file), but PINNED_TS_SHA256 differs:
    # Must accept via actual_sha == expected_sha
    sha_file.write_text(actual_hash, encoding="utf-8")
    with patch.object(toolchain, "PINNED_TS_SHA256", "different_sha_00000000000000000000000000"):
        assert toolchain.ensure_ts_tooling() == tooling_dir

    # Size > 1000 bytes, but neither sha matches:
    sha_file.write_text("corrupted_sha", encoding="utf-8")
    with patch.object(toolchain, "PINNED_TS_SHA256", "different_sha_00000000000000000000000000"):
        with patch("pathlib.Path.is_file", autospec=True) as mock_is_file:
            def custom_is_file(self):
                if "vendor" in str(self):
                    return False
                return os.path.isfile(str(self))
            mock_is_file.side_effect = custom_is_file
            assert toolchain.ensure_ts_tooling() is None


def test_orchestrator_ts_tooling_blocked_env(tmp_path: Path):
    """Kill mutants in orchestrator lines 452-463: missing TS tooling sets BLOCKED_ENV."""
    from core.orchestrator import Vesper
    from core.packets import Verdict

    proj = tmp_path / "ts_proj"
    proj.mkdir()
    docs = proj / "docs"
    docs.mkdir()
    (proj / "package.json").write_text('{"name": "test-pkg"}', encoding="utf-8")

    vesper = Vesper(config={"sandbox": "off"})

    with patch("core.toolchain.check_node_engine", return_value=(True, None)), \
         patch("core.toolchain.ensure_ts_tooling", return_value=None):
        report = vesper.run(project=proj, docs=docs)
        assert report.verdict == Verdict.BLOCKED_ENV.value
        assert "missing TypeScript tooling" in report.post_audit.get("root_cause", "")

    # Node engine failure branch
    with patch("core.toolchain.check_node_engine", return_value=(False, "mismatch required Node 22")):
        report = vesper.run(project=proj, docs=docs)
        assert report.verdict == Verdict.BLOCKED_ENV.value
        assert "node engine precondition" in report.post_audit.get("root_cause", "")


def test_riley_and_sable_ts_tooling_none_or_missing(tmp_path: Path):
    """Kill mutants in riley and sable when ensure_ts_tooling returns None or missing file."""
    from agents.riley import _detect_ts_weak_tests
    from policies.sable import _scan_ts_for_risks

    # ensure_ts_tooling returns None
    with patch("core.toolchain.ensure_ts_tooling", return_value=None):
        assert _detect_ts_weak_tests(tmp_path) == {}
        assert _scan_ts_for_risks(tmp_path) == []

    # ensure_ts_tooling returns directory where typescript.js does not exist
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir(exist_ok=True)
    with patch("core.toolchain.ensure_ts_tooling", return_value=empty_dir):
        assert _detect_ts_weak_tests(tmp_path) == {}
        assert _scan_ts_for_risks(tmp_path) == []


def test_orchestrator_drift_veto_with_blocked_sandbox(tmp_path: Path):
    """Kill mutants in orchestrator line 454 (node_engine_blocked=False) and line 456 (not blocked_reason and)."""
    from core.orchestrator import Vesper
    from core.packets import DriftReport, DriftFinding, Verdict

    proj = tmp_path / "ts_drift_proj"
    proj.mkdir()
    docs = proj / "docs"
    docs.mkdir()
    (proj / "package.json").write_text('{"name": "test-pkg"}', encoding="utf-8")

    # Sandbox required, but get_sandbox_status returns failure
    vesper = Vesper(config={"sandbox": "required"})

    high_finding = DriftFinding(
        kind="missing_cli_flag",
        path="--flag",
        severity="high",
        message="high drift",
    )
    drift_veto_report = DriftReport(findings=[high_finding], verdict=Verdict.VETO.value)

    with patch("core.sandbox.get_sandbox_status", return_value=(False, "bwrap", "bwrap binary missing")), \
         patch("agents.lexi.inspect", return_value=drift_veto_report), \
         patch("core.toolchain.check_node_engine", return_value=(False, "unsupported node")), \
         patch("core.toolchain.ensure_ts_tooling", return_value=None):
        report = vesper.run(project=proj, docs=docs)
        # Drift VETO must outrank sandbox blocked_env when node_engine_blocked is False initially
        assert report.verdict == Verdict.VETO.value
        assert "drift veto" in report.mutation.get("reason", "")


def test_orchestrator_loop_returns_none_explicitly(tmp_path: Path):
    """Verify Vesper._loop returns None on early return paths (line 452 equivalent mutant guard)."""
    from core.orchestrator import Vesper
    from core.packets import FinalReport, HandoffPacket, PolicyDecision, Verdict
    from core.isolation import create_isolation

    proj = tmp_path / "proj"
    proj.mkdir()
    docs = proj / "docs"
    docs.mkdir()
    iso_dir = tmp_path / "iso"
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    isolation = create_isolation(proj, iso_dir, method="copy", read_only=False)
    packet = HandoffPacket(
        run_id="test-run",
        stage="init",
        from_role="vesper",
        to_role="lexi",
        project_root=str(proj),
        docs_root=str(docs),
        isolated_root=str(isolation.isolated_root),
    )
    report = FinalReport(run_id="test-run", verdict="PENDING", sandbox="bwrap")
    vesper = Vesper(config={"sandbox": "auto"})

    relaxed_policy = PolicyDecision(
        verdict=Verdict.CLEAR.value,
        warnings=["subprocess in test"],
    )
    with patch("policies.sable.evaluate_preflight", return_value=relaxed_policy), \
         patch("core.sandbox.get_sandbox_status", return_value=(False, "unavailable", "disappeared")):
        ret = vesper._loop(packet, isolation, work_dir, report)
        assert ret is None
        assert report.verdict == Verdict.VETO.value





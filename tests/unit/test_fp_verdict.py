"""Verdict is derived only from findings (v0.8.4).

A VETO verdict without a VETO finding is impossible, and a VETO finding
always yields VETO. The KeyHive-shaped privacy fixture (a try/except that
swallows assertions in a privacy test) must VETO via a P1 finding with
file:line. Uncovered or weak-only claims alone stay P2 and must not VETO.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.findings import build_document, check_verdict_consistent
from core.orchestrator import Vesper
from core.packets import Verdict
from tests.support import write_code, write_docs


def _write_test(root: Path, name: str, content: str) -> None:
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / name).write_text(content, encoding="utf-8")


def _privacy_swallowed_project(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    write_code(
        root,
        {
            "store.py": (
                "def stored_payload() -> str:\n"
                "    return 'contact user@example.com'\n"
            )
        },
    )
    write_docs(
        root,
        "# Privacy\n\n"
        "The product must never store a plaintext email address anywhere.\n",
    )
    _write_test(
        root,
        "test_fulfillment.py",
        "from store import stored_payload\n"
        "\n"
        "\n"
        "def test_privacy_no_plaintext_email_anywhere():\n"
        "    try:\n"
        "        assert 'user@example.com' not in stored_payload()\n"
        "    except Exception:\n"
        "        pass\n",
    )
    return root


def _config(tmp_path: Path) -> dict:
    return {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "reports"),
        "findings_dir": str(tmp_path / "findings"),
        "llm": {"provider": "none"},
        "isolation_method": "copy",
        "sandbox": {"mode": "auto"},
        "mutation": {"enabled": False},
    }


def test_privacy_swallowed_fixture_vetoes_with_p1_file_line(tmp_path: Path) -> None:
    project = _privacy_swallowed_project(tmp_path / "privacy")
    report = Vesper(_config(tmp_path)).run(project, project / "docs")
    assert report.verdict == Verdict.VETO.value

    doc = build_document(report.to_dict(), fail_on="P1")
    check_verdict_consistent(doc["verdict"], doc["findings"])
    veto = [f for f in doc["findings"] if f.get("verdict_effect") == "VETO"]
    assert veto, "VETO must name the finding that caused it"
    privacy = [
        f
        for f in veto
        if f.get("type") == "weak_test"
        and "test_privacy_no_plaintext_email_anywhere" in (f.get("term") or f.get("evidence") or "")
    ]
    assert privacy, f"expected a P1 privacy weak_test, got {veto}"
    finding = privacy[0]
    assert finding["priority"] == "P1"
    assert finding["file"] and finding["file"].endswith("test_fulfillment.py")
    assert finding["line"] and int(finding["line"]) >= 1
    assert "swallowed" in (finding.get("evidence") or "").lower()


def test_uncovered_claims_alone_do_not_veto(tmp_path: Path) -> None:
    root = tmp_path / "claims_only"
    root.mkdir()
    write_code(root, {"app.py": "def add(a, b):\n    return a + b\n"})
    write_docs(
        root,
        "# App\n\n"
        "`add` returns the sum of two integers.\n\n"
        "The product also promises a refund path that is not implemented in tests.\n",
    )
    _write_test(
        root,
        "test_app.py",
        "from app import add\n"
        "\n"
        "\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
    )
    report = Vesper(_config(tmp_path)).run(root, root / "docs")
    doc = build_document(report.to_dict(), fail_on="P1")
    claims = [f for f in doc["findings"] if f.get("type") == "claim"]
    veto = [f for f in doc["findings"] if f.get("verdict_effect") == "VETO"]
    if claims:
        assert all(f["priority"] == "P2" for f in claims)
        assert all(f["verdict_effect"] == "WARN" for f in claims)
    if not veto:
        assert report.verdict in {Verdict.CLEAR.value, Verdict.BLOCKED_ENV.value}
    else:
        assert report.verdict == Verdict.VETO.value
    check_verdict_consistent(report.verdict, doc["findings"])


def test_written_findings_match_run_verdict(tmp_path: Path) -> None:
    project = _privacy_swallowed_project(tmp_path / "privacy")
    report = Vesper(_config(tmp_path)).run(project, project / "docs")
    latest = tmp_path / "findings" / "latest" / "findings.json"
    assert latest.is_file()
    doc = json.loads(latest.read_text(encoding="utf-8"))
    assert doc["verdict"] == report.verdict == Verdict.VETO.value
    check_verdict_consistent(doc["verdict"], doc["findings"])


def test_skipped_test_in_sandbox_file_stays_p2(tmp_path: Path) -> None:
    """A skipped integration test must not become P1 just because the path
    contains a word like 'sandbox'."""
    from agents.riley import detect_weak_test_hits

    tests = tmp_path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_sandbox.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='needs bwrap')\n"
        "def test_bwrap_integration_blocks_home_network_and_outside_reads():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    hits = detect_weak_test_hits(tmp_path)
    node = "tests/unit/test_sandbox.py::test_bwrap_integration_blocks_home_network_and_outside_reads"
    assert node in hits
    assert hits[node].reason == "skipped test"
    assert hits[node].priority == "P2"


def test_skipped_test_stays_p2_even_when_a_sandbox_claim_points_at_it(tmp_path: Path) -> None:
    from agents.riley import detect_weak_test_hits, review_results, upgrade_hits_for_plan
    from core.packets import Scenario, TestPlan, Verdict

    tests = tmp_path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_sandbox.py").write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='needs bwrap')\n"
        "def test_bwrap_integration_blocks_home_network_and_outside_reads():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    node = "tests/unit/test_sandbox.py::test_bwrap_integration_blocks_home_network_and_outside_reads"
    hits = detect_weak_test_hits(tmp_path)
    plan = TestPlan(scenarios=[
        Scenario(
            id="C19",
            title="Filesystem boundaries inside the sandbox are mounted read-only.",
            steps=["verify"],
            acceptance=["holds"],
            covered_by=[node],
        ),
        Scenario(id="S1", title="negative", steps=["run"], acceptance=["fail"], negative=True),
    ])
    upgrade_hits_for_plan(hits, plan)
    assert hits[node].priority == "P2"
    review = review_results(
        plan,
        {"collected": 1, "passed": 1, "failed": 0, "errors": 0, "exit_code": 0, "stdout": "1 passed", "stderr": ""},
        config={"llm": {"provider": "none"}, "dry_run": True},
        project_root=tmp_path,
    )
    assert review.verdict != Verdict.VETO.value


def _hits_for(tmp_path: Path, name: str, source: str):
    from agents.riley import detect_weak_test_hits

    tests = tmp_path / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / name).write_text(source, encoding="utf-8")
    return detect_weak_test_hits(tmp_path)


def test_unicode_decode_error_around_assert_is_not_p1(tmp_path: Path) -> None:
    """KeyHive pattern: a narrow except does not swallow AssertionError."""
    hits = _hits_for(
        tmp_path,
        "test_fulfillment.py",
        "def test_privacy_no_plaintext_email_anywhere():\n"
        "    for p in []:\n"
        "        try:\n"
        "            assert b'secret@buyer.test' not in p.read_bytes()\n"
        "        except UnicodeDecodeError:\n"
        "            pass\n",
    )
    node = "tests/test_fulfillment.py::test_privacy_no_plaintext_email_anywhere"
    assert node not in hits or hits[node].priority != "P1"


def test_moneyed_call_without_assert_is_not_p1(tmp_path: Path) -> None:
    """A test that calls production code can fail by raising, even with no assert."""
    hits = _hits_for(
        tmp_path,
        "test_moneyed_classes.py",
        "class ExtendedMoney:\n"
        "    def __mul__(self, other):\n"
        "        return self\n"
        "    def do_my_behaviour(self):\n"
        "        pass\n"
        "\n"
        "def test_can_call_subclass_method_after_arithmetic_operations():\n"
        "    extended_money = ExtendedMoney()\n"
        "    extended_money.do_my_behaviour()\n"
        "    (1 * extended_money).do_my_behaviour()\n",
    )
    node = "tests/test_moneyed_classes.py::test_can_call_subclass_method_after_arithmetic_operations"
    assert node in hits
    assert hits[node].reason == "no assertions"
    assert hits[node].priority == "P2"


def test_pass_only_body_is_p1(tmp_path: Path) -> None:
    hits = _hits_for(
        tmp_path,
        "test_empty.py",
        "def test_convert_to_default():\n"
        "    # conversions are not implemented yet\n"
        "    pass\n",
    )
    node = "tests/test_empty.py::test_convert_to_default"
    assert hits[node].reason == "no assertions"
    assert hits[node].priority == "P1"


def test_except_exception_around_assert_is_p1(tmp_path: Path) -> None:
    hits = _hits_for(
        tmp_path,
        "test_swallowed.py",
        "def stored_payload():\n"
        "    return 'contact user@example.com'\n"
        "\n"
        "def test_privacy_swallowed():\n"
        "    try:\n"
        "        assert 'user@example.com' not in stored_payload()\n"
        "    except Exception:\n"
        "        pass\n",
    )
    node = "tests/test_swallowed.py::test_privacy_swallowed"
    assert hits[node].priority == "P1"
    assert "swallowed" in hits[node].reason


def test_except_assertion_error_around_assert_is_p1(tmp_path: Path) -> None:
    hits = _hits_for(
        tmp_path,
        "test_swallowed_ae.py",
        "def test_swallowed_assertion_error():\n"
        "    try:\n"
        "        assert False\n"
        "    except AssertionError:\n"
        "        pass\n",
    )
    node = "tests/test_swallowed_ae.py::test_swallowed_assertion_error"
    assert hits[node].priority == "P1"
    assert "swallowed" in hits[node].reason

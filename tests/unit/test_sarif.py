from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import main as main_module
from core import sarif


# --- helpers ---------------------------------------------------------------
def _minimal_report(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "run-x",
        "verdict": "CLEAR",
        "escalation": "",
        "project_root": "/abs/project",
        "docs_root": "/abs/docs",
        "isolated_root": "/tmp/qa-agent-ecosystem-isolations/run-x/isolated_copy",
        "drift": {},
        "plan": {},
        "policy": {},
        "execution": {},
        "post_audit": {},
        "mutation": {},
    }
    base.update(overrides)
    return base


def _results_by_rule(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in document["runs"][0]["results"]:
        grouped.setdefault(result["ruleId"], []).append(result)
    return grouped


# --- structural invariants -------------------------------------------------
def test_sarif_top_level_shape_and_version() -> None:
    doc = sarif.build_sarif(_minimal_report())
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    assert doc["version"] == "2.1.0"
    assert isinstance(doc["runs"], list) and len(doc["runs"]) == 1
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "QA-Ecosystem"
    assert run["tool"]["driver"]["version"]  # read dynamically from core.__version__
    assert isinstance(run["results"], list)
    assert run["results"] == []


def test_sarif_rule_catalog_is_stable_and_unique() -> None:
    doc = sarif.build_sarif(_minimal_report())
    rules = doc["runs"][0]["tool"]["driver"]["rules"]
    ids = [r["id"] for r in rules]
    assert ids == [entry["id"] for entry in sarif.RULE_CATALOG]
    assert len(ids) == len(set(ids))
    # ruleIndex in every emitted result matches the catalog position.
    for entry in rules:
        assert sarif._RULE_INDEX[entry["id"]] == ids.index(entry["id"])


def test_sarif_build_accepts_object_with_to_dict() -> None:
    class _Wrapper:
        def to_dict(self) -> dict[str, Any]:
            return _minimal_report(escalation="ESCALATE: DOMAIN_EXPERT")

    doc = sarif.build_sarif(_Wrapper())
    results = doc["runs"][0]["results"]
    assert any(r["ruleId"] == "VESPER001" for r in results)


def test_sarif_write_creates_parent_and_valid_json(tmp_path: Path) -> None:
    dest = tmp_path / "nested" / "out.sarif"
    written = sarif.write_sarif(_minimal_report(), dest)
    assert written == dest
    assert dest.is_file()
    loaded = json.loads(dest.read_text(encoding="utf-8"))
    assert loaded["version"] == "2.1.0"


# --- Lexi mapping ----------------------------------------------------------
def test_sarif_lexi_maps_kinds_and_preserves_severity() -> None:
    report = _minimal_report(
        drift={
            "verdict": "VETO",
            "findings": [
                {"kind": "missing_code", "path": "ghost",
                 "message": "not in code", "severity": "high"},
                {"kind": "missing_tests", "path": "tests",
                 "message": "no tests", "severity": "medium"},
                {"kind": "domain_term", "path": "curl",
                 "message": "only in prose", "severity": "low"},
                {"kind": "weird_kind", "path": "whatever",
                 "message": "unclassified", "severity": "medium"},
            ],
            "docs_files": ["README.md"],
            "code_files": ["calculator.py"],
            "test_files": [],
        }
    )
    doc = sarif.build_sarif(report)
    by_rule = _results_by_rule(doc)
    assert by_rule["LEXI001"][0]["level"] == "error"
    assert by_rule["LEXI002"][0]["level"] == "warning"
    assert by_rule["LEXI004"][0]["level"] == "note"
    assert "LEXI999" in by_rule


def test_sarif_lexi_location_only_for_known_relative_file() -> None:
    report = _minimal_report(
        drift={
            "verdict": "VETO",
            "findings": [
                {"kind": "missing_code", "path": "calculator.py",
                 "message": "unknown symbol", "severity": "medium"},
                {"kind": "missing_code", "path": "notlisted.py",
                 "message": "another", "severity": "medium"},
                {"kind": "missing_code", "path": "/abs/project/leak.py",
                 "message": "absolute", "severity": "medium"},
                {"kind": "missing_code",
                 "path": "/tmp/qa-agent-ecosystem-isolations/run-x/isolated_copy/x.py",
                 "message": "isolated", "severity": "medium"},
            ],
            "docs_files": ["README.md"],
            "code_files": ["calculator.py"],
            "test_files": [],
        }
    )
    doc = sarif.build_sarif(report)
    results = doc["runs"][0]["results"]
    located = [r for r in results if "locations" in r]
    assert len(located) == 1
    uri = located[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
    assert uri == "calculator.py"
    # No absolute paths or isolated-copy markers anywhere in the document.
    dumped = json.dumps(doc)
    assert "/abs/" not in dumped
    assert "isolated_copy" not in dumped
    assert "/tmp/" not in dumped


# --- Sable mapping ---------------------------------------------------------
def test_sarif_sable_emits_one_error_per_failed_check() -> None:
    report = _minimal_report(
        policy={
            "preflight": {
                "verdict": "BLOCK",
                "blocked_reasons": ["command not pytest-only"],
                "checks": {
                    "pytest_only": False,
                    "no_secret_env": True,
                    "network_denied": False,
                },
                "command": ["pytest"],
                "allowed": [],
            },
            "post": {
                "verdict": "CLEAR",
                "blocked_reasons": [],
                "checks": {"collected_gt_zero": True},
                "command": [],
                "allowed": [],
            },
        }
    )
    doc = sarif.build_sarif(report)
    by_rule = _results_by_rule(doc)
    assert set(by_rule) == {"SABLE001", "SABLE008"}
    assert all(r["level"] == "error" for r in doc["runs"][0]["results"])


def test_sarif_sable_handles_early_block_shape() -> None:
    report = _minimal_report(
        policy={
            "verdict": "BLOCK",
            "blocked_reasons": ["unsafe operations in tests: tests/foo.py: import subprocess"],
            "checks": {"tests_ast_clean": False},
            "command": ["pytest"],
            "allowed": [],
        }
    )
    doc = sarif.build_sarif(report)
    results = doc["runs"][0]["results"]
    assert len(results) == 1
    entry = results[0]
    assert entry["ruleId"] == "SABLE007"
    loc = entry["locations"][0]["physicalLocation"]["artifactLocation"]
    assert loc["uri"] == "tests/foo.py"
    assert loc["uriBaseId"] == "%SRCROOT%"


# --- Riley mapping ---------------------------------------------------------
def test_sarif_riley_veto_level_and_weak_test_location() -> None:
    report = _minimal_report(
        post_audit={
            "riley": {
                "verdict": "VETO",
                "gaps": [
                    "weak test: tests/foo.py::test_bar (skipped test)",
                    "claim not covered by tests: C1",
                    "some other concern",
                ],
                "required_changes": [],
                "root_cause": "",
                "notes": "",
                "llm_status": "none",
            },
            "lexi": None,
            "root_cause": "",
        }
    )
    doc = sarif.build_sarif(report)
    by_rule = _results_by_rule(doc)
    assert all(r["level"] == "error" for r in doc["runs"][0]["results"])
    assert by_rule["RILEY001"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "tests/foo.py"
    assert "locations" not in by_rule["RILEY002"][0]
    assert "RILEY999" in by_rule


def test_sarif_riley_non_veto_level_is_warning() -> None:
    report = _minimal_report(
        post_audit={
            "riley": {
                "verdict": "CLEAR",
                "gaps": ["weak test: tests/foo.py::test_bar (skipped test)"],
                "required_changes": [],
                "root_cause": "",
                "notes": "",
                "llm_status": "none",
            },
        }
    )
    doc = sarif.build_sarif(report)
    assert doc["runs"][0]["results"][0]["level"] == "warning"


# --- Nova mapping ----------------------------------------------------------
def test_sarif_nova_emits_one_result_per_failed_line() -> None:
    stdout = (
        "=== session start ===\n"
        "FAILED tests/a.py::test_one - assert 1 == 2\n"
        "FAILED tests/b.py::test_two\n"
        "=== short summary ===\n"
    )
    report = _minimal_report(
        execution={
            "collected": 3,
            "passed": 1,
            "failed": 2,
            "exit_code": 1,
            "stdout": stdout,
            "stderr": "",
            "install_status": "ok",
        }
    )
    doc = sarif.build_sarif(report)
    nova = [r for r in doc["runs"][0]["results"] if r["ruleId"] == "NOVA001"]
    assert len(nova) == 2
    uris = {
        r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for r in nova
        if "locations" in r
    }
    assert uris == {"tests/a.py", "tests/b.py"}


def test_sarif_nova_falls_back_when_no_failed_lines() -> None:
    report = _minimal_report(
        execution={
            "collected": 0,
            "passed": 0,
            "failed": 0,
            "exit_code": 2,
            "stdout": "INTERNALERROR> crash\n",
            "stderr": "",
            "install_status": "ok",
        }
    )
    doc = sarif.build_sarif(report)
    results = doc["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["ruleId"] == "NOVA001"
    assert "locations" not in results[0]
    assert "exit=2" in results[0]["message"]["text"]


def test_sarif_nova_install_failure_becomes_nova002() -> None:
    report = _minimal_report(
        execution={
            "collected": 0,
            "passed": 0,
            "failed": 0,
            "exit_code": 4,
            "stdout": "",
            "stderr": "",
            "install_status": "pip install failed (exit 1)",
        }
    )
    doc = sarif.build_sarif(report)
    results = doc["runs"][0]["results"]
    assert [r["ruleId"] for r in results] == ["NOVA002"]
    assert results[0]["level"] == "error"


def test_sarif_nova_silent_when_run_passed() -> None:
    report = _minimal_report(
        execution={
            "collected": 5,
            "passed": 5,
            "failed": 0,
            "exit_code": 0,
            "stdout": "5 passed\n",
            "stderr": "",
            "install_status": "ok",
        }
    )
    doc = sarif.build_sarif(report)
    assert doc["runs"][0]["results"] == []


# --- Vesper mapping --------------------------------------------------------
def test_sarif_vesper_escalation_becomes_vesper001() -> None:
    report = _minimal_report(escalation="ESCALATE: DOMAIN_EXPERT")
    doc = sarif.build_sarif(report)
    results = doc["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["ruleId"] == "VESPER001"
    assert results[0]["level"] == "error"


# --- main.py --sarif wiring (no subprocess, monkeypatched Vesper) ---------
class _FakeReport:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        return self._data

    @property
    def verdict(self) -> str:
        return self._data["verdict"]

    @property
    def escalation(self) -> str:
        return self._data.get("escalation") or ""

    @property
    def sandbox(self) -> str:
        return self._data.get("sandbox", "off")

    @property
    def mutation(self) -> dict[str, Any] | None:
        return self._data.get("mutation")

    @property
    def run_id(self) -> str:
        return self._data.get("run_id", "test-run")

    @property
    def isolated_root(self) -> str:
        return self._data.get("isolated_root", "")


class _FakeVesper:
    def __init__(self, config: dict) -> None:  # noqa: ARG002
        return None

    def run(self, project: Path, docs: Path) -> _FakeReport:  # noqa: ARG002
        return _FakeReport(_minimal_report(escalation="ESCALATE: DOMAIN_EXPERT"))


def test_main_sarif_flag_writes_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "Vesper", _FakeVesper)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("dry_run: true\n", encoding="utf-8")
    docs = tmp_path / "docs.md"
    docs.write_text("# Docs\n", encoding="utf-8")
    sarif_path = tmp_path / "out" / "run.sarif"

    code = main_module.main(
        [
            "--project", str(tmp_path),
            "--docs", str(docs),
            "--config", str(cfg),
            "--reports-dir", str(tmp_path / "reports"),
            "--sarif", str(sarif_path),
        ]
    )

    assert code == 0
    assert sarif_path.is_file()
    doc = json.loads(sarif_path.read_text(encoding="utf-8"))
    assert doc["version"] == "2.1.0"
    rule_ids = [r["id"] for r in doc["runs"][0]["tool"]["driver"]["rules"]]
    assert "VESPER001" in rule_ids
    assert any(r["ruleId"] == "VESPER001" for r in doc["runs"][0]["results"])


def test_main_without_sarif_flag_writes_no_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "Vesper", _FakeVesper)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("dry_run: true\n", encoding="utf-8")
    docs = tmp_path / "docs.md"
    docs.write_text("# Docs\n", encoding="utf-8")
    sarif_path = tmp_path / "unused.sarif"

    code = main_module.main(
        [
            "--project", str(tmp_path),
            "--docs", str(docs),
            "--config", str(cfg),
            "--reports-dir", str(tmp_path / "reports"),
        ]
    )

    assert code == 0
    assert not sarif_path.exists()


# --- path-guard unit tests -------------------------------------------------
def test_safe_rel_path_rejects_unsafe_values() -> None:
    for bad in [
        "", "   ", "/abs/path", "\\\\server\\share",
        "C:\\x", "a/../../b", "../escape",
        "/tmp/x", "tmp/qa-agent-ecosystem-isolations/x",
        "/private/var/folders/abc/isolated_copy/x.py",
        "path\0with\0null",
        None, 3, ["x"],
    ]:
        assert sarif._safe_rel_path(bad) is None, bad


def test_safe_rel_path_accepts_clean_relative_paths() -> None:
    assert sarif._safe_rel_path("a/b/c.py") == "a/b/c.py"
    assert sarif._safe_rel_path("tests\\foo.py") == "tests/foo.py"


# sanity check on the module contract: no imports from agents/
def test_core_sarif_does_not_import_agents() -> None:
    import inspect

    source = inspect.getsource(sarif)
    assert "from agents" not in source
    assert "import agents" not in source


def test_sarif_early_veto_without_results_emits_vesper002() -> None:
    report = _minimal_report(
        verdict="VETO",
        role_results=[
            {"role": "vesper", "verdict": "VETO", "summary": "sandbox required but bwrap unavailable: x"}
        ],
    )
    doc = sarif.build_sarif(report)
    results = doc["runs"][0]["results"]
    assert len(results) == 1
    assert results[0]["ruleId"] == "VESPER002"
    assert results[0]["level"] == "error"
    assert results[0]["ruleIndex"] == len(sarif.RULE_CATALOG) - 1
    assert "run vetoed before execution" in results[0]["message"]["text"]
    assert "bwrap unavailable" in results[0]["message"]["text"]


def test_sarif_vesper002_not_added_when_other_results_exist() -> None:
    report = _minimal_report(verdict="ESCALATE", escalation="ESCALATE: DOMAIN_EXPERT")
    rules = _results_by_rule(sarif.build_sarif(report))
    assert "VESPER001" in rules
    assert "VESPER002" not in rules


def test_sarif_existing_rule_indexes_are_unchanged() -> None:
    ids = [entry["id"] for entry in sarif.RULE_CATALOG]
    assert ids.index("VESPER001") == 20
    assert ids[-1] == "VESPER002"

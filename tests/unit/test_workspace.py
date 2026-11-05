"""Unit tests for v0.7.0: workspaces, diff-scoped mutation, changed-line coverage, and weakened assertions."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from core import diffscope, findings, mutation, workspace
from core.orchestrator import Vesper
from core.packets import Verdict
from tests.support import mini_project


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@e", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


def test_discover_workspaces_npm_pnpm_and_python_containers(tmp_path: Path) -> None:
    root = tmp_path / "monorepo"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"name": "root", "workspaces": {"packages": ["custom/*"]}}),
        encoding="utf-8",
    )
    (root / "pnpm-workspace.yaml").write_text(
        "packages:\n  - 'packages/*'\n",
        encoding="utf-8",
    )

    # 1. TS package in packages/pkg-a
    pkg_a = root / "packages" / "pkg-a"
    pkg_a.mkdir(parents=True)
    (pkg_a / "package.json").write_text(
        json.dumps(
            {
                "name": "@acme/pkg-a",
                "engines": {"node": ">=18"},
                "devDependencies": {"vitest": "^1.0.0"},
            }
        ),
        encoding="utf-8",
    )

    # 2. Python package in services/svc-b
    svc_b = root / "services" / "svc-b"
    svc_b.mkdir(parents=True)
    (svc_b / "pyproject.toml").write_text(
        '[project]\nname = "svc-b-pkg"\nrequires-python = ">=3.11"\n',
        encoding="utf-8",
    )

    # 3. Custom workspace glob in custom/mod-c with setup.py
    mod_c = root / "custom" / "mod-c"
    mod_c.mkdir(parents=True)
    (mod_c / "setup.py").write_text("from setuptools import setup\nsetup(name='mod-c')\n", encoding="utf-8")

    # 4. Skipped dir inside packages/tests should be ignored
    (root / "packages" / "tests").mkdir(parents=True)
    assert workspace._inspect_package_dir(root, root / "packages" / "tests") is None
    assert workspace._inspect_package_dir(root, root / "packages") is None

    ws = workspace.discover_workspaces(root)
    by_path = {w["path"]: w for w in ws}
    assert set(by_path) == {"custom/mod-c", "packages/pkg-a", "services/svc-b"}
    assert by_path["packages/pkg-a"]["name"] == "@acme/pkg-a"
    assert by_path["packages/pkg-a"]["language"] == "ts"
    assert by_path["packages/pkg-a"]["runner"] == "vitest"
    assert by_path["packages/pkg-a"]["engines"] == {"node": ">=18"}
    assert by_path["services/svc-b"]["name"] == "svc-b-pkg"
    assert by_path["services/svc-b"]["language"] == "python"
    assert by_path["services/svc-b"]["runner"] == "pytest"
    assert by_path["services/svc-b"]["engines"] == {"python": ">=3.11"}
    assert by_path["custom/mod-c"]["name"] == "mod-c"


def test_workspace_engine_checks_and_finding_attribution(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    pkg_ok = root / "packages" / "ok-ts"
    pkg_ok.mkdir(parents=True)
    (pkg_ok / "package.json").write_text(json.dumps({"name": "ok-ts"}), encoding="utf-8")

    ws = workspace.discover_workspaces(root)
    ok, msg = workspace.check_workspace_engines(root, ws)
    assert ok is True
    assert msg == ""

    # Incompatible node engine on sub-package fails check_workspace_engines
    pkg_bad = root / "packages" / "bad-ts"
    pkg_bad.mkdir(parents=True)
    (pkg_bad / "package.json").write_text(
        json.dumps({"name": "bad-ts", "engines": {"node": ">=999.0.0"}}),
        encoding="utf-8",
    )
    ws2 = workspace.discover_workspaces(root)
    ok2, msg2 = workspace.check_workspace_engines(root, ws2)
    assert ok2 is False
    assert "workspace package 'bad-ts' (packages/bad-ts)" in msg2

    # Attribution & verdicts
    raw_findings = [
        {"id": "F-001", "file": "packages/bad-ts/src/index.ts", "verdict_effect": "VETO"},
        {"id": "F-002", "file": "packages/ok-ts", "verdict_effect": "WARN"},
        "not-a-dict",
    ]
    attributed = workspace.assign_finding_packages(raw_findings, ws2)  # type: ignore[arg-type]
    assert attributed[0]["package"] == "bad-ts"
    assert attributed[1]["package"] == "ok-ts"

    verdicts = workspace.compute_workspace_verdicts(attributed, ws2, "VETO")
    assert verdicts == {"bad-ts": "VETO", "ok-ts": "CLEAR"}
    blocked_verdicts = workspace.compute_workspace_verdicts([], ws2, "BLOCKED_ENV")
    assert blocked_verdicts == {"bad-ts": "BLOCKED_ENV", "ok-ts": "BLOCKED_ENV"}


def test_diff_changed_lines_uncovered_files_and_weakened_tests(tmp_path: Path) -> None:
    root = mini_project(tmp_path / "proj")
    (root / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(0, 0) == 0\n\n"
        "class TestSuite:\n"
        "    def test_method(self):\n"
        "        assert add(1, 1) == 2\n",
        encoding="utf-8",
    )
    (root / "extra.py").write_text("def f(x):\n    return x + 1\n", encoding="utf-8")
    (root / "tests" / "test_extra.spec.ts").write_text(
        "it('checks math', () => {\n  expect(1).toBe(1);\n  expect(2).toBe(2);\n});\n",
        encoding="utf-8",
    )
    _init_repo(root)

    # Modify calculator.py (covered by test_calculator.py) and extra.py (no covering py test)
    (root / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b + 0\n",
        encoding="utf-8",
    )
    (root / "extra.py").write_text("def f(x):\n    return x + 2\n", encoding="utf-8")
    # Weaken assertions in test_calculator.py (from 2 asserts in test_add down to 1)
    # and in test_extra.spec.ts (from 2 expect calls down to 1)
    (root / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n\n"
        "class TestSuite:\n"
        "    def test_method(self):\n"
        "        assert add(1, 1) == 2\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_extra.spec.ts").write_text(
        "it('checks math', () => {\n  expect(1).toBe(1);\n});\n",
        encoding="utf-8",
    )

    scope = diffscope.compute_diff_scope(root, "HEAD", root / "docs")
    assert "calculator.py" in scope.changed_lines
    assert 2 in scope.changed_lines["calculator.py"]
    assert "extra.py" in scope.uncovered_changed_files
    assert "calculator.py" not in scope.uncovered_changed_files
    scope_dict = scope.to_dict()
    assert "changed_lines" in scope_dict
    assert "uncovered_changed_files" in scope_dict

    weakened = diffscope.detect_weakened_tests(root, "HEAD")
    assert len(weakened) == 2
    assert any("tests/test_calculator.py::test_add (assertions reduced from 2 to 1)" in w for w in weakened)
    assert any("tests/test_extra.spec.ts::checks math (assertions reduced from 2 to 1)" in w for w in weakened)
    assert diffscope._count_py_assertions_by_test("def broken(:") == {}
    assert diffscope.detect_weakened_tests(tmp_path / "non_git_dir", "HEAD") == []


def test_diff_scoped_mutation_and_findings_projection(tmp_path: Path) -> None:
    proj = tmp_path / "mut_proj"
    proj.mkdir()
    (proj / "calc.py").write_text(
        "def unchanged(a, b):\n"
        "    return a + b\n"
        "def changed_fn(x):\n"
        "    return x > 10\n",
        encoding="utf-8",
    )
    # Restrict mutants to line 4 only
    muts = mutation.find_all_mutants(proj, allowed_lines={"calc.py": [4]})
    assert muts
    assert all(m.lineno == 4 for m in muts)
    assert mutation.find_all_mutants(proj, allowed_lines={"other.py": [1]}) == []

    # Build findings with weakened_tests, diff_scoped surviving mutants, and workspaces
    rep = {
        "run_id": "r-070",
        "verdict": "VETO",
        "finished_at": "2026-11-05T12:00:00Z",
        "post_audit": {
            "weakened_tests": ["packages/pkg-a/tests/test_a.py::test_one (assertions reduced from 3 to 1)"],
            "workspaces": [{"name": "pkg-a", "path": "packages/pkg-a", "language": "python", "runner": "pytest", "engines": {}}],
        },
        "mutation": {
            "status": "completed",
            "diff_scoped": True,
            "survived_mutants": [
                {
                    "id": "m-001",
                    "location": "packages/pkg-a/calc.py:4",
                    "type": "comparison",
                    "original": ">",
                    "mutated": ">=",
                }
            ],
        },
    }
    doc = findings.build_document(rep, fail_on="P2")
    assert findings.validate_schema(doc, findings.load_schema()) == []
    assert doc["workspace_verdicts"] == {"pkg-a": "VETO"}
    by_type = {f["type"]: f for f in doc["findings"]}
    assert "weak_test" in by_type
    assert by_type["weak_test"]["package"] == "pkg-a"
    assert "mutation" in by_type
    assert by_type["mutation"]["package"] == "pkg-a"
    assert by_type["mutation"]["line"] == 4
    assert "No test guards this change" in by_type["mutation"]["customer_impact"]


def test_orchestrator_weakened_tests_veto_and_diff_mutation(tmp_path: Path) -> None:
    root = mini_project(
        tmp_path / "proj",
        with_divide=False,
        docs="# Calc\n\n`add` adds two integers.\n",
    )
    (root / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(0, 0) == 0\n"
        "def test_negative():\n"
        "    assert add(-1, 1) == 0\n",
        encoding="utf-8",
    )
    _init_repo(root)

    # Weaken test_calculator.py vs HEAD (from 2 asserts in test_add to 1 assert) -> Vesper in --diff mode must VETO
    (root / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "def test_negative():\n"
        "    assert add(-1, 1) == 0\n",
        encoding="utf-8",
    )
    cfg = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "reports"),
        "isolation_method": "copy",
        "diff": {"base_ref": "HEAD"},
    }
    rep_weak = Vesper(cfg).run(root, root / "docs")
    assert rep_weak.verdict == Verdict.VETO.value
    assert rep_weak.post_audit.get("weakened_tests")
    assert rep_weak.mutation == {"status": "skipped", "reason": "weakened tests in diff mode"}

    # Restore test assertions and modify calculator.py with diff + mutation enabled
    (root / "tests" / "test_calculator.py").write_text(
        "from calculator import add\n\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(0, 0) == 0\n"
        "def test_negative():\n"
        "    assert add(-1, 1) == 0\n",
        encoding="utf-8",
    )
    (root / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n"
        "    return a + b + 0\n",
        encoding="utf-8",
    )
    cfg_mut = {
        "dry_run": True,
        "allow_network": False,
        "reports_dir": str(tmp_path / "reports_mut"),
        "isolation_method": "copy",
        "diff": {"base_ref": "HEAD"},
        "mutation": {"enabled": True},
    }
    with patch(
        "agents.riley.audit_mutations",
        return_value={"status": "completed", "verdict": "CLEAR", "score": 1.0},
    ) as mock_audit:
        rep_mut = Vesper(cfg_mut).run(root, root / "docs")
    assert rep_mut.verdict == Verdict.CLEAR.value
    assert rep_mut.mutation.get("diff_scoped") is True
    assert mock_audit.called


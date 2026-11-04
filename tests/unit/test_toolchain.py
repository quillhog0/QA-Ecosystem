from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.toolchain import (
    _compare_semver,
    _parse_semver,
    _satisfies_single_constraint,
    check_node_engine,
    detect_languages,
    detect_package_manager,
    detect_ts_runner,
    get_available_node_version,
    semver_satisfies,
    typecheck_project,
)


def test_detect_languages_python(tmp_path: Path):
    assert detect_languages(Path("/nonexistent/path")) == []
    assert detect_languages(tmp_path) == []

    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    assert detect_languages(tmp_path) == ["python"]

    (tmp_path / "pyproject.toml").unlink()
    (tmp_path / "requirements-custom.txt").write_text("pytest\n", encoding="utf-8")
    assert detect_languages(tmp_path) == ["python"]


def test_detect_languages_ts(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    assert detect_languages(tmp_path) == ["ts"]


def test_detect_languages_mixed(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
    assert set(detect_languages(tmp_path)) == {"python", "ts"}


def test_detect_package_manager(tmp_path: Path):
    # npm ci
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    pm, cmd, warn = detect_package_manager(tmp_path)
    assert pm == "npm"
    assert cmd == ["npm", "ci"]
    assert warn is None

    # pnpm
    (tmp_path / "package-lock.json").unlink()
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 5.4", encoding="utf-8")
    pm, cmd, warn = detect_package_manager(tmp_path)
    assert pm == "pnpm"
    assert cmd == ["pnpm", "install", "--frozen-lockfile"]
    assert warn is None

    # yarn
    (tmp_path / "pnpm-lock.yaml").unlink()
    (tmp_path / "yarn.lock").write_text("# yarn lockfile", encoding="utf-8")
    pm, cmd, warn = detect_package_manager(tmp_path)
    assert pm == "yarn"
    assert cmd == ["yarn", "install", "--frozen-lockfile"]
    assert warn is None

    # fallback
    (tmp_path / "yarn.lock").unlink()
    pm, cmd, warn = detect_package_manager(tmp_path)
    assert pm == "npm"
    assert cmd == ["npm", "install"]
    assert warn is not None


def test_semver_parse_and_compare():
    assert _parse_semver("v1.2.3") == (1, 2, 3)
    assert _parse_semver("1.2") == (1, 2, 0)
    assert _parse_semver("1") == (1, 0, 0)
    assert _parse_semver("invalid") == (0, 0, 0)

    assert _compare_semver((1, 0, 0), (2, 0, 0)) == -1
    assert _compare_semver((2, 0, 0), (1, 0, 0)) == 1
    assert _compare_semver((1, 2, 3), (1, 2, 3)) == 0


def test_satisfies_single_constraint():
    assert _satisfies_single_constraint((20, 0, 0), "*") is True
    assert _satisfies_single_constraint((20, 0, 0), "x") is True
    assert _satisfies_single_constraint((20, 5, 0), "20.x") is True
    assert _satisfies_single_constraint((20, 5, 1), "20.5.x") is True
    assert _satisfies_single_constraint((21, 5, 0), "20.x") is False

    # Carets
    assert _satisfies_single_constraint((1, 5, 0), "^1.2.0") is True
    assert _satisfies_single_constraint((2, 0, 0), "^1.2.0") is False
    assert _satisfies_single_constraint((0, 2, 5), "^0.2.0") is True
    assert _satisfies_single_constraint((0, 3, 0), "^0.2.0") is False
    assert _satisfies_single_constraint((0, 0, 3), "^0.0.3") is True
    assert _satisfies_single_constraint((0, 0, 4), "^0.0.3") is False

    # Tildes
    assert _satisfies_single_constraint((1, 2, 5), "~1.2.0") is True
    assert _satisfies_single_constraint((1, 3, 0), "~1.2.0") is False

    # Comparison ops
    assert _satisfies_single_constraint((18, 0, 0), ">=18.0.0") is True
    assert _satisfies_single_constraint((17, 0, 0), ">=18.0.0") is False
    assert _satisfies_single_constraint((18, 0, 0), "<=18.0.0") is True
    assert _satisfies_single_constraint((19, 0, 0), "<=18.0.0") is False
    assert _satisfies_single_constraint((19, 0, 0), ">18.0.0") is True
    assert _satisfies_single_constraint((18, 0, 0), ">18.0.0") is False
    assert _satisfies_single_constraint((17, 0, 0), "<18.0.0") is True
    assert _satisfies_single_constraint((18, 0, 0), "<18.0.0") is False
    assert _satisfies_single_constraint((18, 0, 0), "=18.0.0") is True
    assert _satisfies_single_constraint((18, 0, 0), "==18.0.0") is True
    assert _satisfies_single_constraint((18, 0, 1), "18.0.0") is False


def test_semver_satisfies():
    assert semver_satisfies("v20.18.0", ">=18.0.0")
    assert semver_satisfies("v20.18.0", ">=18.0.0 <22.0.0")
    assert not semver_satisfies("v16.14.0", ">=18.0.0")
    assert semver_satisfies("20.18.0", "^20.0.0")
    assert not semver_satisfies("21.0.0", "~20.1.0")
    assert semver_satisfies("20.5.1", "20.x")
    assert semver_satisfies("20.18.0", ">=18")
    assert semver_satisfies("v16.0.0", "^16 || ^18 || ^20")
    assert semver_satisfies("v20.0.0", "^16 || ^18 || ^20")
    assert not semver_satisfies("v14.0.0", "^16 || ^18 || ^20")


def test_get_available_node_version():
    with patch("shutil.which", return_value=None):
        assert get_available_node_version() is None

    with patch("shutil.which", return_value="/bin/node"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="v20.18.0\n")
            assert get_available_node_version() == "v20.18.0"


def test_check_node_engine(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"engines": {"node": ">=22.0.0"}}), encoding="utf-8"
    )
    ok, err = check_node_engine(tmp_path, current_node_version="v20.18.0")
    assert not ok
    assert "Node.js engine mismatch" in err
    assert ">=22.0.0" in err

    (tmp_path / "package.json").write_text(
        json.dumps({"engines": {"node": ">=18.0.0"}}), encoding="utf-8"
    )
    ok, err = check_node_engine(tmp_path, current_node_version="v20.18.0")
    assert ok
    assert err is None

    # .nvmrc fallback
    (tmp_path / "package.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / ".nvmrc").write_text("22\n", encoding="utf-8")
    ok, err = check_node_engine(tmp_path, current_node_version="v20.18.0")
    assert not ok
    assert ".nvmrc" in err

    (tmp_path / ".nvmrc").write_text("20\n", encoding="utf-8")
    ok, err = check_node_engine(tmp_path, current_node_version="v20.18.0")
    assert ok


def test_detect_ts_runner(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps({"devDependencies": {"vitest": "^2.0.0"}}), encoding="utf-8"
    )
    info = detect_ts_runner(tmp_path)
    assert info["runner"] == "vitest"
    assert info["direct"] is True
    assert info["has_local_bin"] is False

    local_bin = tmp_path / "node_modules" / ".bin"
    local_bin.mkdir(parents=True)
    (local_bin / "vitest").write_text("#!/bin/sh", encoding="utf-8")
    info_bin = detect_ts_runner(tmp_path)
    assert info_bin["has_local_bin"] is True

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8"
    )
    info = detect_ts_runner(tmp_path)
    assert info["runner"] == "jest"
    assert info["direct"] is True

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "mocha tests/**/*.js"}}), encoding="utf-8"
    )
    info = detect_ts_runner(tmp_path)
    assert info["runner"] == "mocha"
    assert info["direct"] is True

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "node --test node:test"}}), encoding="utf-8"
    )
    info = detect_ts_runner(tmp_path)
    assert info["runner"] == "node_test"
    assert info["direct"] is True

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "custom-runner"}}), encoding="utf-8"
    )
    info = detect_ts_runner(tmp_path)
    assert info["runner"] == "npm_test"
    assert info["direct"] is False


def test_typecheck_project(tmp_path: Path):
    # No tsconfig.json -> no typecheck performed
    errs = typecheck_project(tmp_path)
    assert errs == []

    (tmp_path / "tsconfig.json").write_text("{}", encoding="utf-8")

    # tsc output parsing format 1: file(line,col): error TSxxx: msg
    tsc_output1 = (
        "src/index.ts(10,5): error TS2322: Type 'number' is not assignable to type 'string'.\n"
    )
    with patch("shutil.which", return_value="/bin/node"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=tsc_output1, stderr="")
            errs = typecheck_project(tmp_path)
            assert len(errs) == 1
            assert errs[0]["file"] == "src/index.ts"
            assert errs[0]["line"] == 10
            assert "TS2322" in errs[0]["message"]

    # tsc output parsing format 2: file:line:col - error TSxxx: msg
    tsc_output2 = (
        "src/app.ts:25:3 - error TS1005: ';' expected.\n"
    )
    with patch("shutil.which", return_value="/bin/node"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout=tsc_output2, stderr="")
            errs2 = typecheck_project(tmp_path)
            assert len(errs2) == 1
            assert errs2[0]["file"] == "src/app.ts"
            assert errs2[0]["line"] == 25
            assert "TS1005" in errs2[0]["message"]

    # tsc execution exception
    with patch("shutil.which", return_value="/bin/tsc"), patch("subprocess.run", side_effect=RuntimeError("disk error")):
        errs3 = typecheck_project(tmp_path)
        assert errs3 == []


def test_tilde_and_relational_constraints():
    assert _satisfies_single_constraint((1, 2, 5), "~1.2.0") is True
    assert _satisfies_single_constraint((1, 3, 0), "~1.2.0") is False
    assert _satisfies_single_constraint((2, 0, 0), "~1.2.0") is False
    assert _satisfies_single_constraint((1, 2, 3), "<=1.2.3") is True
    assert _satisfies_single_constraint((1, 2, 4), "<=1.2.3") is False
    assert _satisfies_single_constraint((1, 2, 2), "<1.2.3") is True
    assert _satisfies_single_constraint((1, 2, 3), "<1.2.3") is False
    assert _satisfies_single_constraint((1, 2, 4), ">1.2.3") is True
    assert _satisfies_single_constraint((1, 2, 3), ">1.2.3") is False


def test_semver_satisfies_ranges():
    assert semver_satisfies("18.0.0", "18.x || 20.x") is True
    assert semver_satisfies("20.1.0", "18.x || 20.x") is True
    assert semver_satisfies("19.0.0", "18.x || 20.x") is False
    assert semver_satisfies("20.0.0", ">=18.0.0 <22.0.0") is True
    assert semver_satisfies("22.0.0", ">=18.0.0 <22.0.0") is False


def test_get_available_node_version():
    with patch("shutil.which", return_value=None):
        assert get_available_node_version() is None

    with patch("shutil.which", return_value="/bin/node"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="err")
            assert get_available_node_version() is None

            mock_run.return_value = MagicMock(returncode=0, stdout="v22.1.0\n", stderr="")
            assert get_available_node_version() == "v22.1.0"


def test_check_node_engine_lts_and_mismatches(tmp_path: Path):
    nvmrc = tmp_path / ".nvmrc"
    nvmrc.write_text("lts/*\n", encoding="utf-8")

    # LTS with Node 20, 22, 24 -> True
    assert check_node_engine(tmp_path, current_node_version="v20.0.0")[0] is True
    assert check_node_engine(tmp_path, current_node_version="v22.0.0")[0] is True
    assert check_node_engine(tmp_path, current_node_version="v24.0.0")[0] is True
    # LTS with Node 14 -> False
    ok, err = check_node_engine(tmp_path, current_node_version="v14.0.0")
    assert ok is False
    assert "available node is v14.0.0" in err

    # Node binary unavailable
    with patch("core.toolchain.get_available_node_version", return_value=None):
        ok, err = check_node_engine(tmp_path, current_node_version=None)
        assert ok is False
        assert "node executable is not available" in err

    # Package.json engines range mismatch
    nvmrc.unlink()
    (tmp_path / "package.json").write_text(json.dumps({"engines": {"node": ">=22"}}), encoding="utf-8")
    ok, err = check_node_engine(tmp_path, current_node_version="v20.0.0")
    assert ok is False
    assert "package.json engines.node '>=22'" in err
    assert "available node is v20.0.0" in err


def test_ensure_ts_tooling_cache(tmp_path: Path, monkeypatch):
    from core.toolchain import ensure_ts_tooling

    tooling_dir = tmp_path / "tooling"
    monkeypatch.setenv("QA_TOOLING_DIR", str(tooling_dir))

    # 1. Target already cached
    target_ts = tooling_dir / "typescript.js"
    tooling_dir.mkdir(parents=True)
    target_ts.write_text("a" * 1500, encoding="utf-8")
    res1 = ensure_ts_tooling()
    assert res1 == tooling_dir

    # 2. Fresh copy from vendor
    target_ts.unlink()
    vendor_file = Path(__file__).resolve().parents[2] / "agents" / "vendor" / "typescript.js"
    created_dummy = False
    if not vendor_file.exists():
        vendor_file.parent.mkdir(parents=True, exist_ok=True)
        vendor_file.write_text("a" * 1500, encoding="utf-8")
        created_dummy = True
    try:
        res2 = ensure_ts_tooling()
        assert res2 == tooling_dir
        assert target_ts.is_file()
        assert (tooling_dir / "typescript.sha256").is_file()
    finally:
        if created_dummy:
            vendor_file.unlink(missing_ok=True)

"""Environmental-cause classification (pure, offline, no subprocess)."""

from __future__ import annotations

from pathlib import Path

from core import environment


def _exec(**overrides) -> dict:
    base = {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "exit_code": 2,
        "stdout": "",
        "stderr": "",
        "install_status": "no dependency manifest",
    }
    base.update(overrides)
    return base


def _package(tmp_path: Path, relative: str) -> Path:
    """Create a package directory (with __init__.py) inside a project root."""
    root = tmp_path / "proj"
    package = root.joinpath(*relative.split("."))
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    return root


# --- not environmental ------------------------------------------------------
def test_a_failing_test_outranks_a_missing_dependency():
    execution = _exec(
        collected=2,
        passed=1,
        failed=1,
        errors=1,
        exit_code=1,
        stdout="FAILED tests/test_x.py::test_add - assert 5 == 0",
        stderr="ModuleNotFoundError: No module named 'freezegun'",
    )
    assert environment.classify_execution(execution) is None


def test_a_clean_pass_is_not_environmental():
    execution = _exec(collected=3, passed=3, failed=0, errors=0, exit_code=0)
    assert environment.classify_execution(execution) is None


def test_skipped_tests_alone_are_not_environmental():
    execution = _exec(collected=3, passed=2, skipped=1, errors=0, exit_code=0)
    assert environment.classify_execution(execution) is None


def test_an_unexplained_empty_collection_is_not_environmental():
    execution = _exec(collected=0, errors=0, exit_code=5, stdout="no tests ran in 0.01s")
    assert environment.classify_execution(execution) is None


def test_unexplained_collection_errors_are_not_environmental():
    execution = _exec(
        collected=4,
        errors=1,
        exit_code=2,
        stderr="ERROR tests/test_x.py - ValueError: fixture 'gone' not found",
    )
    assert environment.classify_execution(execution) is None


def test_a_nonzero_exit_without_any_signal_is_not_environmental():
    assert environment.classify_execution(_exec(collected=1, passed=1, exit_code=1)) is None


# --- missing dependency -----------------------------------------------------
def test_missing_module_on_stdout_names_the_dependency():
    execution = _exec(
        collected=1,
        errors=1,
        stdout="ERROR collecting tests/test_calculator.py\nModuleNotFoundError: No module named 'qa_nonexistent_pkg_xyz'",
    )
    cause = environment.classify_execution(execution)
    assert cause is not None
    assert cause.kind == "missing_dependency"
    assert cause.cause == (
        "missing dependency: qa_nonexistent_pkg_xyz (not installable or not declared in requirements)"
    )


def test_missing_module_on_stderr_only_is_still_found():
    execution = _exec(
        collected=66,
        errors=2,
        stderr="tests/test_timed.py:3: in <module>\n    from freezegun import freeze_time\n"
        "E   ModuleNotFoundError: No module named 'freezegun'",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_dependency"
    assert "missing dependency: freezegun" in cause.cause


def test_wrapped_exception_message_is_matched_across_the_line_break():
    """pytest wraps long lines and interleaves ANSI codes, so the separator
    between the exception name and its message is whitespace, not one space."""
    execution = _exec(
        collected=0,
        errors=0,
        exit_code=1,
        stderr="ModuleNotFoundError:\n No module named 'trio'\n\x1b[0m",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_dependency"
    assert "missing dependency: trio" in cause.cause


def test_plain_importerror_is_matched_too():
    execution = _exec(stderr="ImportError: No module named 'solders'")
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_dependency"
    assert "missing dependency: solders" in cause.cause


def test_a_bare_no_module_named_string_without_the_exception_is_not_a_cause():
    """Prose or a test's own output mentioning the phrase is not an import error."""
    execution = _exec(collected=1, errors=1, stdout="assert 'No module named x' in banner")
    assert environment.classify_execution(execution) is None


# --- generated build artifacts ----------------------------------------------
def test_generated_version_module_in_a_src_layout_is_a_build_artifact(tmp_path):
    root = _package(tmp_path, "src.humanize")
    execution = _exec(
        collected=0,
        errors=6,
        stderr="src/humanize/__init__.py:1: in <module>\n    from ._version import __version__\n"
        "E   ModuleNotFoundError: No module named 'humanize._version'",
    )
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.kind == "generated_module"
    assert cause.cause == (
        "generated module absent: humanize._version (written at build time, not present in the source tree)"
    )


def test_generated_version_module_in_a_flat_layout_is_a_build_artifact(tmp_path):
    root = _package(tmp_path, "widget")
    execution = _exec(stderr="ModuleNotFoundError: No module named 'widget._version'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.kind == "generated_module"
    assert "generated module absent: widget._version" in cause.cause


def test_generated_module_in_a_nested_package_is_recognised(tmp_path):
    root = _package(tmp_path, "src.widget.inner")
    execution = _exec(stderr="ModuleNotFoundError: No module named 'widget.inner.__about__'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.kind == "generated_module"
    assert "widget.inner.__about__" in cause.cause


def test_a_missing_module_whose_package_is_absent_stays_a_dependency(tmp_path):
    root = _package(tmp_path, "src.other")
    execution = _exec(stderr="ModuleNotFoundError: No module named 'humanize._version'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.kind == "missing_dependency"


def test_a_package_without_init_does_not_make_a_generated_module(tmp_path):
    root = tmp_path / "proj"
    (root / "src" / "humanize").mkdir(parents=True)
    execution = _exec(stderr="ModuleNotFoundError: No module named 'humanize._version'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.kind == "missing_dependency"


def test_a_missing_submodule_of_the_projects_own_package_is_a_defect(tmp_path):
    """Not a build artifact and not an environment stop: ``humanize.helpers`` is
    the project's own submodule, so its absence accuses the project."""
    root = _package(tmp_path, "src.humanize")
    execution = _exec(stderr="ModuleNotFoundError: No module named 'humanize.helpers'")
    assert environment.classify_execution(execution, project_root=root) is None


def test_a_top_level_version_module_is_not_dotted_and_stays_a_dependency(tmp_path):
    root = _package(tmp_path, "src.humanize")
    execution = _exec(stderr="ModuleNotFoundError: No module named '_version'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.kind == "missing_dependency"


def test_without_a_project_root_a_version_module_is_only_a_dependency():
    execution = _exec(stderr="ModuleNotFoundError: No module named 'humanize._version'")
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_dependency"


# --- the project's own modules are never an environment problem -------------
def test_a_test_importing_the_projects_own_missing_module_is_a_defect(tmp_path):
    """Escalation 1: ``from mypkg.core import add`` fails because the project
    lost ``mypkg/core.py``. Calling that a missing dependency would hand the
    project an environment excuse for its own broken import."""
    root = _package(tmp_path, "mypkg")
    execution = _exec(
        stderr="tests/test_add.py:2: in <module>\n    from mypkg.core import add\n"
        "E   ModuleNotFoundError: No module named 'mypkg.core'",
    )
    assert environment.classify_execution(execution, project_root=root) is None


def test_a_plain_top_level_module_counts_as_the_projects_own(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "calculator.py").write_text("", encoding="utf-8")
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'calculator.helpers'")
    assert environment.classify_execution(execution, project_root=root) is None


def test_a_module_that_exists_in_the_tree_is_the_projects_own(tmp_path):
    """Present but not importable is a packaging defect, not a missing package."""
    apps = (tmp_path / "proj" / "apps" / "mypkg").absolute()
    apps.mkdir(parents=True)
    (apps / "__init__.py").write_text("", encoding="utf-8")
    (apps / "core.py").write_text("", encoding="utf-8")
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'mypkg.core'")
    assert environment.classify_execution(execution, project_root=tmp_path / "proj") is None


def test_a_directory_without_init_is_not_the_projects_own_package(tmp_path):
    root = tmp_path / "proj"
    (root / "trio").mkdir(parents=True)
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'trio'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause is not None and cause.kind == "missing_dependency"


def test_a_nested_copy_does_not_make_an_absent_top_level_module_own(tmp_path):
    """Single-part names resolve only at the tree root or under ``src/``, so a
    ``docs/trio/__init__.py`` neither excuses nor accuses the run."""
    vendor = tmp_path / "proj" / "docs" / "trio"
    vendor.mkdir(parents=True)
    (vendor / "__init__.py").write_text("", encoding="utf-8")
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'trio'")
    cause = environment.classify_execution(execution, project_root=tmp_path / "proj")
    assert cause is not None and cause.kind == "missing_dependency"


# --- a project must not be able to fake an environment failure --------------
def test_a_conftest_that_raises_module_not_found_error_is_a_defect(tmp_path):
    """Escalation 2: the name is third-party, but the project raised the error
    itself, so no environment claim is accepted for it."""
    root = _package(tmp_path, "widget")
    execution = _exec(
        stderr="tests/conftest.py:7: in <module>\n"
        "    raise ModuleNotFoundError(\"No module named 'numpy'\")\n"
        "E   ModuleNotFoundError: No module named 'numpy'",
    )
    assert environment.classify_execution(execution, project_root=root) is None


def test_a_deliberate_raise_is_refused_even_without_a_project_tree():
    execution = _exec(
        stderr="conftest.py:7: in <module>\n"
        "    raise ModuleNotFoundError(\"No module named 'numpy'\")\n"
        "E   ModuleNotFoundError: No module named 'numpy'",
    )
    assert environment.classify_execution(execution) is None


def test_a_raise_inside_a_fixture_body_is_refused_too(tmp_path):
    root = _package(tmp_path, "widget")
    execution = _exec(
        stderr="tests/conftest.py:12: in pytest_configure\n"
        "    raise ImportError(\"No module named 'numpy'\")\n"
        "E   ImportError: No module named 'numpy'",
    )
    assert environment.classify_execution(execution, project_root=root) is None


def test_a_deliberately_raised_metadata_error_is_a_defect_too(tmp_path):
    root = _package(tmp_path, "widget")
    execution = _exec(
        stderr="tests/conftest.py:9: in <module>\n"
        "    raise PackageNotFoundError(\"No package metadata was found for widget\")\n"
        "E   importlib.metadata.PackageNotFoundError: No package metadata was found for widget",
    )
    assert environment.classify_execution(execution, project_root=root) is None


def test_an_import_statement_frame_still_reads_as_an_environment_stop(tmp_path):
    """The frame also proves what the project did *not* do: a plain import of a
    real third-party name remains environmental."""
    root = _package(tmp_path, "widget")
    execution = _exec(
        stderr="tests/test_timed.py:3: in <module>\n    from freezegun import freeze_time\n"
        "E   ModuleNotFoundError: No module named 'freezegun'",
    )
    cause = environment.classify_execution(execution, project_root=root)
    assert cause is not None and cause.kind == "missing_dependency"


# --- what the manifests say about the missing name --------------------------
def test_a_declared_but_absent_dependency_says_so(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements-test.txt").write_text(
        "pytest\nhypothesis[dateutil]>=6.0\n# a comment\n-r base.txt\n", encoding="utf-8"
    )
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'hypothesis'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.cause == (
        "missing dependency: hypothesis (declared in the project manifest but not installed)"
    )


def test_declared_names_are_read_from_every_pyproject_group(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "svc"\ndependencies = ["flask>=2"]\n\n'
        '[project.optional-dependencies]\ntest = ["pytest-cov"]\ndocs = ["Sphinx"]\n\n'
        '[dependency-groups]\ndev = ["ruff", {requirement = "Tabulate"}]\n',
        encoding="utf-8",
    )
    names = environment.declared_dependency_names(root)
    assert {"flask", "pytest-cov", "sphinx", "ruff", "tabulate"} <= names


def test_separators_and_case_do_not_hide_a_declared_name(tmp_path):
    """PEP 503 form: an import of ``qa_json_lib`` answers the declared ``qa-JSON-lib``."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("qa-JSON-lib==1.0\n", encoding="utf-8")
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'qa_json_lib'")
    cause = environment.classify_execution(execution, project_root=root)
    assert "declared in the project manifest but not installed" in cause.cause


def test_an_undeclared_name_keeps_the_installable_or_undeclared_wording(tmp_path):
    root = _package(tmp_path, "widget")
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'qa_absent_dep'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.cause == (
        "missing dependency: qa_absent_dep (not installable or not declared in requirements)"
    )


def test_an_unreadable_manifest_only_costs_the_wording(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text("not = [ valid toml", encoding="utf-8")
    execution = _exec(stderr="E   ModuleNotFoundError: No module named 'qa_absent_dep'")
    cause = environment.classify_execution(execution, project_root=root)
    assert cause.kind == "missing_dependency"
    assert "not installable or not declared" in cause.cause


# --- distribution metadata --------------------------------------------------
def test_missing_package_metadata_names_the_distribution():
    execution = _exec(
        collected=33221,
        errors=1,
        exit_code=2,
        stderr="tests/test_deprecations.py:43: in <module>\n"
        '    (click, "__version__", importlib.metadata.version("click")),\n'
        "E   importlib.metadata.PackageNotFoundError: No package metadata was found for click",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_metadata"
    assert cause.cause.startswith("missing package metadata: click (")
    assert "never builds or installs the project under test" in cause.cause


def test_a_missing_module_outranks_missing_metadata():
    execution = _exec(
        collected=1,
        errors=2,
        stderr="ModuleNotFoundError: No module named 'freezegun'\n"
        "PackageNotFoundError: No package metadata was found for click",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_dependency"


def test_metadata_for_a_hyphenated_distribution_is_captured():
    execution = _exec(
        collected=1,
        errors=1,
        stderr="PackageNotFoundError: No package metadata was found for my-dist.tool",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_metadata"
    assert "missing package metadata: my-dist.tool" in cause.cause


# --- installation -----------------------------------------------------------
def test_a_refused_unsafe_requirement_is_environmental_even_when_tests_passed():
    execution = _exec(
        collected=1,
        passed=1,
        errors=0,
        exit_code=0,
        install_status="pip install failed: unsafe requirement: -e .[brotli,cli,http2,socks,zstd]",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "refused_requirement"
    assert cause.cause == (
        "refused unsafe requirement: -e .[brotli,cli,http2,socks,zstd] "
        "(installing the project under test is not permitted)"
    )


def test_an_install_failure_without_a_refusal_keeps_the_install_status():
    execution = _exec(collected=0, exit_code=1, install_status="pip install failed: timed out")
    cause = environment.classify_execution(execution)
    assert cause.kind == "install_failed"
    assert cause.cause == "dependency installation failed: pip install failed: timed out"


def test_a_refused_requirement_outranks_the_import_error_it_caused():
    """httpx shape: the refusal made every later import fail, so the refusal is
    the cause the report names. The import error is a symptom of it."""
    execution = _exec(
        collected=0,
        exit_code=1,
        install_status="pip install failed: unsafe requirement: -e .[brotli]",
        stderr="ModuleNotFoundError: No module named 'trio'",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "refused_requirement"
    assert cause.cause == (
        "refused unsafe requirement: -e .[brotli] "
        "(installing the project under test is not permitted)"
    )


def test_an_import_failure_outranks_an_ordinary_install_failure():
    """Only a refusal is the stronger fact; a plain install error stays behind
    the concrete collection error in the report."""
    execution = _exec(
        collected=0,
        exit_code=1,
        install_status="pip install failed: timed out",
        stderr="ModuleNotFoundError: No module named 'trio'",
    )
    cause = environment.classify_execution(execution)
    assert cause.kind == "missing_dependency"
    assert "trio" in cause.cause


def test_a_successful_install_is_not_a_cause():
    execution = _exec(collected=1, passed=1, exit_code=0, install_status="ok")
    assert environment.classify_execution(execution) is None


# --- tolerant input ---------------------------------------------------------
def test_missing_counters_and_exit_code_are_tolerated():
    cause = environment.classify_execution({"stderr": "ModuleNotFoundError: No module named 'trio'"})
    assert cause.kind == "missing_dependency"


def test_a_missing_exit_code_is_not_treated_as_a_pass():
    execution = _exec(
        collected=1,
        passed=1,
        errors=0,
        stderr="ModuleNotFoundError: No module named 'trio'",
    )
    del execution["exit_code"]
    # exit_code absent means "unknown", not 0, so a clean-looking run is still
    # examined and the import error is reported.
    assert environment.classify_execution(execution).kind == "missing_dependency"
    # ...while the same counters with no cause text stay unexplained.
    assert environment.classify_execution({"collected": 1, "passed": 1}) is None


def test_none_values_do_not_raise():
    execution = _exec(collected=None, passed=None, failed=None, errors=None, exit_code=None,
                      stdout=None, stderr=None, install_status=None)
    assert environment.classify_execution(execution) is None
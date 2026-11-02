"""Environmental stops: the run could not witness the tests at all.

A stop is environmental only when nothing accuses the project under test: no
test failed, and the reason is a precondition of *this* environment - a test
dependency that cannot be installed offline, a requirement the policy refused,
an installed distribution's metadata, a module written at build time, or a
required sandbox that is unavailable. Every other stop stays a VETO.

Three guards keep a project from borrowing an environment excuse for its own
broken import: a name that is one of the project's own packages or modules is a
defect, a dotted name that resolves to files somewhere inside the tree is a
defect (it exists but is not importable), and an import error the project raised
on purpose is a defect - proven by a traceback whose raising frame shows a
``raise`` statement. Where the evidence runs out the classifier says so and the
caller keeps the VETO.

This module is pure classification. It imports no agent and runs no command, so
the roles, the orchestrator and the report layer share one definition.

The same boundary also runs *inside* a suite that did execute. ``classify_failure``
answers it per failing test, for the two cases where the isolated copy differs
from the developer's checkout in a way the project cannot help: a test that needs
the repository's VCS state (the copy carries no ``.git``) and a test that shells
out to a program the sandbox does not carry (it gets an allowlisted PATH and a
sandbox HOME, so a tool living in the developer's home directory is invisible).
``annotate_failures`` tags every failure and returns the two counts, so
environment-only failures can be reported apart instead of inflating the product
failure total. Anything this classifier cannot prove environmental stays a product
failure: hiding a defect behind exit 2 is the worse mistake.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# pytest interleaves ANSI colour codes and tracebacks wrap, so the separator
# between the exception name and its message is whitespace, not a fixed space.
IMPORT_ERROR_RE = re.compile(r"(?:ModuleNotFoundError|ImportError):\s+No module named '([A-Za-z0-9_.]+)'")
METADATA_ERROR_RE = re.compile(
    r"PackageNotFoundError:\s+No package metadata was found for ([A-Za-z0-9_.\-]+)"
)
INSTALL_FAILED_PREFIX = "pip install failed"
UNSAFE_REQUIREMENT_MARKER = "unsafe requirement:"
# Modules a build backend writes into the package when it is installed. They are
# absent from a source checkout by design, not because the project is broken.
GENERATED_MODULE_LABELS = frozenset({"_version", "version", "_build_meta", "__about__"})
# A traceback frame as pytest prints it (``--tb=short`` keeps the raising one):
#   conftest.py:7: in <module>
# The path may contain spaces, so the location is matched lazily up to the line
# number; the source that raised is printed on the next line.
FRAME_RE = re.compile(r"^(?P<location>.*\.py):(?P<lineno>\d+): in (?P<func>.+)$")
RAISE_RE = re.compile(r"^raise\b")
# A requirement line's distribution name, up to the first specifier, extra or
# environment marker. ``-r other.txt`` and URLs are not names and never match.
REQUIREMENT_NAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)")
# Path components that can never hold the project's own modules, so a tree
# lookup stops there instead of blaming a vendored copy for a missing package.
NON_SOURCE_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        "site-packages",
        "dist-packages",
        "build",
        "dist",
    }
)
REQUIREMENT_FILE_NAMES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt")


@dataclass(frozen=True)
class EnvCause:
    """One environmental precondition, with the exact text to report."""

    kind: str
    cause: str


def _is_generated_module(name: str, project_root: Path | None) -> bool:
    """True for ``pkg._version`` when ``pkg`` really is in the source tree.

    The package directory must exist, so a module the project simply lost is
    still reported as a missing dependency instead of a build artifact.
    """
    if project_root is None or "." not in name:
        return False
    package, _, label = name.rpartition(".")
    if label not in GENERATED_MODULE_LABELS:
        return False
    parts = package.split(".")
    for base in (project_root, project_root / "src"):
        if (base.joinpath(*parts) / "__init__.py").is_file():
            return True
    return False


def _normalize(name: Any) -> str:
    """PEP 503 comparison form: lower case, every run of ``-_.`` becomes a dash.

    ``Foo-bar`` and ``foo_bar`` normalize alike, so a declared ``hypothesis``
    still answers an imported ``hypothesis`` when the separators differ.
    """
    return re.sub(r"[-_.]+", "-", str(name or "")).strip().lower()


def own_package_dir_names(project_root: Path) -> set[str]:
    """Top-level import names backed by a package directory inside the tree.

    The same directory-backed rule Sable uses for its own-package exemption: a
    directory holding ``__init__.py`` at the project root or under ``src/``.
    """
    names: set[str] = set()
    for base in (project_root / "src", project_root):
        if not base.is_dir():
            continue
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and (child / "__init__.py").is_file():
                names.add(child.name)
    return names


def own_import_names(project_root: Path) -> set[str]:
    """Import names the project can be accused of losing: packages and modules.

    Wider than Sable's package-only rule, and deliberately so in this one
    direction. For the network guard a wide net would create a bypass; here a
    wide net only ever produces a VETO, which is the conservative answer, so a
    plain ``<name>.py`` at the root or under ``src/`` counts as the project's own.
    """
    names = own_package_dir_names(project_root)
    for base in (project_root / "src", project_root):
        if not base.is_dir():
            continue
        try:
            children = list(base.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_file() and child.suffix == ".py" and child.name != "__init__.py":
                names.add(child.stem)
    return names


def _module_path_in_tree(project_root: Path, name: str) -> bool:
    """True when a *dotted* module path resolves to files somewhere in the tree.

    A submodule that exists but cannot be imported is a packaging defect, not a
    missing dependency. Single-part names are not searched here: only ``root``
    and ``src/`` can supply a top-level module, and ``own_import_names`` already
    covers them - finding ``docs/foo.py`` must not excuse or accuse ``foo``.
    """
    parts = tuple(part for part in str(name or "").split(".") if part)
    if len(parts) < 2:
        return False
    leaf = parts[-1]
    try:
        # A submodule is either a file ``<leaf>.py`` or a package directory
        # ``<leaf>/`` holding ``__init__.py``; ``rglob(leaf)`` alone would miss
        # the file, because the pattern matches a name exactly, not "leaf.py".
        candidates = list(project_root.rglob(f"{leaf}.py")) + list(project_root.rglob(leaf))
    except OSError:
        return False
    for hit in candidates:
        try:
            relative = hit.relative_to(project_root)
        except ValueError:
            continue
        if any(part in NON_SOURCE_DIR_NAMES for part in relative.parts):
            continue
        if hit.is_file() and hit.suffix == ".py":
            chain = relative.with_suffix("").parts
        elif hit.is_dir() and (hit / "__init__.py").is_file():
            chain = relative.parts
        else:
            continue
        if chain[-len(parts):] == parts:
            return True
    return False


def _raised_by_project(text: str, needle_start: int) -> bool:
    """True when the project raised this import error itself.

    pytest prints the raising frame before the exception line::

        tests/conftest.py:7: in <module>
            raise ModuleNotFoundError("No module named 'numpy'")
        E   ModuleNotFoundError: No module named 'numpy'

    A frame whose source line starts with ``raise`` means the project chose to
    fail this way, so no environment claim is accepted for it - otherwise a
    single line in a conftest could hide every finding behind an exit 2.
    """
    lines = text[:needle_start].splitlines()
    for index in range(len(lines) - 1, -1, -1):
        if not FRAME_RE.match(lines[index].strip()):
            continue
        for following in lines[index + 1:]:
            stripped = following.strip()
            if stripped:
                return bool(RAISE_RE.match(stripped))
            break
        return False
    return False


def _requirement_name(line: Any) -> str:
    """Distribution name of one requirement line, ``''`` when it has none."""
    text = str(line or "").split("#", 1)[0].strip()
    if not text or text.startswith("-") or "://" in text or text.startswith("."):
        return ""
    match = REQUIREMENT_NAME_RE.match(text)
    return _normalize(match.group(1)) if match else ""


def _names_from(entries: Any) -> set[str]:
    """Requirement names from a list of strings or PEP 735 group tables."""
    found: set[str] = set()
    for entry in entries or []:
        if isinstance(entry, str):
            name = _requirement_name(entry)
        elif isinstance(entry, dict):
            name = _normalize(entry.get("name") or "") or _requirement_name(entry.get("requirement", ""))
        else:
            name = ""
        if name:
            found.add(name)
    return found


def declared_dependency_names(project_root: Path) -> set[str]:
    """Dependency names the project declares, however it declares them.

    Requirements files, ``[project].dependencies``, **every**
    ``[project.optional-dependencies]`` group and ``[dependency-groups]``
    (PEP 735). Unreadable or invalid manifests add nothing: this only sharpens
    the cause text, it never decides the verdict.
    """
    names: set[str] = set()
    for file_name in REQUIREMENT_FILE_NAMES:
        path = project_root / file_name
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        names.update(_requirement_name(line) for line in content.splitlines())
        names.discard("")
    pyproject = project_root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            project = data.get("project")
            project = project if isinstance(project, dict) else {}
            names.update(_names_from(project.get("dependencies")))
            optional = project.get("optional-dependencies")
            if isinstance(optional, dict):
                for group in optional.values():
                    names.update(_names_from(group))
            groups = data.get("dependency-groups")
            if isinstance(groups, dict):
                # PEP 735: {group = ["ruff", {requirement = "Tabulate"}]} - the
                # group *keys* are not packages, only the entries in each list.
                for group in groups.values():
                    names.update(_names_from(group))
            elif isinstance(groups, list):
                names.update(_names_from(groups))
    return names


def _missing_module_detail(name: str, project_root: Path | None) -> str:
    """Finish the cause sentence with what the manifests say about ``name``.

    Only the wording depends on the manifest; the verdict was already decided.
    """
    if project_root is not None and _normalize(name.split(".")[0]) in declared_dependency_names(
        project_root
    ):
        return "declared in the project manifest but not installed"
    return "not installable or not declared in requirements"


def classify_execution(
    execution: Mapping[str, Any],
    *,
    project_root: Path | None = None,
) -> EnvCause | None:
    """Return the environmental cause of a stop, or ``None`` when it is not one.

    ``None`` means "not environmental": a failing test, a clean pass, or a stop
    this classifier cannot explain (for example an empty test tree). Callers
    must keep their VETO in that case.

    ``project_root`` is the tree that was tested (the isolated copy). Without it
    nothing can be proved about the project's own modules or declared
    dependencies, so a missing module stays a dependency - but an import error
    the project raised on purpose is still refused, because that needs no tree.
    """
    if int(product_failure_count(execution)) > 0:
        # A test that really failed accuses the project. Failures this module
        # proved to be environmental are taken out of that count elsewhere, so an
        # ``environment_failed``-only run can still be an environmental stop.
        return None
    install_status = str(execution.get("install_status") or "")
    install_failed = install_status.startswith(INSTALL_FAILED_PREFIX)
    collected = int(execution.get("collected") or 0)
    errors = int(execution.get("errors") or 0)
    raw_exit = execution.get("exit_code")
    exit_code = int(raw_exit if raw_exit is not None else -1)
    if collected > 0 and errors == 0 and exit_code == 0 and not install_failed:
        return None

    text = f"{execution.get('stdout') or ''}\n{execution.get('stderr') or ''}"
    # A requirement the policy refused is the stronger fact: it explains every
    # import that failed afterwards, so it outranks the collection error it
    # caused instead of being hidden behind the first missing module name.
    if install_failed and UNSAFE_REQUIREMENT_MARKER in install_status:
        line = install_status.partition(UNSAFE_REQUIREMENT_MARKER)[2].strip()
        return EnvCause(
            "refused_requirement",
            f"refused unsafe requirement: {line} (installing the project under test is not permitted)",
        )
    missing = IMPORT_ERROR_RE.search(text)
    if missing:
        if _raised_by_project(text, missing.start()):
            return None
        name = missing.group(1)
        if _is_generated_module(name, project_root):
            return EnvCause(
                "generated_module",
                f"generated module absent: {name} (written at build time, not present in the source tree)",
            )
        if project_root is not None:
            top = name.split(".")[0]
            if top in own_import_names(project_root) or _module_path_in_tree(project_root, name):
                # The name belongs to the project: its own module or submodule is
                # gone, or it is in the tree but not importable. Either way this
                # accuses the project, so the caller keeps the VETO.
                return None
        return EnvCause(
            "missing_dependency",
            f"missing dependency: {name} ({_missing_module_detail(name, project_root)})",
        )
    metadata = METADATA_ERROR_RE.search(text)
    if metadata:
        if _raised_by_project(text, metadata.start()):
            return None
        return EnvCause(
            "missing_metadata",
            f"missing package metadata: {metadata.group(1)} (the suite needs an installed "
            "distribution; this run never builds or installs the project under test)",
        )
    if install_failed:
        return EnvCause("install_failed", f"dependency installation failed: {install_status}")
    return None


# --- one failing test at a time ----------------------------------------------

# The isolated copy is a file tree, never a repository checkout: ``.git`` is not
# copied (core/isolation.py) so a test that shells out to git fails with git's own
# words. That says something about the copy, not about the project.
GIT_ABSENT_RE = re.compile(r"not a git repository", re.IGNORECASE)
# ``FileNotFoundError: [Errno 2] No such file or directory: 'node'``. The sandbox
# runs with an allowlisted PATH and a sandbox HOME, so a program the developer
# keeps in ~/.local/bin or ~/.nvm is simply not visible, and a test that shells
# out to it fails before it can judge the project.
MISSING_PROGRAM_RE = re.compile(
    r"(?:FileNotFoundError|OSError):\s*\[Errno 2\][^\n'\"]*['\"](?P<name>[^'/\n\"]+)['\"]"
)


def _looks_like_program(name: str) -> bool:
    """True for a bare executable name, false for a path or a data file.

    Deliberately narrow: a name carrying a slash, a backslash or a dot is read as
    a file the project should have produced, which stays a product failure.
    """
    cleaned = (name or "").strip()
    if len(cleaned) < 2 or len(cleaned) > 64:
        return False
    if "/" in cleaned or "\\" in cleaned or "." in cleaned:
        return False
    return all(not char.isspace() for char in cleaned)


def classify_failure(
    failure: Mapping[str, Any], *, project_root: Path | None = None
) -> EnvCause | None:
    """Return why *this* failure belongs to the environment, or ``None``.

    ``None`` means "count it against the project". Only two facts are accepted,
    both of them unambiguous in the failure's own text, and both refused when the
    traceback shows the project raised the error on purpose (the same guard that
    stops a forged import error). Anything else - an assertion, an unexpected
    exception, a missing fixture - stays a product failure, because an unsure
    classifier must never hide a defect behind exit 2.
    """
    text = "\n".join(
        part
        for part in (
            str(failure.get("message") or ""),
            str(failure.get("traceback") or ""),
        )
        if part
    )
    if not text:
        return None
    git = GIT_ABSENT_RE.search(text)
    if git and not _raised_by_project(text, git.start()):
        return EnvCause(
            "missing_vcs_metadata",
            "repository metadata absent: this test needs a real .git, and the isolated "
            "copy is deliberately a file tree without VCS state",
        )
    program = MISSING_PROGRAM_RE.search(text)
    while program:
        name = program.group("name")
        if (
            _looks_like_program(name)
            and not _raised_by_project(text, program.start())
            and not (project_root is not None and (project_root / name).exists())
        ):
            return EnvCause(
                "missing_tool",
                f"external program unavailable: {name} (the sandbox gets an allowlisted "
                "PATH and a sandbox HOME, so a tool installed only in the user's home "
                "directory is not visible to the tested process)",
            )
        program = MISSING_PROGRAM_RE.search(text, program.end())
    # A single test that cannot even be collected because a third-party module is
    # absent is the same precondition the whole-run classifier already excuses, so
    # the per-failure path must agree with it - otherwise fix E's structured
    # failure list would relabel a missing dependency as a product defect and the
    # guard in ``classify_execution`` would force a VETO. The three conservative
    # guards are reused verbatim: a project that raised the error on purpose, a
    # name that is one of the project's own modules, or a dotted name that still
    # resolves inside the tree all stay product failures.
    missing = IMPORT_ERROR_RE.search(text)
    if missing:
        if _raised_by_project(text, missing.start()):
            return None
        name = missing.group(1)
        if _is_generated_module(name, project_root):
            return EnvCause(
                "generated_module",
                f"generated module absent: {name} (written at build time, not present in the source tree)",
            )
        if project_root is not None:
            top = name.split(".")[0]
            if top in own_import_names(project_root) or _module_path_in_tree(project_root, name):
                return None
        return EnvCause(
            "missing_dependency",
            f"missing dependency: {name} ({_missing_module_detail(name, project_root)})",
        )
    return None


def annotate_failures(
    failures: list[dict[str, Any]], *, project_root: Path | None = None
) -> tuple[int, int]:
    """Tag each failure ``product`` or ``environment``; return both counts.

    The tag is written into the report next to the test name, so a reader can see
    which side of the boundary every single failure landed on.
    """
    product = 0
    environment = 0
    for failure in failures:
        cause = classify_failure(failure, project_root=project_root)
        if cause is None:
            failure["classification"] = "product"
            product += 1
        else:
            failure["classification"] = "environment"
            failure["environment_kind"] = cause.kind
            failure["environment_cause"] = cause.cause
            environment += 1
    return product, environment


def product_failure_count(execution: Mapping[str, Any]) -> int:
    """Failures that accuse the project, or the raw count when nothing is known.

    A run whose failures were never annotated (no structured ``failures``) keeps
    its old behaviour: every failure counts against the project, because nothing
    has proved any of them environmental. Only once a failure list has been
    classified is ``product_failed`` trusted - and even then the unclassified
    remainder is never allowed to under-report, so an unsure input stays a VETO.
    """
    raw = execution.get("product_failed")
    failed = int(execution.get("failed") or 0)
    if not execution.get("failures"):
        # Nothing classified: the conservative default from Task 8b.
        return failed
    if raw is None:
        return failed
    # A classified run can legitimately have product_failed < failed (the
    # difference was proven environmental); keep that, but never below zero.
    return max(int(raw), 0)
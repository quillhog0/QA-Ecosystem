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

import ast
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

# pytest interleaves ANSI colour codes and tracebacks wrap, so the separator
# between the exception name and its message is whitespace, not a fixed space.
IMPORT_ERROR_RE = re.compile(r"(?:ModuleNotFoundError|ImportError):\s+No module named '([A-Za-z0-9_.]+)'")
# The exception may print qualified (``importlib.metadata.PackageNotFoundError``)
# or bare (``PackageNotFoundError``); both are the same missing distribution.
METADATA_ERROR_RE = re.compile(
    r"(?:[A-Za-z_][A-Za-z0-9_]*\.)*PackageNotFoundError:\s+No package metadata was found for ([A-Za-z0-9_.\-]+)"
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
# Frames that belong to the interpreter or to installed distributions, never to
# the project source: the standard library lives in a ``pythonX.Y`` (or
# ``pythonX.YZ``) directory, third-party code in ``site-packages``/``dist-packages``.
# Such a frame may raise its own error (``importlib.metadata`` raising
# ``PackageNotFoundError`` is the ordinary way a missing distribution reports
# itself); that is the runtime answering, not the project failing on purpose.
_STDLIB_FRAME_RE = re.compile(r"python\d\.\d+(?:\.\d+)?[/\\]")
_PACKAGE_FRAME_RE = re.compile(r"(?:site|dist)-packages[/\\]")
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
    """True when the project raised this error itself.

    pytest prints the raising frame before the exception line::

        tests/conftest.py:7: in <module>
            raise ModuleNotFoundError("No module named 'numpy'")
        E   ModuleNotFoundError: No module named 'numpy'

    A frame whose source line starts with ``raise`` means the project chose to
    fail this way, so no environment claim is accepted for it - otherwise a
    single line in a conftest could hide every finding behind an exit 2.

    Frames from the standard library or from installed distributions are not the
    project: they raise their own errors constantly (``importlib.metadata``
    raising ``PackageNotFoundError`` is how a missing distribution reports
    itself). The scan skips over those runtime frames and stops at the first
    project frame, and only that frame's source line decides.
    """
    lines = text[:needle_start].splitlines()
    for index in range(len(lines) - 1, -1, -1):
        stripped = lines[index].strip()
        match = FRAME_RE.match(stripped)
        if not match:
            continue
        location = match.group("location")
        if _STDLIB_FRAME_RE.search(location) or _PACKAGE_FRAME_RE.search(location):
            continue
        for following in lines[index + 1:]:
            candidate = following.strip()
            if candidate:
                return bool(RAISE_RE.match(candidate))
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

    # If all test failures in the run were proved to be environmental:
    failures = list(execution.get("failures") or [])
    if failures and int(execution.get("failed") or 0) > 0:
        env_causes = []
        for f in failures:
            c = classify_failure(f, project_root=project_root)
            if c is not None:
                env_causes.append(c)
        if len(env_causes) == len(failures):
            return env_causes[0]

    return None


# --- one failing test at a time ----------------------------------------------

# The isolated copy is a file tree, never a repository checkout: ``.git`` is not
# copied (core/isolation.py) so a test that shells out to git fails with git's own
# words. That says something about the copy, not about the project.
GIT_ABSENT_RE = re.compile(
    r"(?:not a git repository|fatal: not a git repository|No such file or directory: ['.]git)",
    re.IGNORECASE,
)
# ``FileNotFoundError: [Errno 2] No such file or directory: 'node'``. The sandbox
# runs with an allowlisted PATH and a sandbox HOME, so a program the developer
# keeps in ~/.local/bin or ~/.nvm is simply not visible, and a test that shells
# out to it fails before it can judge the project.
MISSING_PROGRAM_RE = re.compile(
    r"(?:FileNotFoundError|OSError):\s*\[Errno 2\][^\n'\"]*['\"](?P<name>[^'/\n\"]+)['\"]"
)
# A launcher or wrapper program names a missing tool in its own words instead of
# raising an OSError: the phrase ``not found`` closes the line
# (``bash: line 1: node: not found``). Requiring the end of line keeps ordinary
# assertion prose ("assert 'plugin not found' in output") out of the rule. The
# name candidate is taken from the *text before* the phrase - the last token,
# colon stripped - never from a capture group. A line that opens with an
# exception class header (``OSError: node not found``) is a program's own
# message, not a tool report, and is refused wholesale; only OSError's canonical
# ``[Errno 2]`` repr (MISSING_PROGRAM_RE above) excuses a missing module import.
NOT_FOUND_PHRASE_RE = re.compile(r"\s+not found\s*$", re.MULTILINE)
EXCEPTION_HEADER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception|Warning|Interrupt):")
# The OS-level classes a missing program actually surfaces as: ``os.exec*`` and
# ``subprocess.Popen`` raise ``OSError``/``FileNotFoundError`` when the program
# cannot be found, so a bare ``OSError: node not found`` is the environment
# speaking. Every other exception class (``RuntimeError: handler not found``) is
# a message the project wrote itself and never excuses a missing tool. This is a
# taxonomy of exception classes, not an allowlist of program names.
OS_ERROR_RE = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*(?:OSError|FileNotFoundError):")
# ``subprocess.CalledProcessError: Command '['git', 'ls-files']' returned
# non-zero exit status 128.`` - git answering that the copy is not a repository
# in its own exit code, without printing the words GIT_ABSENT_RE knows. The
# exception's own ``str()`` wraps the printed command (argv list repr, or the
# shell string repr) between the literal ``Command '`` and ``' returned``, so the
# capture takes the text in between and ``_argv_program`` parses it - the program
# name is read from the command, never guessed from prose.
CALLED_PROCESS_ERROR_RE = re.compile(
    r"""CalledProcessError:\s+Command\s+'(?P<argv>.*?)'\s+returned"""
)
# ``PermissionError: [Errno 13] Permission denied: '/var/run/app.pid'``. The
# sandbox bind-mounts only the isolated copy (and a tmp home) writable, so a
# write aimed at the host - an absolute path outside the copy, or a cwd-relative
# pid file the runtime protects - is the environment refusing, not the project
# failing.
PERMISSION_DENIED_RE = re.compile(
    r"PermissionError:\s*\[Errno 13\][^\n]*?['\"](?P<path>[^'\n]+)['\"]"
)
TTY_ERROR_RE = re.compile(
    r"(?:inappropriate ioctl for device|termios\.error|"
    r"UnsupportedOperation:[^\n]*?(?:fileno|pseudofile)|"
    r"pytest_pager|no pager found|pager is not available|terminal pager|not a tty|"
    r"device not configured|requires a tty|stdin is not a tty)",
    re.IGNORECASE,
)
READ_ONLY_FS_RE = re.compile(
    r"(?:(?:OSError|PermissionError):\s*\[Errno 30\]\s*Read-only file system|EROFS:\s*read-only file system|Error:\s*EROFS)",
    re.IGNORECASE,
)
CACHED_NODE_MODULES_ERROR_RE = re.compile(
    r"(?:ERR_MODULE_NOT_FOUND|MODULE_NOT_FOUND|Cannot find module\s*['\"][^'\"]*node_modules|"
    r"node_modules/\.bin/[^:\s]+:\s*(?:not found|No such file or directory)|"
    r"sh:\s*line\s*\d+:\s*vitest:\s*command not found|sh:\s*vitest:\s*not found)",
    re.IGNORECASE,
)
# A test that cannot proceed because it needs an *external program* the hermetic
# sandbox simply does not carry announces that in its own words instead of raising
# an OS error the rules above can trust: ``<Tool> is required ... in CI environment``
# or ``<Tool> is required ... not installed/available``. Deliberately narrower than
# the wording it recognises, so it can never hide a real defect:
#   * the name must be a bare program token sitting directly against ``is required``
#     (``node``), never a common noun or a path - an ordinary domain validation
#     ("a password is required", "a valid session id is required") names a concept,
#     not an executable, and is refused by the program-shape test; and
#   * the very same sentence must explicitly frame the absence as a build/runtime
#     toolchain precondition (a CI marker or a "not installed/available/found"
#     clause), so a plain product assertion that merely contains the word "required"
#     is never excused; and
#   * the program must not resolve to a file inside the tested tree (a launcher the
#     project ships itself is the project's own concern).
# No list of tool names is consulted, so this is project-agnostic.
REQUIRED_TOOL_RE = re.compile(
    r"(?P<tool>[A-Za-z][A-Za-z0-9+#._-]{0,40})\s+is\s+required\b[^\n]*?"
    r"(?:\bCI\b|in\s+CI\b|\bnot\s+(?:installed|available|found|present)\b|\bon\s+this\s+machine\b)",
    re.IGNORECASE,
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


def _base_program_name(token: str) -> str:
    """Reduce a prose tool name to a bare program candidate (``Node.js`` -> ``node``).

    Only cosmetic decoration is stripped - a leading article, any ``.js``/``.exe``/
    version-style suffix, and surrounding punctuation - and the result is lower-cased.
    This never consults a table of known tools; it just reshapes whatever word the
    message put in front of ``is required``.
    """
    cleaned = (token or "").strip().strip("'\"`()[]{}.,;:")
    if cleaned[:1].isalpha() and cleaned.lower().startswith("the "):
        cleaned = cleaned[4:].strip()
    # Drop a trailing dotted extension or version: Node.js -> Node, python3.11 -> python3.
    cleaned = re.sub(r"\.[A-Za-z][A-Za-z0-9]*$", "", cleaned)
    cleaned = re.sub(r"\.\d+(?:\.\d+)*$", "", cleaned)
    return cleaned.lower()


def _argv_program(argv_text: str) -> str:
    """Program name from the command printed in a CalledProcessError.

    Handles both forms - the argv list repr (``['git', 'ls-files', '-z']``) and
    the single shell string (``'git ls-files -z'``) - by parsing the literal
    with ``ast.literal_eval`` and taking the basename of its first element (or
    first word). Anything unparseable yields the first word of the raw text,
    and an empty result means "no program candidate".
    """
    raw = (argv_text or "").strip()
    if not raw:
        return ""
    try:
        parsed = ast.literal_eval(raw)
    except (SyntaxError, ValueError):
        parsed = None
    if isinstance(parsed, (list, tuple)):
        items = [str(item) for item in parsed if item]
    elif isinstance(parsed, str):
        items = parsed.split()
    else:
        items = raw.split()
    if not items:
        return ""
    first = items[0].strip("'\"()[]")
    if not first:
        return ""
    return Path(first).name


def _program_before_phrase(text: str, phrase_start: int) -> str:
    """The program candidate in front of a ``not found`` phrase, ``''` for none.

    Read from the text position, not from a capture group: the last token of the
    line holding the phrase, with a trailing colon stripped (``bash: line 1:
    node: not found`` names ``node``). A line that opens with an exception class
    header (``OSError: node not found``) is that program's own message, not a
    launcher reporting a missing tool, and yields no candidate at all.
    """
    head = text[:phrase_start].rstrip("\"'`")
    if not head:
        return ""
    line = head[head.rfind("\n") + 1:]
    line = re.sub(r"^E\s+", "", line.strip())
    if EXCEPTION_HEADER_RE.match(line) and not OS_ERROR_RE.match(line):
        return ""
    tokens = line.split()
    if not tokens:
        return ""
    candidate = tokens[-1].rstrip(":")
    if ":" in candidate or not candidate.isascii() or not candidate.islower():
        return ""
    return candidate


def _permission_outside_copy(path_text: str, project_root: Path | None) -> bool:
    """True when a denied write target could only be the sandbox refusing.

    Accepted, in order: an *absolute* path that is provably outside the tested
    tree (a host location the bind mount never makes writable), or a
    cwd-relative pid file (``*.pid``), which the runtime protects wherever the
    process points its working directory. A denied write to an absolute path
    *inside* the copy is the project's own business and stays product. Without
    a known ``project_root`` nothing is provable and nothing is excused.
    """
    if project_root is None:
        return False
    candidate = (path_text or "").strip()
    if not candidate:
        return False
    if candidate.lower().endswith(".pid") and not os.path.isabs(candidate):
        return True
    if not os.path.isabs(candidate):
        return False
    try:
        Path(candidate).relative_to(project_root)
    except ValueError:
        return True
    return False


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
    # A git subprocess that exits non-zero in a copy without any .git: git's
    # answer to "which repository is this" is an exit code, not the words
    # GIT_ABSENT_RE knows. Only accepted when the tested tree provably has no
    # repository state - with a real .git present, a failing git call is the
    # project's own affair.
    called = CALLED_PROCESS_ERROR_RE.search(text)
    if (
        called
        and _argv_program(called.group("argv")) == "git"
        and project_root is not None
        and not (project_root / ".git").exists()
        and not _raised_by_project(text, called.start())
    ):
        return EnvCause(
            "missing_vcs_metadata",
            "repository metadata absent: this test shells out to git, and the "
            "isolated copy is deliberately a file tree without VCS state",
        )
    # The same invisible-tool fact as MISSING_PROGRAM_RE, phrased by the tool's
    # own launcher ("node: not found") instead of by the OSError repr.
    not_found = NOT_FOUND_PHRASE_RE.search(text)
    while not_found:
        name = _program_before_phrase(text, not_found.start())
        if (
            _looks_like_program(name)
            and not _raised_by_project(text, not_found.start())
            and not (project_root is not None and (project_root / name).exists())
        ):
            return EnvCause(
                "missing_tool",
                f"external program unavailable: {name} (the sandbox gets an allowlisted "
                "PATH and a sandbox HOME, so a tool installed only in the user's home "
                "directory is not visible to the tested process)",
            )
        not_found = NOT_FOUND_PHRASE_RE.search(text, not_found.end())
    # The same invisible-tool fact, this time phrased by the test itself: it needs
    # an external program the hermetic sandbox does not carry and says so ("Node.js
    # is required ... in CI environment"). Guarded tightly so it cannot excuse a
    # product defect - see REQUIRED_TOOL_RE.
    required = REQUIRED_TOOL_RE.search(text)
    while required:
        token = required.group("tool")
        # A missing external tool is named like a product (capitalised, or carrying a
        # version/extension marker such as ``Node.js``/``.NET``/``C++``); a domain
        # requirement is a common noun ("a password is required", "a valid method is
        # required"). This shape test - not a list of tool names - is what keeps a
        # real product assertion from being excused as an absent toolchain.
        named = token[:1].isupper() or any(mark in token for mark in (".", "+", "#"))
        base = _base_program_name(token)
        if (
            named
            and _looks_like_program(base)
            and not _raised_by_project(text, required.start())
            and not (project_root is not None and (project_root / base).exists())
        ):
            return EnvCause(
                "missing_tool",
                f"external program unavailable: {base} (the suite requires {base} as an "
                "external tool and the sandbox does not install it; a missing toolchain "
                "is an environment precondition, not a product defect)",
            )
        required = REQUIRED_TOOL_RE.search(text, required.end())
    # A denied write aimed at the host (absolute path outside the copy) or at a
    # cwd-relative pid file is the sandbox's bound, not a project defect.
    denied = PERMISSION_DENIED_RE.search(text)
    while denied:
        if _permission_outside_copy(denied.group("path"), project_root) and not _raised_by_project(
            text, denied.start()
        ):
            return EnvCause(
                "sandbox_denied_write",
                f"write denied outside the isolated copy: {denied.group('path')} (only "
                "the tested tree and a temporary home are writable in the sandbox)",
            )
        denied = PERMISSION_DENIED_RE.search(text, denied.end())
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
    # Package metadata is absent in an isolated source copy that was never installed.
    metadata = METADATA_ERROR_RE.search(text)
    if metadata:
        if _raised_by_project(text, metadata.start()):
            return None
        return EnvCause(
            "missing_metadata",
            f"missing package metadata: {metadata.group(1)} (the suite needs an installed "
            "distribution; this run never builds or installs the project under test)",
        )
    # sqlite in the sandbox has no writable persistent database by design.
    sqlite_pos = text.find("unable to open database file")
    if sqlite_pos != -1 and not _raised_by_project(text, sqlite_pos):
        return EnvCause(
            "missing_writable_storage",
            "sqlite cannot open a database file (the isolated copy has no writable "
            "persistent storage for sqlite)",
        )
    tty = TTY_ERROR_RE.search(text)
    if tty and not _raised_by_project(text, tty.start()):
        return EnvCause(
            "sandbox_missing_tty",
            f"interactive TTY/pager unavailable in sandbox: {tty.group(0)} "
            "(the test requires an interactive terminal/pager not attached in the isolated sandbox)",
        )
    ro_fs = READ_ONLY_FS_RE.search(text)
    if ro_fs and not _raised_by_project(text, ro_fs.start()):
        return EnvCause(
            "sandbox_read_only_fs",
            "filesystem location is read-only in sandbox (the test attempted to write to a path outside the writable copy)",
        )
    nm_err = CACHED_NODE_MODULES_ERROR_RE.search(text)
    if nm_err and not _raised_by_project(text, nm_err.start()):
        return EnvCause(
            "cached_node_modules_resolution",
            f"cached node_modules resolution issue in sandbox: {nm_err.group(0)}",
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
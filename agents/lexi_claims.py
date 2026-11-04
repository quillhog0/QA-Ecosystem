"""First-class Lexi claim verification: CLI commands/flags, call-forms, env vars, install instructions."""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from agents.inventory import list_docs, list_sources, public_names
from core.packets import DriftFinding

# Known common non-env uppercase tokens
NON_ENV_TOKENS = frozenset({
    "README", "HTTP", "HTTPS", "JSON", "YAML", "REST", "CRUD", "TODO", "NOTE",
    "WARN", "INFO", "VETO", "CLEAR", "BLOCK", "ESCALATE", "MIT", "GPL", "BSD",
    "APACHE", "ASCII", "UTF", "ANSI", "POSIX", "LINUX", "WINDOWS", "MACOS",
    "HTML", "CSS", "SQL", "URI", "URL", "UUID", "API", "CLI", "SDK", "IDE", "CI", "CD",
    "ARGV", "STDOUT", "STDERR", "STDIN", "PATH", "HOME", "TRUE", "FALSE", "NONE",
})

# Built-in or standard library object call names to ignore
COMMON_CALL_OBJECTS = frozenset(
    set(getattr(sys, "stdlib_module_names", set())) | {
        "console", "os", "sys", "path", "json", "JSON", "time", "math", "Math", "logging", "logger",
        "Object", "Array", "Promise", "String", "Number", "Boolean",
        "shutil", "subprocess", "re", "io", "string", "random", "itertools", "functools",
        "pytest", "unittest", "mock", "vi", "jest", "expect", "assert", "describe", "test", "it",
        "self", "cls", "dt",
        "pathlib", "tomllib", "datetime", "urllib", "typing", "dataclasses", "collections", "contextlib", "inspect",
    }
)


EXAMPLE_IDENTIFIERS = {
    "foo", "bar", "baz", "qux", "quux", "corge", "grault", "garply", "waldo", "fred", "plugh", "xyzzy", "thud",
    "my_function", "my_func", "my_var", "my_app", "my_class", "my_method", "my_module", "my_pkg", "my_tool",
    "sample", "example", "dummy", "placeholder", "demo",
}


BUILTIN_METHOD_NAMES = frozenset({
    # Python built-in container & string & IO & db methods
    "append", "extend", "insert", "remove", "pop", "clear", "index", "count", "sort", "reverse", "copy",
    "keys", "values", "items", "get", "update", "setdefault", "popitem",
    "add", "discard", "difference", "intersection", "symmetric_difference", "union", "issubset", "issuperset",
    "write", "read", "readline", "readlines", "close", "flush", "seek", "tell",
    "strip", "lstrip", "rstrip", "split", "rsplit", "join", "replace", "format", "startswith", "endswith", "lower", "upper", "title", "capitalize", "casefold",
    "encode", "decode", "isoformat", "timestamp", "fromtimestamp", "now", "utcnow", "today", "date", "timedelta",
    "execute", "executemany", "cursor", "commit", "rollback", "fetchall", "fetchone", "fetchmany",
    "match", "search", "findall", "finditer", "sub", "subn", "appendleft", "object",
    # JS / TS common built-in methods
    "push", "shift", "unshift", "slice", "splice", "map", "filter", "reduce", "forEach",
    "includes", "indexOf", "lastIndexOf", "find", "findIndex", "some", "every", "fill", "flat", "flatMap",
    "trim", "trimStart", "trimEnd", "toLowerCase", "toUpperCase", "toString", "valueOf", "substring", "charAt",
    "then", "catch", "finally", "emit", "on", "once", "off", "addListener", "removeListener",
    "set", "has", "delete",
})


EXTERNAL_TOOLS = frozenset({
    "git", "curl", "wget", "pyperf", "pytest", "docker", "pip", "npm", "pnpm", "yarn", "tar", "rm", "cp", "mv",
    "cat", "echo", "chmod", "bash", "sh", "make", "sphinx-build", "doc2dash", "coverage", "tox", "pre-commit",
    "bumpversion", "flit", "twine", "wheel", "hatch", "pdm", "poetry", "uv", "ruff", "black", "flake8", "mypy",
    "pyright", "codecov", "towncrier", "cogapp", "pytest-benchmark", "pytest-codspeed",
})


def _is_doc_code_example_ident(ident: str, doc_text: str) -> bool:
    """True if ident is illustrative/example user code or placeholder."""
    lowered = ident.lower()
    if lowered in EXAMPLE_IDENTIFIERS:
        return True
    if any(lowered.startswith(prefix) for prefix in ("example_", "sample_", "dummy_", "my_", "test_", "demo_")):
        return True
    if any(lowered.endswith(suffix) for suffix in ("_example", "_sample", "_dummy")):
        return True
    # Check if defined inside code blocks: e.g. def my_function, function my_func, const foo =, def fn(ident):
    def_patterns = [
        re.compile(rf"def\s+[a-zA-Z0-9_]*\([^)]*\b{re.escape(ident)}\b"),
        re.compile(rf"def\s+{re.escape(ident)}\s*\("),
        re.compile(rf"function\s+[a-zA-Z0-9_]*\([^)]*\b{re.escape(ident)}\b"),
        re.compile(rf"function\s+{re.escape(ident)}\s*\("),
        re.compile(rf"(?:const|let|var)\s+{re.escape(ident)}\s*="),
        re.compile(rf"class\s+{re.escape(ident)}\b"),
        re.compile(rf"['\"]{re.escape(ident)}['\"]"),
    ]
    # Check both markdown fences and rst code blocks
    code_blocks = re.findall(r"```[^\n]*\n(.*?)```", doc_text, re.DOTALL)
    code_blocks += re.findall(r"::[\s\n]+((?:(?:    |\t)[^\n]*\n)+)", doc_text)
    code_blocks += re.findall(r"\.\.\s+(?:code-block|sourcecode|code)::[^\n]*\n((?:(?:    |\t)[^\n]*\n)+)", doc_text)
    for block in code_blocks:
        if any(p.search(block) for p in def_patterns):
            return True
    return False


def _find_cli_definitions(project_root: Path) -> tuple[set[str], set[str], list[str]]:
    """Return (commands, flags, source_locations_searched)."""
    commands: set[str] = set()
    flags: set[str] = set()
    searched: list[str] = []

    # Flag regex: e.g. --dry-run, --port, -v, --name <name>, -p, --port [port]
    flag_re = re.compile(r"['\"](?:-[a-zA-Z0-9],\s*)?(--[a-zA-Z0-9][a-zA-Z0-9_-]*|-[a-zA-Z0-9])(?:[\s<=[].*?)?['\"]")

    for path in list_sources(project_root):
        rel = str(path.relative_to(project_root)).replace("\\", "/")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        searched.append(rel)

        # Python argparse / click / typer
        if path.suffix == ".py":
            # argparse: add_argument('--flag', ...)
            for m in flag_re.finditer(text):
                flags.add(m.group(1))

            # click: @click.command('name'), @click.option('--flag')
            for m in re.finditer(r"@(?:click|app)\.command\(\s*['\"]([^'\"]+)['\"]", text):
                commands.add(m.group(1))
            for m in re.finditer(r"def\s+([a-zA-Z0-9_]+)\s*\(", text):
                commands.add(m.group(1).replace("_", "-"))
                commands.add(m.group(1))

        # TS/JS commander / yargs / cac
        elif path.suffix in (".ts", ".js", ".mjs", ".cjs"):
            # program.command('sync')
            for m in re.finditer(r"\.command\(\s*['\"]([a-zA-Z0-9_-]+)", text):
                commands.add(m.group(1))
            # .option('--dry-run')
            for m in flag_re.finditer(text):
                flags.add(m.group(1))

    return commands, flags, searched


def check_cli_claims(docs_root: Path, project_root: Path) -> list[DriftFinding]:
    """Verify that CLI commands and flags shown in README/docs exist in code."""
    from agents.lexi import _is_historical_doc
    findings: list[DriftFinding] = []
    commands_in_code, flags_in_code, searched = _find_cli_definitions(project_root)

    # CLI command pattern in docs: e.g. mytool sync --dry-run or $ mytool --flag
    cli_line_re = re.compile(
        r"(?:^\s*[$>]\s*|`)([a-zA-Z0-9_-]+)\s+([a-zA-Z0-9_-]+)?\s*([^`\n]*)"
    )

    for doc_path in list_docs(docs_root):
        if not any(doc_path.name.lower().startswith(pfx) for pfx in ("readme", "install")):
            continue
        if _is_historical_doc(doc_path, docs_root):
            continue
        try:
            lines = doc_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        rel_doc = str(doc_path.relative_to(docs_root)).replace("\\", "/") if doc_path.is_relative_to(docs_root) else doc_path.name

        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("<!--") or "-->" in stripped:
                continue
            # Skip external tools
            m_cmd = re.search(r"(?:^\s*[$>]\s*|`)([a-zA-Z0-9_.-]+)", stripped)
            if m_cmd and m_cmd.group(1).lower() in EXTERNAL_TOOLS:
                continue
            # Extract flags from the line
            line_flags = re.findall(r"(--[a-zA-Z0-9][a-zA-Z0-9_-]*)", stripped)
            if "python -m " in stripped and any(mod in stripped for mod in ("timeit", "pip", "pytest", "venv", "http.server", "json.tool")):
                continue
            # Check if this line looks like a CLI invocation
            if "--" in stripped and any(kw in stripped for kw in ("$", ">", "run", "cli", "Usage:", "usage:", "python -m")):
                for flag in line_flags:
                    if flag not in flags_in_code and flag not in ("--help", "--version"):
                        search_note = ", ".join(searched[:5]) + (f" and {len(searched) - 5} more" if len(searched) > 5 else "")
                        findings.append(
                            DriftFinding(
                                kind="missing_cli_flag",
                                path=flag,
                                severity="high",
                                message=(
                                    f"CLI flag '{flag}' documented at {rel_doc}:{lineno} "
                                    f"does not exist in code (searched in {search_note})"
                                ),
                                docs_file=rel_doc,
                                docs_line=lineno,
                            )
                        )

            # Subcommand check
            m = re.search(r"(?:^\s*[$>]\s*|`)[a-zA-Z0-9_-]+\s+([a-zA-Z0-9_-]+)\s+--", stripped)
            if m:
                subcmd = m.group(1)
                if subcmd and not subcmd.startswith("-") and subcmd not in commands_in_code and subcmd not in ("run", "exec", "python", "node"):
                    search_note = ", ".join(searched[:5])
                    findings.append(
                        DriftFinding(
                            kind="missing_cli_command",
                            path=subcmd,
                            severity="high",
                            message=(
                                f"CLI subcommand '{subcmd}' documented at {rel_doc}:{lineno} "
                                f"does not exist in code (searched in {search_note})"
                            ),
                            docs_file=rel_doc,
                            docs_line=lineno,
                        )
                    )

    return findings


def check_call_form_claims(docs_root: Path, project_root: Path) -> list[DriftFinding]:
    """Verify that methods called in README call-form exist as public symbols."""
    from agents.lexi import _is_historical_doc
    findings: list[DriftFinding] = []

    # Collect public symbols and methods in code
    public_symbols: set[str] = set()
    for src in list_sources(project_root):
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if src.suffix == ".py":
            try:
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        public_symbols.add(node.name)
                    elif isinstance(node, ast.Assign):
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                public_symbols.add(target.id)
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            public_symbols.add(alias.asname or alias.name.split(".")[-1])
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            public_symbols.add(alias.asname or alias.name)
            except Exception:
                pass
        for name in public_names(src):
            public_symbols.add(name)
        # Class methods in TS/JS / Python
        for m in re.finditer(r"(?:^\s*|[{;]\s*)(?:async\s+)?([a-zA-Z0-9_]+)\s*\([^)]*\)\s*[{:]", text, re.MULTILINE):
            public_symbols.add(m.group(1))

    # Pattern: obj.method_name(...) - identifiers must start with a letter or underscore
    call_form_re = re.compile(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")

    for doc_path in list_docs(docs_root):
        if not any(doc_path.name.lower().startswith(pfx) for pfx in ("readme", "install")):
            continue
        if _is_historical_doc(doc_path, docs_root):
            continue
        try:
            doc_text = doc_path.read_text(encoding="utf-8", errors="replace")
            lines = doc_text.splitlines()
        except OSError:
            continue

        rel_doc = str(doc_path.relative_to(docs_root)).replace("\\", "/") if doc_path.is_relative_to(docs_root) else doc_path.name

        for lineno, line in enumerate(lines, start=1):
            for m in call_form_re.finditer(line):
                obj_name, method_name = m.group(1), m.group(2)
                if obj_name in COMMON_CALL_OBJECTS:
                    continue
                if method_name.startswith("_"):
                    continue
                if method_name in BUILTIN_METHOD_NAMES:
                    continue
                # If illustrative code defined inside doc example, skip
                if _is_doc_code_example_ident(method_name, doc_text) or _is_doc_code_example_ident(obj_name, doc_text):
                    continue

                if method_name not in public_symbols:
                    findings.append(
                        DriftFinding(
                            kind="missing_code",
                            path=method_name,
                            severity="high",
                            message=(
                                f"method '{obj_name}.{method_name}()' called at {rel_doc}:{lineno} "
                                f"does not exist as a public symbol in the codebase"
                            ),
                            docs_file=rel_doc,
                            docs_line=lineno,
                        )
                    )

    return findings


def check_env_vars(docs_root: Path, project_root: Path, config: dict[str, Any] | None = None) -> list[DriftFinding]:
    """Verify env variables: documented vs code reads."""
    findings: list[DriftFinding] = []
    ignore = set((config or {}).get("lexi", {}).get("ignore_identifiers") or [])

    # 1. Collect env vars read in code
    code_env_vars: set[str] = set()
    secret_code_vars: set[str] = set()

    for src in list_sources(project_root):
        try:
            text = src.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # os.environ['VAR'], os.environ.get('VAR'), os.getenv('VAR')
        for m in re.finditer(r"(?:os\.environ(?:\[|\.get\()|os\.getenv\()\s*['\"]([A-Z0-9_]+)['\"]", text):
            var = m.group(1)
            code_env_vars.add(var)
            if any(frag in var for frag in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                secret_code_vars.add(var)

        # process.env.VAR, process.env['VAR']
        for m in re.finditer(r"process\.env(?:\.([A-Z0-9_]+)|\[\s*['\"]([A-Z0-9_]+)['\"]\s*\])", text):
            var = m.group(1) or m.group(2)
            code_env_vars.add(var)
            if any(frag in var for frag in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
                secret_code_vars.add(var)

    # 2. Collect documented env vars
    doc_env_vars: dict[str, tuple[str, int]] = {}
    all_doc_text = []

    for doc_path in list_docs(docs_root):
        try:
            lines = doc_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel_doc = str(doc_path.relative_to(docs_root)).replace("\\", "/") if doc_path.is_relative_to(docs_root) else doc_path.name

        for lineno, line in enumerate(lines, start=1):
            all_doc_text.append(line)
            # Find env vars: e.g. export FOO_BAR=..., `FOO_BAR`
            for m in re.finditer(r"\b([A-Z][A-Z0-9_]{3,})\b", line):
                var = m.group(1)
                if var in NON_ENV_TOKENS or var in ignore:
                    continue
                if "export " in line or "=" in line or "env" in line.lower() or "variable" in line.lower() or "token" in line.lower() or "key" in line.lower():
                    if var not in doc_env_vars:
                        doc_env_vars[var] = (rel_doc, lineno)

    # Documented env var not read in code -> P2
    for var, (doc_file, doc_line) in doc_env_vars.items():
        if var in ignore:
            continue
        if var not in code_env_vars:
            findings.append(
                DriftFinding(
                    kind="undocumented_env",
                    path=var,
                    severity="medium",
                    message=f"documented env variable '{var}' at {doc_file}:{doc_line} is not read anywhere in code",
                    docs_file=doc_file,
                    docs_line=doc_line,
                )
            )

    # Required secret env var read in code but undocumented -> P2
    joined_docs = "\n".join(all_doc_text)
    for var in secret_code_vars:
        if var not in joined_docs:
            findings.append(
                DriftFinding(
                    kind="undocumented_secret",
                    path=var,
                    severity="medium",
                    message=f"code reads required secret environment variable '{var}' but it is not documented in README or docs",
                )
            )

    return findings


def check_install_instructions(docs_root: Path, project_root: Path) -> list[DriftFinding]:
    """Verify that documented install command matches manifests."""
    findings: list[DriftFinding] = []

    pyproject_path = project_root / "pyproject.toml"
    package_json_path = project_root / "package.json"

    expected_py_name = ""
    if pyproject_path.is_file():
        try:
            data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            expected_py_name = str(data.get("project", {}).get("name") or "").strip()
        except Exception:
            pass

    expected_node_name = ""
    if package_json_path.is_file():
        try:
            data = json.loads(package_json_path.read_text(encoding="utf-8"))
            expected_node_name = str(data.get("name") or "").strip()
        except Exception:
            pass

    for doc_path in list_docs(docs_root):
        try:
            lines = doc_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel_doc = str(doc_path.relative_to(docs_root)).replace("\\", "/") if doc_path.is_relative_to(docs_root) else doc_path.name

        for lineno, line in enumerate(lines, start=1):
            # Check pip install <pkg>
            m_pip = re.search(r"pip\s+install\s+([a-zA-Z0-9_.-]+)", line)
            if m_pip and expected_py_name:
                pkg = m_pip.group(1).lower().replace("_", "-")
                norm_exp = expected_py_name.lower().replace("_", "-")
                if pkg not in (norm_exp, ".", "-e", "-r"):
                    findings.append(
                        DriftFinding(
                            kind="install_instruction_mismatch",
                            path=m_pip.group(1),
                            severity="medium",
                            message=(
                                f"install instruction at {rel_doc}:{lineno} claims 'pip install {m_pip.group(1)}' "
                                f"but project name in pyproject.toml is '{expected_py_name}'"
                            ),
                            docs_file=rel_doc,
                            docs_line=lineno,
                        )
                    )

            # Check npm install <pkg>
            m_npm = re.search(r"(?:npm\s+(?:i|install)|yarn\s+add|pnpm\s+add)\s+([a-zA-Z0-9_.-]+)", line)
            if m_npm and expected_node_name:
                pkg = m_npm.group(1).lower()
                norm_exp = expected_node_name.lower()
                if pkg != norm_exp and not pkg.startswith("-"):
                    findings.append(
                        DriftFinding(
                            kind="install_instruction_mismatch",
                            path=m_npm.group(1),
                            severity="medium",
                            message=(
                                f"install instruction at {rel_doc}:{lineno} claims install package '{m_npm.group(1)}' "
                                f"but project name in package.json is '{expected_node_name}'"
                            ),
                            docs_file=rel_doc,
                            docs_line=lineno,
                        )
                    )

    return findings

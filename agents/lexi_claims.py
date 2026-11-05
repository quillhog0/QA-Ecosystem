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

from agents.inventory import CODE_SKIP, list_claim_docs, list_sources, public_names
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
    "node", "npx", "bun", "deno", "python", "python3", "brew", "cargo", "go", "pip3", "gem",
})

DOT_EXTENSIONS = frozenset({
    "js", "ts", "mjs", "cjs", "jsx", "tsx", "py", "json", "yaml", "yml", "md", "html", "css",
    "io", "org", "com", "net", "sh", "txt", "wasm", "map", "sql", "db",
})

DOT_PACKAGES = frozenset({
    "sql.js", "chart.js", "node.js", "next.js", "three.js", "vue.js", "moment.js",
    "highlight.js", "socket.io", "day.js", "pdf.js", "math.js", "fabric.js", "video.js",
    "anime.js", "d3.js", "p5.js", "require.js",
})


def is_external_tool_invocation(line: str) -> bool:
    """True if line represents an invocation of a third-party tool."""
    clean = line.strip()
    m = re.search(
        r"(?:^\s*[$>#]\s*|`|(?:^|\s)(?:run|exec|install|via|using|use|start)\s+|^\s*)([a-zA-Z0-9_.-]+)",
        clean,
        re.IGNORECASE,
    )
    if m and m.group(1).lower() in EXTERNAL_TOOLS:
        return True
    for seg in re.split(r"[|;&]", clean):
        seg_clean = seg.strip().lstrip("$># ").strip("`\"' ")
        tokens = [t.strip("`\"' ") for t in seg_clean.split() if t.strip("`\"' ")]
        while tokens and tokens[0].lower() in {"run", "exec", "install", "via", "using", "use", "start", "to", "with"}:
            tokens = tokens[1:]
        if tokens and tokens[0].lower() in EXTERNAL_TOOLS:
            return True
    return False

_COMMIT_HASH_RE = re.compile(r"[0-9a-f]{7,40}")


def is_commit_hash(token: str) -> bool:
    """True for a git commit hash (7-40 lowercase hex chars with a digit).

    The digit requirement keeps real hex-only words (``facade``, ``deadbeef``
    style names are still code-shaped elsewhere) from being swallowed; every
    abbreviated or full hash git prints carries at least one digit in practice.
    """
    return bool(_COMMIT_HASH_RE.fullmatch(token)) and any(ch.isdigit() for ch in token)


# Repository data files: a name they contain is real project content. Config
# files (YAML/TOML/INI, manifests, lockfiles) are deliberately not data: a key
# only counts there if code reads it.
_DATA_FILE_EXT = frozenset({".json", ".jsonl", ".ndjson", ".csv"})
_NON_DATA_JSON = frozenset({
    "package.json", "package-lock.json", "tsconfig.json", "jsconfig.json",
    "composer.json", "composer.lock", ".eslintrc.json", "deno.json",
})


def _data_file_text(project_root: Path, limit_bytes: int = 5_000_000) -> str:
    """Concatenated text of the project's data files (JSON fixtures, seeds)."""
    chunks: list[str] = []
    total = 0
    root = Path(project_root)
    if not root.is_dir():
        return ""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _DATA_FILE_EXT:
            continue
        if path.name.lower() in _NON_DATA_JSON or path.name.lower().startswith("tsconfig"):
            continue
        rel = path.relative_to(root)
        if any(part in CODE_SKIP or part.startswith(".") for part in rel.parts):
            continue
        try:
            size = path.stat().st_size
            if size > 1_000_000 or total + size > limit_bytes:
                continue
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        total += size
    return "\n".join(chunks)


# Env-var candidates in docs come only from an explicit env context.
_ENV_NAME = r"([A-Z][A-Z0-9_]{1,})"
_DOC_ENV_PATTERNS = (
    re.compile(r"process\.env(?:\.|\[\s*['\"])" + _ENV_NAME),
    re.compile(r"os\.environ(?:\[|\.get\()\s*['\"]" + _ENV_NAME),
    re.compile(r"\bgetenv\(\s*['\"]" + _ENV_NAME),
    re.compile(r"\$\{?" + _ENV_NAME + r"\b"),
    re.compile(r"\bexport\s+" + _ENV_NAME + r"="),
    re.compile(r"\benv(?:ironment)?[ -]var(?:iable)?s?\s+`?" + _ENV_NAME + r"\b", re.IGNORECASE),
    re.compile(r"`?\b" + _ENV_NAME + r"\b`?\s+env(?:ironment)?[ -]var(?:iable)?\b", re.IGNORECASE),
)
_ENV_EXAMPLE_NAMES = (".env.example", ".env.sample", ".env.template", "env.example")
_ENV_EXAMPLE_KEY_RE = re.compile(r"^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", re.MULTILINE)


def doc_env_candidates(line: str) -> list[str]:
    """Env-var names a docs line names in an env context (in order, unique)."""
    found: list[str] = []
    for pattern in _DOC_ENV_PATTERNS:
        for m in pattern.finditer(line):
            name = m.group(1)
            if not name.isupper() or name in found:
                continue
            found.append(name)
    return found


def env_example_keys(project_root: Path) -> dict[str, tuple[str, int]]:
    """Keys declared in a committed env template (never the real ``.env``)."""
    keys: dict[str, tuple[str, int]] = {}
    for name in _ENV_EXAMPLE_NAMES:
        path = Path(project_root) / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            m = _ENV_EXAMPLE_KEY_RE.match(line)
            if m and m.group(1) not in keys:
                keys[m.group(1)] = (name, lineno)
    return keys


def _root_readmes(project_root: Path) -> list[Path]:
    """README-style files at the project root (may live outside docs_root)."""
    root = Path(project_root)
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir()
        if p.is_file() and p.name.lower().startswith("readme") and p.suffix.lower() in (".md", ".rst", ".txt", "")
    )


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

    for doc_path in list_claim_docs(docs_root):
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
            if is_external_tool_invocation(stripped):
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
            m = re.search(r"(?:^\s*[$>]\s*|`|^)\s*([a-zA-Z0-9_-]+)\s+([a-zA-Z0-9_-]+)\s+--", stripped)
            if m:
                tool_name = m.group(1).lower()
                subcmd = m.group(2)
                if tool_name in EXTERNAL_TOOLS or tool_name in ("run", "exec", "python", "node"):
                    continue
                if (
                    subcmd
                    and not subcmd.startswith("-")
                    and subcmd not in commands_in_code
                    and subcmd not in ("run", "exec", "python", "node")
                    and subcmd not in EXTERNAL_TOOLS
                ):
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

    for doc_path in list_claim_docs(docs_root):
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
            line_clean = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line)
            for m in call_form_re.finditer(line_clean):
                obj_name, method_name = m.group(1), m.group(2)
                if obj_name in COMMON_CALL_OBJECTS:
                    continue
                if method_name.startswith("_"):
                    continue
                if method_name in BUILTIN_METHOD_NAMES:
                    continue
                if f"{obj_name}.{method_name}".lower() in DOT_PACKAGES or method_name.lower() in DOT_EXTENSIONS:
                    continue
                if is_commit_hash(method_name) or is_commit_hash(obj_name):
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

    # 2. Collect documented env vars - only from an explicit env context
    doc_env_vars: dict[str, tuple[str, int]] = {}
    all_doc_text: list[str] = []

    doc_paths = list(list_claim_docs(docs_root))
    seen_paths = {p.resolve() for p in doc_paths}
    # The README usually lives at the project root even when docs_root is a
    # ``docs/`` directory: its mentions document env vars too.
    for readme in _root_readmes(project_root):
        if readme.resolve() not in seen_paths:
            doc_paths.append(readme)
            seen_paths.add(readme.resolve())

    for doc_path in doc_paths:
        try:
            lines = doc_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if doc_path.is_relative_to(docs_root):
            rel_doc = str(doc_path.relative_to(docs_root)).replace("\\", "/")
        else:
            rel_doc = doc_path.name

        for lineno, line in enumerate(lines, start=1):
            all_doc_text.append(line)
            for var in doc_env_candidates(line):
                if var in NON_ENV_TOKENS or var in ignore or is_commit_hash(var):
                    continue
                if var not in doc_env_vars:
                    doc_env_vars[var] = (rel_doc, lineno)

    example_keys = env_example_keys(project_root)
    for var, location in example_keys.items():
        all_doc_text.append(var)
        if var in NON_ENV_TOKENS or var in ignore:
            continue
        doc_env_vars.setdefault(var, location)

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
    for var in sorted(secret_code_vars):
        # A mention in any form documents it: `BOT_TOKEN=...` in a code block,
        # a table cell, a `$BOT_TOKEN` reference or an env template key.
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(var)}(?![A-Za-z0-9_])", joined_docs):
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

    for doc_path in list_claim_docs(docs_root):
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

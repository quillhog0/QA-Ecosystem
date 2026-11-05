"""Language and toolchain detection for Python and TypeScript/JavaScript."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from core.runner import sanitized_env
PYTHON_MANIFESTS = ('pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt', 'requirements-dev.txt', 'requirements-test.txt')
TS_MANIFESTS = ('package.json',)

def detect_languages(project_root: Path) -> list[str]:
    """Detect languages present in the project: 'python', 'ts', or both."""
    if not project_root or not project_root.is_dir():
        return []
    languages: list[str] = []
    has_python = False
    for manifest in PYTHON_MANIFESTS:
        if (project_root / manifest).is_file():
            has_python = True
            break
    if not has_python:
        for f in project_root.glob('requirements*.txt'):
            if f.is_file():
                has_python = True
                break
    if has_python:
        languages.append('python')
    has_ts = (project_root / 'package.json').is_file()
    if has_ts:
        languages.append('ts')
    selection = os.environ.get('QA_LANGUAGES', 'auto').strip().lower() or 'auto'
    if selection == 'python':
        languages = [lang for lang in languages if lang == 'python']
    elif selection == 'ts':
        languages = [lang for lang in languages if lang == 'ts']
    return languages

def detect_package_manager(project_root: Path) -> tuple[str, list[str], str | None]:
    """Detect package manager and install command from lockfile.

    Returns (pm_name, install_cmd, optional_warning).
    """
    if (project_root / 'package-lock.json').is_file():
        return ('npm', ['npm', 'ci'], None)
    if (project_root / 'pnpm-lock.yaml').is_file():
        return ('pnpm', ['pnpm', 'install', '--frozen-lockfile'], None)
    if (project_root / 'yarn.lock').is_file():
        return ('yarn', ['yarn', 'install', '--frozen-lockfile'], None)
    return ('npm', ['npm', 'install'], 'no lockfile found (package-lock.json, pnpm-lock.yaml, yarn.lock); falling back to npm install')

def _parse_semver(v: str) -> tuple[int, int, int]:
    clean = re.sub('^[^\\d]*', '', str(v).strip())
    parts = clean.split('.')
    major = int(parts[0]) if parts and parts[0].isdigit() else 0
    minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    return (major, minor, patch)

def _compare_semver(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    if a < b:
        return -1
    if a > b:
        return 1
    return 0

def _satisfies_single_constraint(version: tuple[int, int, int], constraint: str) -> bool:
    c = constraint.strip()
    if not c or c == '*' or c.lower() == 'x':
        return True
    m_wild = re.match('^(\\d+)(?:\\.([xX*]|\\d+))?(?:\\.([xX*]|\\d+))?$', c)
    if m_wild:
        maj = int(m_wild.group(1))
        min_str = m_wild.group(2)
        if min_str is None or min_str in ('x', 'X', '*'):
            return version[0] == maj
        minor = int(min_str)
        patch_str = m_wild.group(3)
        if patch_str is None or patch_str in ('x', 'X', '*'):
            return version[0] == maj and version[1] == minor
        return version == (maj, minor, int(patch_str))
    if c.startswith('^'):
        target = _parse_semver(c[1:])
        if target[0] > 0:
            return version[0] == target[0] and _compare_semver(version, target) >= 0
        elif target[1] > 0:
            return version[0] == 0 and version[1] == target[1] and (_compare_semver(version, target) >= 0)
        else:
            return version == target
    if c.startswith('~'):
        target = _parse_semver(c[1:])
        return version[0] == target[0] and version[1] == target[1] and (_compare_semver(version, target) >= 0)
    m_op = re.match('^(>=|<=|>|<|==|=)\\s*(.*)$', c)
    if m_op:
        op = m_op.group(1)
        target = _parse_semver(m_op.group(2))
        comp = _compare_semver(version, target)
        if op == '>=':
            return comp >= 0
        if op == '<=':
            return comp <= 0
        if op == '>':
            return comp > 0
        if op == '<':
            return comp < 0
        if op in ('=', '=='):
            return comp == 0
    target = _parse_semver(c)
    return version == target

def semver_satisfies(version_str: str, range_str: str) -> bool:
    """Check if version_str satisfies range_str (e.g. '>=18.0.0 <22.0.0' or '^20')."""
    v = _parse_semver(version_str)
    or_clauses = range_str.split('||')
    for clause in or_clauses:
        clause = clause.strip()
        if not clause:
            continue
        and_parts = re.findall('(?:>=|<=|>|<|==|=|~|\\^)?\\s*[^\\s]+', clause)
        if not and_parts:
            continue
        if all((_satisfies_single_constraint(v, part) for part in and_parts)):
            return True
    return False

def get_available_node_version() -> str | None:
    """Return available Node.js version string (e.g. 'v20.18.0'), or None."""
    node_bin = shutil.which('node')
    if not node_bin:
        return None
    try:
        proc = subprocess.run([node_bin, '--version'], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return None

def check_node_engine(project_root: Path, current_node_version: str | None=None) -> tuple[bool, str | None]:
    """Check Node.js engine compatibility against package.json and .nvmrc.

    Returns (is_ok, mismatch_error_message).
    """
    pkg_json_path = project_root / 'package.json'
    nvmrc_path = project_root / '.nvmrc'
    required_range = ''
    source = ''
    if pkg_json_path.is_file():
        try:
            data = json.loads(pkg_json_path.read_text(encoding='utf-8'))
            engines = data.get('engines') or {}
            if isinstance(engines, dict) and engines.get('node'):
                required_range = str(engines['node']).strip()
                source = 'package.json engines.node'
        except Exception:
            pass
    if not required_range and nvmrc_path.is_file():
        try:
            line = nvmrc_path.read_text(encoding='utf-8').strip().splitlines()[0].strip()
            if line:
                required_range = line
                source = '.nvmrc'
        except Exception:
            pass
    if not required_range:
        return (True, None)
    node_version = current_node_version or get_available_node_version()
    if not node_version:
        return (False, f"Node.js engine mismatch: project requires {source} '{required_range}', but node executable is not available in environment")
    if required_range.lower().startswith('lts'):
        curr_maj = _parse_semver(node_version)[0]
        if curr_maj in (18, 20, 22, 24):
            return (True, None)
        return (False, f"Node.js engine mismatch: project requires {source} '{required_range}', but available node is {node_version}")
    if not semver_satisfies(node_version, required_range):
        return (False, f"Node.js engine mismatch: project requires {source} '{required_range}', but available node is {node_version}")
    return (True, None)

def detect_ts_runner(project_root: Path) -> dict[str, Any]:
    """Detect test runner from package.json devDependencies and scripts."""
    pkg_json_path = project_root / 'package.json'
    if not pkg_json_path.is_file():
        return {'runner': 'npm_test', 'command': ['npm', 'test']}
    try:
        data = json.loads(pkg_json_path.read_text(encoding='utf-8'))
    except Exception:
        return {'runner': 'npm_test', 'command': ['npm', 'test']}
    dev_deps = data.get('devDependencies') or {}
    deps = data.get('dependencies') or {}
    scripts = data.get('scripts') or {}
    all_deps = {**deps, **dev_deps}
    test_script = str(scripts.get('test') or '')
    if 'vitest' in all_deps or 'vitest' in test_script:
        return {'runner': 'vitest', 'has_local_bin': (project_root / 'node_modules' / '.bin' / 'vitest').exists(), 'direct': True}
    if 'jest' in all_deps or 'jest' in test_script:
        return {'runner': 'jest', 'has_local_bin': (project_root / 'node_modules' / '.bin' / 'jest').exists(), 'direct': True}
    if 'mocha' in all_deps or 'mocha' in test_script:
        return {'runner': 'mocha', 'has_local_bin': (project_root / 'node_modules' / '.bin' / 'mocha').exists(), 'direct': True}
    if 'node --test' in test_script and 'node:test' in test_script:
        return {'runner': 'node_test', 'has_local_bin': False, 'direct': True}
    return {'runner': 'npm_test', 'has_local_bin': False, 'direct': False}

def typecheck_project(project_root: Path, node_bin: str | None=None) -> list[dict[str, Any]]:
    """Run tsc --noEmit if tsconfig.json exists. Return list of type errors."""
    tsconfig = project_root / 'tsconfig.json'
    if not tsconfig.is_file():
        return []
    local_tsc = project_root / 'node_modules' / '.bin' / 'tsc'
    cmd: list[str] = []
    if local_tsc.is_file():
        cmd = [str(local_tsc), '--noEmit']
    else:
        tsc_bin = shutil.which('tsc')
        if tsc_bin:
            cmd = [tsc_bin, '--noEmit']
        else:
            npx_bin = shutil.which('npx')
            if npx_bin:
                cmd = [npx_bin, '--no-install', 'tsc', '--noEmit']
            else:
                return []
    env = sanitized_env(allow_network=False)
    if node_bin:
        env['PATH'] = str(Path(node_bin).parent) + os.pathsep + env.get('PATH', '')
    try:
        proc = subprocess.run(cmd, cwd=str(project_root), env=env, capture_output=True, text=True, timeout=120, check=False)
    except Exception as exc:
        return []
    if proc.returncode == 0:
        return []
    errors: list[dict[str, Any]] = []
    combined = (proc.stdout or '') + '\n' + (proc.stderr or '')
    error_re1 = re.compile('^(?P<file>[^(:]+?)\\((?P<line>\\d+),(?P<col>\\d+)\\):\\s*error\\s*(?P<code>TS\\d+):\\s*(?P<msg>.*)$')
    error_re2 = re.compile('^(?P<file>[^:]+?):(?P<line>\\d+):(?P<col>\\d+)\\s*-\\s*error\\s*(?P<code>TS\\d+):\\s*(?P<msg>.*)$')
    for line in combined.splitlines():
        line = line.strip()
        m = error_re1.match(line) or error_re2.match(line)
        if m:
            file_rel = m.group('file').strip().replace('\\', '/')
            lineno = int(m.group('line'))
            msg = f"{m.group('code')}: {m.group('msg').strip()}"
            errors.append({'file': file_rel, 'line': lineno, 'evidence': f'{file_rel}:{lineno} {msg}', 'message': msg})
    return errors

PINNED_TS_SHA256 = "3ae902c92cc44dace175c0e69e13a4b0899f6983c6121d76b9ab8dd5795e7675"


def ensure_ts_tooling(project_root: Path | None = None) -> Path | None:
    """Ensure pinned TypeScript compiler API is cached in ~/.cache/qa-ecosystem/tooling/.

    SECURITY: Only QA-controlled sources (QA_TOOLING_DIR or ~/.cache/qa-ecosystem/tooling
    populated from QA's own pinned vendor artifact) are permitted. The tested project's
    node_modules or arbitrary external paths must NEVER supply code executed by QA tooling.
    """
    override = os.environ.get("QA_TOOLING_DIR")
    if override:
        tooling_dir = Path(override).expanduser()
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
        tooling_dir = base / "qa-ecosystem" / "tooling"
    tooling_dir.mkdir(parents=True, exist_ok=True)
    target_ts = tooling_dir / "typescript.js"
    sha_file = tooling_dir / "typescript.sha256"

    if target_ts.is_file() and target_ts.stat().st_size > 1000:
        actual_sha = hashlib.sha256(target_ts.read_bytes()).hexdigest()
        expected_sha = sha_file.read_text(encoding="utf-8").strip() if sha_file.is_file() else PINNED_TS_SHA256
        if actual_sha == expected_sha or actual_sha == PINNED_TS_SHA256:
            return tooling_dir

    vendor_ts = Path(__file__).resolve().parents[1] / "agents" / "vendor" / "typescript.js"
    if vendor_ts.is_file():
        actual_sha = hashlib.sha256(vendor_ts.read_bytes()).hexdigest()
        if actual_sha == PINNED_TS_SHA256:
            shutil.copy2(vendor_ts, target_ts)
            sha_file.write_text(actual_sha, encoding="utf-8")
            return tooling_dir

    return None
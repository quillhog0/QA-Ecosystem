# Changelog

## 0.2.1 (2026-10-29)

### Fixed
- Fewer false vetoes on real projects:
  - Sable allows file deletion in tests when the Bubblewrap sandbox is active, because it can only touch the isolated copy. Without the sandbox, deletion still blocks.
  - Riley counts unittest-style assertions (self.assert*, self.fail).
  - Python files that start with a UTF-8 byte order mark now parse.
  - Lexi treats a plain backticked doc word that is missing from code as a warning. It still vetoes code-shaped names (snake_case, UPPER_SNAKE, camelCase), call forms and names used in claims. Plain words veto only with --strict.
  - Lexi also looks for documented names in JS/TS, HTML, shell, SQL and config keys.

## 0.2.0 (2026-10-29)

### Added
- GitHub Action (action.yml) that runs the QA gate in any workflow, fails the job on VETO and writes the report to the job summary.
- Riley flags skipped or xfail tests, assertions swallowed by try/except, and tests with only trivial assertions as weak; a claim covered only by weak tests is not counted as covered and leads to VETO.
- cheating_agent benchmark fixture (skip, swallowed assertion, trivial assertions).
- Bubblewrap (`bwrap`) OS-level sandbox for isolated test execution (`core/sandbox.py`).
- Linux namespace isolation (`--unshare-all`, `--die-with-parent`) disabling network access and restricting filesystem access.
- Read-only system binds (`/usr`, `/lib`, `/lib64`, `/bin`, `/etc`, python virtualenv) and read-only `work_dir` mount (protecting `_sable_guard.py`), with read-write access restricted to the isolated project copy, `pytest_cache`, `pytest_tmp`, `/home/sandbox` (`work_dir/home`), and a private `/tmp`.
- Validation of `extra_ro_binds` rejecting any path equal to or an ancestor of host `$HOME` or `/`, and `run_pytest` path resolution with `os.pathsep` rejection.
- Strict environment allowlist in `sanitized_env` (`PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TEMP`, `TMP`, `SYSTEMROOT`, `COMSPEC`), blocking host `PYTEST_ADDOPTS`, `PYTHONSTARTUP`, `PYTHONPATH`, and `PYTHON*`/`PYTEST*` wildcards.
- Sandbox mode setting (`sandbox.mode: auto | required | off`) configurable via system config or CLI flag `--sandbox`, raising `ValueError` on unknown/misspelled modes (fail closed).
- VETO enforcement when `sandbox.mode: required` is requested but Bubblewrap is unavailable, and explicit `sandbox: unavailable (ran unsandboxed)` reporting when `auto` falls back.
- Comprehensive unit tests and integration tests for sandbox constraints.
- Custom AST-based mutation testing engine (`core/mutation.py`) with comparison, arithmetic, boolean constant, return value, and boolean operator mutators.
- Optional Riley audit phase for mutation scoring (`mutation.enabled`, `mutation.min_score`, `mutation.max_mutants`) via system config or CLI flags `--mutation`, `--mutation-min-score`.
- Mutation testing Markdown report section detailing mutation score and surviving mutants.
- Added `strong_tests` and `weak_tests` benchmark fixtures.

### Fixed
- Mutation audit counted a mutant as killed when the test run crashed before any test executed; such runs are now errors.
- Sandbox now binds a project venv whose interpreter is a symlink to the system Python.
- Mutation audit uses the project venv prepared for the run.
- Report output is English only.

### Changed
- httpx is an optional dependency (extra "llm"); it is only needed when an LLM is enabled.

## 0.1.0 (2026-10-28)

First public release.

- Six roles with a fixed test cycle and structured handoffs.
- Isolated working-tree copy, including uncommitted changes.
- Runtime network guard loaded as a separate pytest plugin.
- Static scan of tests, conftest files and project sources.
- Weak-test detection and scored claim-to-test coverage.
- An LLM can tighten a verdict but never override a deterministic VETO.
- Benchmark with 12 fixtures.

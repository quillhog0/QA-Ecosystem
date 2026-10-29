# Changelog

## [Unreleased]

### Added
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

## 0.1.0 (2026-10-28)

First public release.

- Six roles with a fixed test cycle and structured handoffs.
- Isolated working-tree copy, including uncommitted changes.
- Runtime network guard loaded as a separate pytest plugin.
- Static scan of tests, conftest files and project sources.
- Weak-test detection and scored claim-to-test coverage.
- An LLM can tighten a verdict but never override a deterministic VETO.
- Benchmark with 12 fixtures.

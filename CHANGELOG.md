# Changelog

## 0.3.2 (2026-10-30)

### Added
- Phase timings (isolation, install, pytest, mutation) in the report.
- Parallel mutation audit: `mutation.workers` (default min(4, CPU count), cap 16) and `--mutation-workers`. Each worker uses its own tree copy; per-mutant results, counts and survivor order are identical to a sequential run. Self-test with mutation is about 3x faster.
- Venv cache keyed by a hash of the requirement file contents and the interpreter, stored under the operator's cache directory (`QA_VENV_CACHE_DIR` or `~/.cache/qa-ecosystem/venvs`), never in the tested project. Only a successful install is cached.

### Fixed
- Lexi no longer reports `missing_code` for keyword-argument and parameter names, or for git branch names read from the live project's `.git` (plain file reads, the isolated copy still has no `.git`).

### Tests
- Kill-tests for new survivors; self-test mutation score 96.7% (29 killed, 1 equivalent survivor).

## 0.3.1 (2026-10-30)

### Tests
- Kill-tests for 13 of the 15 surviving mutants reported at 0.3.0; self-test mutation score 48.3% -> 93.1% (27 killed, 2 survived, 1 error). The two remaining survivors (agents/aria.py:48, core/mutation.py:385) are equivalent mutants.

## 0.3.0 (2026-10-30)

### Added
- SARIF 2.1.0 output: `--sarif PATH` writes a document of the run's findings (core/sarif.py). The GitHub Action gains a `sarif` input (true/false) and a `sarif` output with the document path. Stable rule ids: LEXI001-005, LEXI999, SABLE001-009, RILEY001, RILEY002, RILEY999, NOVA001, NOVA002, VESPER001, VESPER002; locations are project-relative under `%SRCROOT%`.
- VESPER002 ("run vetoed before execution"): a non-CLEAR run with no other SARIF result, such as a sandbox-required veto, now emits one error result instead of an empty document.

### Fixed
- Malformed YAML from an LLM no longer aborts the run; `parse_yaml_block` returns an empty mapping and the heuristic result is kept.
- An invalid `sandbox.mode` is rejected before the isolated copy is created, so it no longer leaks a copy under the temp directory.
- `mutation.max_mutants` of 0 (or negative) no longer reports CLEAR with score 1.0 when candidates exist; mutation is reported as skipped with a reason and has no verdict impact. Negative values no longer crash.
- Action: the `sarif` output is set only when the file was written, and a missing report no longer masks the real exit code.
- SABLE007 short description now reads "Blocked call in the isolated copy" (the scan covers all files in the copy).

### Changed
- CI action smoke test runs with `sarif: "true"` and checks the file exists.
- README documents `--config`, `--no-dry-run`, the remaining config keys and two more known limits; the mutation self-test figures were re-measured.
- Removed dead code and unused imports; stronger assertions in two tests; author handle is quillhog0.

## 0.2.2 (2026-10-30)

### Fixed
- Lexi no longer reports `missing_code` for a docs term that exists as an exact string literal in project code (tests and docs excluded).
- Lexi no longer reports `missing_code` for a docs term that names a declared dependency (requirements*.txt, pyproject.toml dependencies and optional-dependencies, PEP 503 name normalization).
- Self-test passes again: the simulated pytest timeout in tests/unit/test_runner.py is allowlisted for Sable (it raises subprocess.TimeoutExpired without spawning a process).

### Security
- The project under test can no longer suppress Lexi findings via its own config.yaml. Its keys are no longer ignored, and config file keys no longer count as evidence that a documented name exists; only names read by code do.

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

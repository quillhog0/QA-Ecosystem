# Changelog

## 0.4.3 (2026-11-05)

### Fixed
- **BLOCKED_ENV "missing TypeScript tooling" on every fresh runner** (regression in 0.4.2): the pinned QA artifact `agents/vendor/typescript.js` was git-ignored and therefore absent from the released action, so `ensure_ts_tooling` could never populate `~/.cache/qa-ecosystem/tooling` and every TypeScript project stopped with `BLOCKED_ENV` before any test ran. The pinned file (SHA-256 `3ae902c9…e7675`, Apache-2.0, see `agents/vendor/LICENSE`) is now tracked.
- **Summary language fallback**: when a run produced no execution data (e.g. `BLOCKED_ENV`), the summary reported `Languages: python` regardless of the project or `--languages`. It now reports the detected/selected languages of the tested tree.

### Added
- Regression tests `tests/unit/test_vendor_ts_tooling.py`: vendor artifact is tracked and matches the pin; `ensure_ts_tooling` works with an empty HOME and no `QA_TOOLING_DIR`; summary language fallback.

## 0.4.2 (2026-11-05)

### Fixed
- **Determinism across test plan design and review**:
  - Aria test plan generation for pure TypeScript/JavaScript projects: discover JS/TS sources and test function declarations (`it(...)` / `test(...)`) so baseline (S1) and negative-path (S3) scenarios are generated deterministically on attempt 1, even when documentation lacks explicit error keywords.
  - Two-pass helper assertion discovery in `ts_analyzer.js` with alphabetical file iteration sorting, eliminating ordering variance between test and helper files.
  - Refined swallowed-catch detection in `ts_analyzer.js`: try/catch blocks wrapping external process execution are not flagged as weak tests when valid assertions exist outside the try block.
  - Deterministic gap reporting: Riley gap lists and root cause explanations sort weak test identifiers before formatting.
- **Security hardening for TypeScript compiler tooling**:
  - Reverted project-supplied compiler fallbacks in `ensure_ts_tooling` and `resolveTypeScript`: the tested project's `node_modules` (local or parent) and cached project `node_modules` trees are never executed or cached. Only QA-controlled sources (`QA_TOOLING_DIR` or `~/.cache/qa-ecosystem/tooling` populated from QA's pinned artifact verified with SHA-256) are permitted.
  - When TypeScript tooling is missing or unverified for a TypeScript project, the pipeline stops with `BLOCKED_ENV` rather than falling back to project code.
- **Lexi false positives on README claims**:
  - Third-party CLI tools (`npm`, `npx`, `pip`, `git`, `docker`, `node`): subcommands (such as `ci`) and flags (such as `--omit=dev`) are recognized as third-party invocations and excluded from project CLI checks.
  - Dot-delimited package names (`sql.js`, `chart.js`, `node.js`, `next.js`) and markdown links (`[sql.js](...)`) are excluded from method call-form verification.

### Added
- Benchmark fixtures:
  - `ts_plan_determinism` & `py_plan_determinism`: verify identical plan acceptance and verdict across clean and warm cache runs.
  - `ts_third_party_cli_fp` & `py_third_party_cli_fp`: verify third-party CLI flags/subcommands and dotted package names produce CLEAR.
  - `ts_malicious_node_modules`: verify project `node_modules/typescript` cannot inject code into QA tooling.
- Regression unit tests in `tests/unit/test_determinism.py`, `tests/unit/test_security_ts_tooling.py`, and `tests/unit/test_task14_plan_determinism_mutation.py`.

## 0.4.1 (2026-11-04)

### Fixed
- Lexi docs-vs-code false positives found in field runs on two TypeScript/Python projects:
  - Git commit hashes (7-40 lowercase hex characters containing a digit, e.g. `ae5a684`) are never treated as code symbols, in backticked names and in call forms.
  - Environment-variable candidates in docs come only from an explicit env context: `process.env.X`, `os.environ[...]` / `os.environ.get(...)` / `getenv(...)`, `$X` / `${X}`, `export X=`, keys of a committed `.env.example` / `.env.sample` / `.env.template`, or explicit "env var X" / "X environment variable" wording. Plain uppercase words (`UPDATE`, `SIGTERM`, `USDC`, `FIFO`, `MERGED`, ...) are no longer env findings.
  - `undocumented_secret` no longer fires for a secret that the root README documents when the docs root is a sub-directory such as `docs/`: root `README*` files and env template keys are now part of the documented-name search, which matches whole names in any form (code block assignment, table cell, backticks).
  - QA certificates (`QA-CERTIFICATE*`), plans (`PLAN-*`) and any document under a `fixtures/` directory are excluded from doc-vs-code claims (`list_claim_docs`); other scanners still see them through `list_docs`.
  - A documented name that exists in a repository JSON/CSV data file (for example a test fixture) is capped at low severity instead of a P1 `missing_code` veto. Config files and manifests (YAML, `package.json`, `tsconfig*.json`) still do not count as evidence.

### Added
- Benchmark fixtures `py_lexi_factory_fp` and `ts_lexi_factory_fp` and unit tests `tests/unit/test_task14_lexi_factory_fp.py`.

## 0.4.0 (2026-11-04)

### Added
- First-class TypeScript and JavaScript support:
  - Automatic language and toolchain detection for Python and TS/JS (including mixed repositories).
  - Package manager detection with frozen lockfile enforcement (`package-lock.json` -> `npm ci`, `pnpm-lock.yaml` -> `pnpm install --frozen-lockfile`, `yarn.lock` -> `yarn install --frozen-lockfile`, fallback `npm install` with WARN).
  - Test runner detection and execution (vitest, jest, mocha, node:test, and npm test) with machine-readable reporting.
  - Typecheck verification with `tsc --noEmit` when `tsconfig.json` exists (P1 finding on compiler errors).
  - Node engine version verification against `engines.node` and `.nvmrc` (stops with `BLOCKED_ENV` on engine mismatch).
  - TS test quality analysis (Riley TS) using vendored TypeScript compiler AST: flags empty assertions, tautologies, constant/mock assertions, swallowed catch blocks, committed `.only` (P1), and committed `.skip`/`.todo` (P2).
  - TS safety analysis (Sable TS): detects child process executions, filesystem writes outside temp directories, unmocked network calls, process environment secrets, and dynamic code evaluation.
- New README and documentation feature claim checks in Lexi:
  - Documented CLI subcommands and flags must exist in code definitions (argparse, click, typer for Python; commander, yargs, cac, argv for TS/JS; P1 on missing).
  - Documented call-form functions/methods must resolve to public symbols in exports and modules (P1 on missing).
  - Documented environment variables and config keys must be referenced in code (P2 on missing).
  - Documented install commands and runtime versions must match manifests (P2 on mismatch).
  - Optional `--claims-strict` flag: marks documented feature claims with no covering test as P2 findings.
- Additional quality and security checks:
  - In-repo `.qa-ignore` (YAML) support in tested projects for downgrading P1/P2/P3 findings (P0 ignore rejected with WARN; expired entries ignored with WARN).
  - Shipped tree identity leak scanner for sensitive tokens, private keys, emails, and personal identifiers (P1).
  - Clean install check (`--clean-install`): verifies that installation from scratch in an empty environment succeeds according to documentation (P1 on failure).
  - Test deletion guard in diff mode (`--diff`): detects deleted test cases versus base ref (P1).
  - Buyer-safe summary report (`summary.md`): compact (<40 lines) report scrubbed of identity and personal data, detailing verdict, test counts, runtime environments, verified features, and known limitations.
- Enhanced Python dependency installation:
  - Automatic installation of test extras (`.[test]`, `.[tests]`, `.[dev]`, PEP 735 dependency groups, and `requirements*.txt`).
  - Fallback pretend version (`SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0`) and minimal git metadata initialization for VCS-backed build tools.
- Updated GitHub Action inputs (`languages`, `node-version`, `diff-base`, `install-extras`, `clean-install`, `summary`) and matrix workflow example `docs/examples/multi-repo-gate.yml`.

### Fixed
- Lexi false-positive drift: documentation example code identifiers (e.g. `foo`, `bar`, `my_function`) and prose words are no longer flagged as missing code P1 findings.
- Non-product and administrative documentation (`AGENTS.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, etc.) capped below veto severity.

## 0.3.6 (2026-11-03)

Verified with 702 unit tests passing (1 skipped), benchmark 15/15, field test 10/10 and a self-test mutation score of 1.00 (29 killed, 0 survived, verdict CLEAR).

### Added
- Exceptions: `exceptions/<project>.yaml` in the QA repo lets a human accept a finding by its stable `fingerprint`, with a required reason and an expiry of at most 90 days. Excepted findings become INFO, expired exceptions produce a WARN, invalid entries fail at startup, and P0 can be excepted only with `allow_p0: true` and is always listed at the top. See `docs/exceptions.md`.
- Every finding in `findings.json` carries a stable `fingerprint`.
- Diff mode: `--diff <base-ref>` scopes all agents to the changed files, turns mutation off and falls back to a full run (stated in the report) when the scope cannot be computed. Typical pre-push run takes seconds.

### Changed
- New tests cover isolation fallbacks, `.env` symlink removal and Nova failure verdicts.

## 0.3.5 (2026-11-03)

Verified with 676 unit tests passing (1 skipped), benchmark 15/15, field test 10/10 and a self-test mutation score of 0.97 (29 killed, 1 survived, verdict CLEAR).

### Added
- IDE findings report: every run writes `findings.json` and `findings.md` with P0-P3 priorities, `file:line` locations, customer impact and a proposed RED test for each finding. Output goes to a per-run directory plus a `latest/` copy; `--findings-dir` chooses the location. The format is described by `docs/findings.schema.json` and the priorities by `docs/findings-priority.md`.
- Lexi indexes TypeScript and JavaScript symbols, so drift checks cover TS/JS projects.

### Changed
- Only P0/P1 findings backed by evidence can produce a `VETO`.
- Vendored and generated directories and backup docs are skipped during scanning.
- Open-question and speculative prose sections are capped at low severity.

### Fixed
- The isolated copy now expands untracked nested repositories and reports skipped files as `WARN` instead of silently swallowing copy errors.

## 0.3.4 (2026-11-02)

Verified with 585 unit tests passing (1 skipped), benchmark 15/15, field test 10/10 and a self-test mutation score of 0.90 (27 killed, 3 survived, verdict CLEAR).

### Summary
- New `BLOCKED_ENV` verdict (exit `2`) for runs stopped by an environment precondition.
- Field test harness in `benchmark/field_test.py`.
- `--llm-network` split and secret scrubbing of LLM payloads.
- JUnit XML output parsing (`core/junit.py`).
- Fixed catastrophic backtracking in a Lexi regular expression.
- Lexi false-positive fixes: anchoring only to defined functions and classes, blockquotes, CI workflow inputs, glossary entries, `file:line` citations, and historical docs capped at `low` severity.
- Environment classification for `PackageNotFoundError`, sqlite open failures, a missing `.git`, `PermissionError` and a missing `node` executable.
- The isolated copy is writable like a normal checkout.

### Fixed
- Lexi no longer escalates plain documentation words to high severity just because they sit next to an unrelated identifier (for example `json.dumps` in a helper script). Anchoring now requires the sibling to be a defined function or class in the project. Test-only names mentioned in docs are ignored.
- Per-failure classification now treats `PackageNotFoundError` (missing distribution metadata in an isolated copy) and `sqlite3: unable to open database file` as environment, matching the whole-run classifier. Broader git-absent messages are also recognized.


### Added
- `BLOCKED_ENV` verdict with exit code `2` for a run stopped by an environment precondition instead of a defect: a test dependency that cannot be imported, a requirement Sable refused to install, missing distribution metadata, a build-time module absent from the source tree, another dependency installation failure, or `sandbox.mode: required` while `bwrap` cannot start. `core/environment.py` classifies the cause and the report names it exactly. Precedence is monotonic (`VETO` > `BLOCKED_ENV` > `CLEAR`): a failing test, an unexplained empty collection, a weak test or a Sable `BLOCK` still vetoes, and `BLOCKED_ENV` is never a pass.
- SARIF rule `VESPER003` (level `warning`) for an environment-blocked run. It is appended to `RULE_CATALOG`, so every existing rule index is unchanged.
- `benchmark/field_test.py`: field test over public repositories pinned to exact commits (`benchmark/field_repos.yaml`), with per-repo expectations in `benchmark/field_expected.yaml`. Writes a markdown table (repo, sha, verdict, sandbox, collected/passed/failed, exit, expected, status, cause) and marks each row `OK`, `OBSERVED`, `REGRESSION` or `ERROR`. `--repos-dir` reuses existing clones read-only and `--offline` forbids fetching; the shallow pinned `git fetch` in the harness is the only network step and happens outside every QA cycle, which keeps `allow_network=false` and never installs the project under test.
- Three guards in `core/environment.py` so an import failure cannot be borrowed as an environment excuse. A missing module whose top-level name is one of the project's own - a package directory backed by `src/<name>/__init__.py` or `<name>/__init__.py`, or a plain top-level `<name>.py` module - stays a `VETO`, and so does a dotted name that resolves to files somewhere inside the copy. A `ModuleNotFoundError` or `PackageNotFoundError` whose traceback raising frame is a `raise` in a project file is a deliberate failure and stays a `VETO`. `BLOCKED_ENV` for a missing module therefore requires the name to look like a real third-party dependency, and the cause states which side of that line it is on: `declared in the project manifest but not installed` or `not installable or not declared in requirements`, read from `requirements*.txt`, `[project.dependencies]`, every `[project.optional-dependencies]` group and `[dependency-groups]`. Where the evidence does not decide, the `VETO` stands.
- `llm.allow_network` config and a `--llm-network` CLI flag split the network switch: they let only the QA process reach a remote LLM provider, while the tested project's sandboxed pytest stays offline regardless of this setting (`--unshare-all` proves the containment). Prompt and payload text are scrubbed of secret-shaped values before any remote call, and the key is never written into a report.
- The runner requests a JUnit XML report (`--junitxml`, written beside - not inside - the wiped `--basetemp` directory) and reads every failure and error node from it, so a clipped console view can never hide a failure; the console summary still supplies the counts. When nothing proves a failure environmental, every failure is conservatively counted against the project.

### Changed
- Riley's technical blocks (empty collection, pytest errors, failed dependency install, non-zero exit) report `BLOCKED_ENV` when the cause is environmental. Its weak-test and uncovered-claim vetoes are unchanged and still outrank the environmental explanation.
- Lexi's "execution collected 0 tests" contradiction drops from `high` to `medium` when the cause is environmental, so an empty collection that nothing ran to contradict no longer escalates to `VETO` on its own.
- The GitHub Action fails the step on any non-zero exit code and emits a workflow warning for `BLOCKED_ENV`; the `verdict` output documents `0` = CLEAR, `2` = BLOCKED_ENV, `1` = VETO or ESCALATE.
- The `missing_dep` benchmark fixture now expects `BLOCKED_ENV` with exit `2`, and the four `seed_dep_*` expectation files record the same verdict. The mutation audit is skipped on a blocked run because there is no passing baseline.
- `benchmark/compare.py` forwards `--sandbox` (default `auto`) to every QA-Ecosystem run and stamps the requested mode, the observed sandbox and the relaxation note into `benchmark/results.md`, so the published table always names the containment its verdicts were measured under.
- The seeded comparison is published for both sandbox modes, because the four `dangerous_test` seeds depend on it: `--sandbox auto` with Bubblewrap active gives default 16/24 (67 %), strict 20/24 (83 %), flat 8/24 (33 %), while `--sandbox off` gives default 20/24 (83 %), strict 24/24 (100 %), flat 8/24 (33 %). The four `seed_dep_*` rows read `BLOCKED_ENV` instead of `VETO` in both modes and still count as caught, so they change no total.
- A refused unsafe requirement now outranks the import errors it caused: with `pip install failed: unsafe requirement: ...` in the install status, the cause is `refused unsafe requirement: <line>` rather than the first missing module name. An ordinary install failure (a timeout, for example) still ranks below a concrete collection error.
- `benchmark/field_test.py` validates the manifest before anything is executed - name `^[A-Za-z0-9._-]+$` without `..`, SHA a full 40-character lowercase commit hash, URL starting with `https://`, docs path inside the clone - and refuses a malformed entry with a clear message instead of passing it to git. `git init` and `git fetch` take `--` before their positional arguments, the pinned commit is re-verified with `git rev-parse HEAD` before `main.py` runs, and a row that never produced a verdict (no clone, wrong commit, no docs) is `ERROR` rather than `REGRESSION`, which also fails the script with exit `1`.
- Lexi never raises a `high` or `VETO` on plain prose. A mention counts as missing code only when it is code-shaped (an identifier that reads like a symbol) and structurally anchored (a definition, list or back-tick context), never as an ordinary sentence word; names that resolve to a standard-library module, a built-in exception or an external package are treated as a reference, not missing project code. This holds for any Latin language, so English, Czech and German prose words do not veto.
- Lexi scans identifiers inside string constants (SQL statements, user-facing messages), and the string-literal check runs before the comments-only check, so a name that only ever appears in a string is reported as present rather than missing.
- Riley no longer flags `assert x is not None` as a tautology, and accepts assertions made through `mock` (for example `assert_called_with`) as real assertions.
- A Lexi `VETO` no longer short-circuits execution: the plan still runs so the report shows what actually happened, and the final precedence stays monotonic (`Sable` > `Riley` > `Lexi`).

### Fixed
- `BLOCKED_ENV` could mask two real defects. A test importing the project's own missing module (`from mypkg.core import add` with no `mypkg/core.py`) was reported as `missing dependency: mypkg.core` and exited `2`; it is a `VETO` now. A `conftest.py` raising `ModuleNotFoundError("No module named 'numpy'")` on purpose earned the same environment stop; the classifier now reads the raising frame and keeps the `VETO`, so a project cannot fake an environment failure.
- A required sandbox that could not start returned before the isolation cleanup and leaked the isolated copy under the temp directory. The run now stays on the normal path, releases the copy, and still reports the drift, plan and pre-flight stages before stopping.
- The dotted-name scan in Lexi could hang the self-test and any large-project drift check. `_DOTTED_NAME_RE` wrapped its leading identifier in its own `+` quantifier (`(?:[A-Za-z_][A-Za-z0-9_]*)+`); that nested quantifier backtracks exponentially over a long run of word characters with no following dot, so scanning an ordinary documentation word blocked for minutes. Rewritten to a linear form (`\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\b`) that matches exactly the same dotted names, with a regression test bounding the scan time.

### Tests
- Offline unit tests for the cause classifier, the field harness (manifest and expectation parsing, clone policy, row evaluation, table rendering, CLI wiring) and the orchestrator/Lexi aggregation paths, including "dangerous code plus a missing dependency stays a VETO" and the exit-code contract. No test touches the network.
- Unit suite: 523 passed, 1 skipped.
- Benchmark fixtures: 15 of 15 match expectation, including `missing_dep` at `BLOCKED_ENV` with exit `2`.
- Field test on the 10 pinned public repositories (`--repos-dir`, `--offline`): 10 OK, 0 OBSERVED, 0 REGRESSION - 5 CLEAR and 5 BLOCKED_ENV with the exact cause named, 0 VETO. The same tree reported 5 false VETOs before this change.
- Self-test mutation score 0.93 (30 evaluated, 28 killed, 2 survived, 0 timeout, 0 error). Both survivors are equivalent mutants: `agents/lexi.py:329` (`or` -> `and` in `_is_prose_phrase`, where every token that satisfies a disjunct also fails the `FUNC_LIKE` fall-through, so the output is identical) and `core/isolation.py:284` (`==` -> `!=` inside an `except AttributeError` that only runs when `Path.is_relative_to` is missing, i.e. unreachable on the 3.11+ baseline). Eight kill tests in `tests/unit/test_task9_mutation_survivors.py` cover `agents/aria.py`, `agents/lexi.py`, `agents/riley.py`, `core/report.py`, `core/runner.py` and `core/sandbox.py`.

## 0.3.3 (2026-11-02)

### Changed
- Sable reports subprocess, exec, eval and `__import__` in tests as WARN instead of BLOCK, but only when the Bubblewrap sandbox is active. In non-test source only eval, exec and `__import__` can become WARN. Without an active sandbox they still block. Findings are labelled `[in tests]` or `[in source]`.
- A test importing the project's own package is no longer treated as network access when the package name matches a network module (for example httpx). The exemption applies only to packages backed by a directory in the copy (`<name>/__init__.py` or `src/<name>/__init__.py`), never to the declared distribution name.
- The orchestrator probes the sandbox again after pre-flight. If WARN relaxations or sandbox-confined deletions were granted but Bubblewrap is no longer available, the run is blocked and vetoed before any test executes.
- For src-layout projects, `src/` inside the isolated copy is added to `PYTHONPATH`, so tests import the copy instead of a same-named package installed in the runner interpreter.

### Fixed
- Lexi no longer reports drift for identifiers in disclaimers and illustrative prose, and treats names used as string literals in code as present.
- Riley recognizes assertion helpers (for example `assert_equal` in a shared test module): one level deep, only helpers with a meaningful assertion, and only modules inside the test tree of the project.

### Tests
- Field test on 10 public Python projects: 0 false VETOs (5 CLEAR, 5 VETOs caused by genuine environment failures such as missing test dependencies).
- Self-test mutation score 93.1% (27 killed, 2 equivalent survivors, 1 timeout).

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

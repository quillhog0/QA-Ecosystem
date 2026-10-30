# QA Agent Ecosystem

[![CI](https://github.com/quillhog0/QA-Ecosystem/actions/workflows/ci.yml/badge.svg)](https://github.com/quillhog0/QA-Ecosystem/actions/workflows/ci.yml)

Version 0.3.1. See CHANGELOG.md.

Part of the quillhog toolkit — [github.com/quillhog0](https://github.com/quillhog0) · [quillhog.xyz](https://quillhog.xyz) · X [@Quillhog0](https://x.com/Quillhog0).

Deterministic QA control plane with six roles. Give it documentation plus a Python project; it walks a fixed test cycle in an **isolated copy** and returns a structured report with a verdict: `CLEAR`, `VETO`, or `ESCALATE`.

It does not patch code, does not run against production, does not read secrets, and does not promise coverage. It answers one question: *do the docs, the code, and the tests actually agree, and does the suite prove what the docs claim?*

## Why

Green tests are not the same as verified claims. A README can promise "never transfers tokens" while no test checks it. This system extracts the claims from the docs, maps each one to the tests that cover it, runs the suite in isolation, and reports the gaps.

## Roles

| Role | Kind | Job |
|------|------|-----|
| **Vesper** | code | Orchestrator: isolation, packets, iteration caps, escalation |
| **Lexi** | judgment | Docs vs code vs tests. Extracts claims from docs. `VETO` on drift |
| **Aria** | judgment | Scenarios, acceptance criteria, claim-to-test coverage mapping |
| **Riley** | judgment | Adversarial review. Technical `VETO` with root cause, flags uncovered claims |
| **Sable** | policy-as-code | Pre-flight: no secrets, no live tree, pytest-only, no destructive commands. `BLOCK` on risk |
| **Nova** | code | Per-project venv, dependency install, pytest in the copy. Never repairs |

Roles exchange only structured packets (`core/packets.py`), never free chat. Veto order: **Sable > Riley > Lexi**. Max **2** iterations per stage. If the loop cannot close: `ESCALATE: DOMAIN_EXPERT`.

Judgment roles run on deterministic heuristics by default. An LLM is optional, and every report states per role whether it was used: `llm: none`, `llm: ok`, or `llm: fallback: <reason>`. An LLM can add findings or tighten a verdict, but can never override a deterministic VETO.

## Loop

1. Vesper → Lexi (drift + claims)
2. Lexi OK → Aria (plan + claim coverage)
3. Aria → Riley (plan review)
4. Riley OK → Sable (pre-flight)
5. Sable OK → Nova (venv + pytest in the copy)
6. Nova → Riley + Lexi (post-audit)
7. Vesper closes or escalates

## Example output

Run against `examples/sample-project` (bundled in this repo).

```
verdict=CLEAR
nova / execute: collected=3 passed=3 failed=0 exit=0 install=no dependency manifest
```

| claim | covered_by | score |
|---|---|---|
| `divide` must reject a zero divisor with `ValueError`. | tests/test_calculator.py::test_divide_rejects_zero, tests/test_calculator.py::test_divide | 13 |

## Does it beat a single agent?

Comparison against a "flat agent" baseline that only runs `pytest` and returns `CLEAR` on exit 0 (see `benchmark/compare.py` and `benchmark/results.md`).

**Summary:** default 20/24 (83 %), strict (riley.veto_on_uncovered_claims enabled via --strict) 24/24 (100 %), flat 8/24 (33 %)

| project | bug category | default | strict | flat |
|---|---|---|---|---|
| seed_code_01_pricing_discount | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_code_02_auth_validation | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_code_03_ledger_balance | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_code_04_rate_limiter | untested_code_bug | CLEAR | VETO | CLEAR |
| seed_danger_01_eval_call | dangerous_test | VETO | VETO | CLEAR |
| seed_danger_02_os_system | dangerous_test | VETO | VETO | CLEAR |
| seed_danger_03_dunder_import | dangerous_test | VETO | VETO | CLEAR |
| seed_danger_04_conftest_exec | dangerous_test | VETO | VETO | CLEAR |
| seed_dep_01_missing_client | missing_dep | VETO | VETO | VETO |
| seed_dep_02_missing_parser | missing_dep | VETO | VETO | VETO |
| seed_dep_03_missing_test_helper | missing_dep | VETO | VETO | VETO |
| seed_dep_04_missing_crypto_lib | missing_dep | VETO | VETO | VETO |
| seed_drift_01_removed_export | doc_drift | VETO | VETO | CLEAR |
| seed_drift_02_renamed_endpoint | doc_drift | VETO | VETO | CLEAR |
| seed_drift_03_missing_constant | doc_drift | VETO | VETO | CLEAR |
| seed_drift_04_unimplemented_helper | doc_drift | VETO | VETO | CLEAR |
| seed_tautology_01_math_div | tautology_test | VETO | VETO | CLEAR |
| seed_tautology_02_str_strip | tautology_test | VETO | VETO | CLEAR |
| seed_tautology_03_status_check | tautology_test | VETO | VETO | CLEAR |
| seed_tautology_04_crypto_hash | tautology_test | VETO | VETO | CLEAR |
| seed_uncommitted_01_subtraction | uncommitted_bug | VETO | VETO | VETO |
| seed_uncommitted_02_off_by_one | uncommitted_bug | VETO | VETO | VETO |
| seed_uncommitted_03_inverted_bool | uncommitted_bug | VETO | VETO | VETO |
| seed_uncommitted_04_broken_multiplier | uncommitted_bug | VETO | VETO | VETO |

**Result:** default 20/24, strict (riley.veto_on_uncovered_claims enabled via --strict) 24/24, flat 8/24.

## Isolation

- Every run works on a copy under the system temp dir: `<tmp>/qa-agent-ecosystem-isolations/<run_id>/isolated_copy`, regardless of `--reports-dir`.
- The live working tree is never checked out, written, or deleted.
- `.env`, keys, and credential filenames are skipped.
- Nova builds a venv in the run's work dir and installs the project's declared dependencies (`requirements.txt` / `pyproject.toml`). Only `pip install` may use the network; pytest runs with network off unless `--allow-network` is passed.
- Default is **dry-run**: no live actions, no secret access, logs archived.

## Sandbox

Execution of the isolated pytest test suite is wrapped in a kernel-level sandbox using **Bubblewrap (`bwrap`)**:

- **Namespaces:** Unshares all namespaces (`--unshare-all`: network, mount, PID, IPC, UTS, and user).
- **Network off:** Network access is completely disabled at the kernel level for tests by default; sockets cannot be connected.
- **Filesystem boundaries:** System binaries and libraries (`/usr`, `/etc`, `/bin`, `/lib`, `/lib64`) and `work_dir` (including `_sable_guard.py`) are mounted read-only. `/tmp` is an isolated in-memory `tmpfs`. The only writable mounts inside the sandbox are the isolated project copy (`isolated_root`), `work_dir/pytest_cache`, `work_dir/pytest_tmp`, and `/home/sandbox`. Extra read-only mounts (`extra_ro_binds`) reject any path equal to or an ancestor of the host `$HOME` or `/`.
- **Isolated `$HOME`:** `work_dir/home` is created and bound to `/home/sandbox` before `/home` is remounted read-only (`--remount-ro /home`), so `HOME` (`/home/sandbox`) exists and is writable only there while real host credentials, SSH keys, and user dotfiles in `$HOME` are never exposed.
- **Strict environment allowlist:** `sanitized_env` passes only a strict allowlist (`PATH, LANG, LC_ALL, LC_CTYPE, TMPDIR, TEMP, TMP, SYSTEMROOT, COMSPEC`) and explicitly never forwards `PYTEST_ADDOPTS, PYTHONSTARTUP, PYTHONPATH` or `PYTHON*`/`PYTEST*` wildcards from the host.
- **Path validation:** `run_pytest` resolves `isolated_root` and `work_dir` once at the start and raises `ValueError` if either contains `os.pathsep`.
- **Process lifecycle:** Runs with `--die-with-parent` to ensure child processes terminate with the runner.
- **Defense-in-depth:** The existing Sable runtime guard (`-p _sable_guard`, `NO_NETWORK=1`) remains active as a second layer on a read-only mount.
- **Configuration:** Set via `sandbox.mode = auto | required | off` (default `auto`) or CLI `--sandbox [auto|required|off]`. Unknown or misspelled modes raise `ValueError` (fail closed). When set to `required`, a missing `bwrap` binary yields an immediate `VETO`. When `auto` falls back on non-Linux or systems without `bwrap`, the report states `sandbox: unavailable (ran unsandboxed)` clearly. Tested projects can never modify sandbox configuration.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
```

This installs the `qa-eco` command (run it from the cloned repo with `pip install -e .`; a non-editable install is not supported yet).

## Run

```bash
qa-eco --project /path/to/project --docs /path/to/docs
# or, without installing:
python main.py --project /path/to/project --docs /path/to/docs
```

Writes `reports/<run_id>.md` and `reports/<run_id>.yaml` (or into `--reports-dir`).

Process exit code: `0` = `CLEAR`, `1` = `VETO` or `ESCALATE`.

### Sample project bundled here

```bash
python main.py --project examples/sample-project --docs examples/sample-project/docs
```

## GitHub Action

Add QA Ecosystem as a gate in any repository's workflow. The job fails when the verdict is VETO, and the report is written to the job summary.

```yaml
- uses: actions/checkout@v4
- uses: quillhog0/QA-Ecosystem@master
  with:
    project: .
    docs: docs
    strict: "true"
```

Inputs: project, docs, strict, mutation, mutation-min-score, sandbox (auto, required or off), allow-sysctl-userns, python-version and sarif (true/false, defaults to false). On GitHub's Ubuntu 24.04 runners the sandbox needs allow-sysctl-userns set to true, which relaxes a kernel security setting on the runner; without it the run is unsandboxed and the report says so. Outputs: verdict, report and sarif (path to the SARIF document, empty when the sarif input is false or the file was not written). The action writes the SARIF file; uploading it to a code-scanning service is left to the caller.

## SARIF output

Pass `--sarif PATH` on the command line, or set `sarif: "true"` in the action, to emit a SARIF 2.1.0 document that maps the finished run to code annotations. The document is produced by `core/sarif.py` from the report dictionary only; it never re-runs any role and never imports anything from `agents/`. Locations always use project-relative URIs under `%SRCROOT%`; absolute paths, isolated-copy paths and any traversal are dropped instead of being written.

Rule identifiers and their positions in the document's `tool.driver.rules` array are stable across runs and match the table below. To add a rule, append it to `RULE_CATALOG` in `core/sarif.py` and to this table; never reorder existing rows.

| rule id | what it flags | default level |
|---|---|---|
| LEXI001 | Docs term has no matching code symbol | warning (severity: high→error, medium→warning, low→note) |
| LEXI002 | Missing or unreferenced test surface | warning |
| LEXI003 | Post-audit contradicts the docs | error |
| LEXI004 | Docs term appears only in comments or docstrings | note |
| LEXI005 | Documentation drift flagged by review | warning |
| LEXI999 | Other Lexi drift kind | warning |
| SABLE001 | Command is not pytest-only | error |
| SABLE002 | Secret-looking environment keys reach Nova | error |
| SABLE003 | Secret-looking files in the isolated copy | error |
| SABLE004 | Refusing to run against the live tree | error |
| SABLE005 | Isolated root is missing | error |
| SABLE006 | pytest target, `--rootdir` or cwd escapes the isolated copy | error |
| SABLE007 | Blocked call in the isolated copy | error |
| SABLE008 | Network utility invoked while `allow_network` is false | error |
| SABLE009 | pytest collected zero tests | error |
| RILEY001 | Weak test detected (skipped, tautological, or assertion swallowed) | warning (error when Riley vetoes) |
| RILEY002 | Documented claim is uncovered or covered only by a weak test | warning (error when Riley vetoes) |
| RILEY999 | Other Riley gap | warning (error when Riley vetoes) |
| NOVA001 | A pytest test failed during the isolated run | error |
| NOVA002 | Dependency installation failed before pytest ran | error |
| VESPER001 | The loop did not close and the run escalated | error |
| VESPER002 | Run vetoed before execution: verdict is not CLEAR and no other rule produced a result | error |

## Tests and benchmark

```bash
pytest
python benchmark/run_benchmark.py
python main.py --self-test
```

- `pytest` runs `tests/unit/` and the fixture benchmark (`tests/test_benchmark.py`). The one network test runs only with `QA_NETWORK_TESTS=1`.
- `python benchmark/run_benchmark.py` walks `benchmark/fixtures/`, compares verdict, exit code, `install_status`, and `root_cause` to each `expected.yaml`, and writes `reports/benchmark-<ts>.md`. Exit `0` only when every fixture matches.
- `python main.py --self-test` runs the QA cycle on this repository (`project=.`, docs = `README.md` + `schemas/`).

Benchmark fixtures:

| fixture | expected |
|---|---|
| clean | CLEAR |
| live_tree_guard | CLEAR, live tree untouched |
| secret_leak | CLEAR, `.env` never copied |
| dangerous_test | BLOCK/VETO by Sable |
| doc_drift | VETO by Lexi |
| failing_test | VETO by Riley |
| missing_dep | VETO, root cause `missing dependency: <pkg>` |
| no_tests | VETO |
| malicious_conftest | BLOCK/VETO by Sable |
| own_conftest | CLEAR |
| strong_tests | CLEAR, 100% mutants killed by test suite |
| tautology_test | VETO by Riley |
| uncommitted_bug | VETO by Nova/Riley |
| weak_tests | VETO, surviving mutants drop score below threshold |
| cheating_agent | VETO: skipped test, assertion swallowed by try/except, only trivial assertions |

## Mutation testing

An optional adversarial test quality audit evaluated by **Riley** after baseline test execution passes:

- **Mutation operators:** AST-level mutations for comparisons (`< <= > >= == !=`), arithmetic (`+ -`), boolean constants (`True/False`), return values (`return expr -> return None`), and boolean operators (`and/or`).
- **Isolation:** Each mutant runs in a clean working copy inside the Bubblewrap sandbox with Sable guards active.
- **Scoring:** `score = killed / (killed + survived)`. If `mutation.min_score > 0` and the score falls below the threshold, Riley issues a `VETO`.
- **Configuration:** Enabled only via system config or CLI flags `--mutation`, `--mutation-min-score [float]`, `--mutation-max-mutants [int]`. Tested projects can never modify or disable mutation settings.

### Self-test results

Running mutation testing on QA Ecosystem itself (`python main.py --self-test --mutation`, evaluating 30 deterministic mutants) yields:

- **Score:** **93.1%** at release 0.3.1 (27 killed, 2 survived, 0 timeout, 1 error, out of 30 sampled mutants; errors are excluded from the score). The mutant sample is deterministic (seed 42) but shifts as the code changes, so the figure varies between releases; re-run the command to measure the current tree.
- **Note:** earlier builds counted a mutant as killed when the test run crashed before any test executed (for example when the sandbox could not start the project venv). That bug is fixed and covered by tests, so this score only counts real test failures.
- **Surviving mutants at 0.3.1:** agents/aria.py:48 (`>` -> `>=`) and core/mutation.py:385 (`True` -> `False`). Both are equivalent mutants: at aria.py:48 the single-part match re-adds a token that is already added and then deduplicated, so the output is unchanged; at mutation.py:385 the ignore-errors flag on the cleanup tree removal only matters in filesystem states where the original would also fail.

## Config

See `config.yaml`.

- `dry_run` (default true): skip outbound LLM calls unless `llm.call_in_dry_run` is true
- `allow_network` (default false): Nova env gets dead proxies; Sable still forbids curl/wget
- `max_iterations`: per-stage cap (default 2)
- `sandbox.mode`: `auto` (default, uses bwrap when available), `required` (veto if bwrap missing), or `off`
- `mutation.enabled`: `false` (default), enable mutation testing phase
- `mutation.min_score`: `0.0` (default, report only); set > 0.0 to VETO if mutation score is below threshold
- `mutation.max_mutants`: `30` (default), limit of mutants evaluated per run
- `llm.provider`: `none` (heuristics), `openrouter`, or `local`
- `nova.install_deps`, `nova.pip_timeout`: dependency install in the isolated venv
- `lexi.doc_globs`, `lexi.ignore_identifiers`: which docs to read, which terms to ignore
- `lexi.ignore_identifiers` is read only from the operator config, never from the config.yaml of the project under test
- `keep_isolated_copy` (default false): keep the isolated copy after the run instead of deleting it
- `pytest.timeout_seconds` (default 120): timeout for the isolated pytest run
- `sable.allow_in_tests`: operator allowlist of blocked calls in tests; each entry needs `path`, `call` and `reason`
- `mutation.seed` (default 42), `mutation.timeout_per_mutant` (default 30 seconds)
- `riley.veto_on_uncovered_claims`: Riley vetoes when a documented claim has no covering test

CLI: `--config PATH` loads an operator config other than `config.yaml`; `--no-dry-run` turns off dry-run so configured LLM calls can run.

Sable and Nova never depend on a model. OpenRouter is used only when `OPENROUTER_API_KEY` is set, `llm.provider` is `openrouter`, dry-run LLM is allowed, and `--allow-network` is on. The key is never written into reports.

## Known limits

- **Static scan scope:** Sable's AST scan flags `subprocess`, `socket`, `httpx` and `requests` usage only in test files. Application code may legitimately spawn processes or open connections, so it is not blocked statically; at runtime the socket guard plugin and the network-less sandbox still apply.
- **Bubblewrap coverage & non-covered vectors:** On Linux with `bwrap` installed, test suites run in an unprivileged container sandbox (unshared network, mount, PID, IPC, UTS, and user namespaces, read-only system trees and `work_dir`, writable mounts restricted to `isolated_root`, `pytest_cache`, `pytest_tmp`, and `/home/sandbox`, and tmpfs `/tmp`).
  - *What bwrap covers:* Kernel-enforced denial of socket creation and outbound network connections, prevention of writes to `work_dir/_sable_guard.py` or outside the isolated project copy and designated scratch directories (`pytest_cache`, `pytest_tmp`, `/home/sandbox`), rejection of read-only mounts equal to or above `$HOME` or `/`, and isolation from host user files and credentials in `$HOME`.
  - *What bwrap does not cover:* It does not protect against host Linux kernel vulnerabilities; it does not enforce cgroup-based CPU/memory quotas; `/etc` is mounted read-only so world-readable host files under `/etc` remain readable; and dependency installation (`pip install`) runs in a preliminary step before the sandbox (though manifests are statically screened for unsafe package sources). On platforms without `bwrap` or non-Linux systems in `auto` mode, execution falls back to process-level isolation (`chmod`, `-p _sable_guard` monkeypatching, and static AST scanning) and reports `sandbox: unavailable (ran unsandboxed)`.
  - *Ubuntu 24.04 user namespace restriction:* On Ubuntu 24.04, AppArmor restricts unprivileged user namespaces (`kernel.apparmor_restrict_unprivileged_userns=1`); `is_bwrap_available` probes bwrap namespace creation and falls back to unsandboxed execution with `sandbox: unavailable (bwrap present but not usable: <reason>)` in `auto` mode or vetoes in `required` mode unless permitted by AppArmor or sysctl.
- **Dependency installation is not sandboxed.** When the project has a requirements file or pyproject dependencies, pip installs them before the sandbox starts and with network access, so package build scripts run on the host. Manifests are screened for unsafe sources, but only the test run itself is sandboxed.
- **With an LLM enabled, docs and code go to the provider unmasked.** Nothing is redacted or anonymised before the API call.
- **Claim-to-test mapping is a heuristic.** Aria matches doc claims to tests by token overlap and test-body analysis. It is not formal verification.
- **The local LLM provider is not gated by `allow_network`.** Only the OpenRouter path checks `allow_network`; `llm.provider: local` is gated only by dry-run.
- **Sable's static scan is literal-name based.** It misses aliased imports (`import shutil as s`) and calls on variable receivers (`p.unlink()`), and it respects the project's own pytest `norecursedirs`; the Bubblewrap sandbox still applies at runtime.
- **String literals count as code.** A function removed from code but whose name still appears as a string literal is not reported by Lexi.

## Project layout

```
main.py              CLI entry point (--project/--docs, --self-test)
config.yaml          safe defaults (dry_run, no network, llm.provider: none)
core/                Vesper orchestrator, isolation, packets, pytest runner, deps, report, SARIF writer
agents/              Lexi, Aria, Riley, Nova + LLM adapters and prompts
policies/sable.py    Sable pre-flight policy (secrets, live tree, dangerous calls)
schemas/             report and handoff packet formats
benchmark/           fixtures + run_benchmark.py
examples/            sample project used in the docs
tests/               unit tests and fixture benchmark test
```

## What this is not

- Not a fixer, not a coverage tool, not a production runner
- Not a single-chat role-play: each role has its own prompt or policy and only structured packets move between them

## Related

[safe-agent-kit](https://github.com/quillhog0/safe-agent-kit) is the companion rule set: plain-Markdown role templates that tell AI coding agents what they may and may not do. This repo is the enforcement side: it checks the result in isolation and returns a report or a veto.

## License

MIT. See [LICENSE](LICENSE).

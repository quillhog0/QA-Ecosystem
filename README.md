# QA Agent Ecosystem

[![CI](https://github.com/quillhog0/QA-Ecosystem/actions/workflows/ci.yml/badge.svg)](https://github.com/quillhog0/QA-Ecosystem/actions/workflows/ci.yml)

Version 0.1.0. See CHANGELOG.md.

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

Run against `examples/sample-project` (bundled in this repo). The claim-coverage table header `tvrzení` is printed verbatim by `core/report.py`; it means "claim".

```
verdict=CLEAR
nova / execute: collected=3 passed=3 failed=0 exit=0 install=no dependency manifest
```

| tvrzení (claim) | covered_by | score |
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
- **Strict environment allowlist:** `sanitized_env` passes only a strict allowlist (`PATH`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TMPDIR`, `TEMP`, `TMP`, `SYSTEMROOT`, `COMSPEC`) and explicitly never forwards `PYTEST_ADDOPTS`, `PYTHONSTARTUP`, `PYTHONPATH`, or `PYTHON*`/`PYTEST*` wildcards from the host.
- **Path validation:** `run_pytest` resolves `isolated_root` and `work_dir` once at the start and raises `ValueError` if either contains `os.pathsep`.
- **Process lifecycle:** Runs with `--die-with-parent` to ensure child processes terminate with the runner.
- **Defense-in-depth:** The existing Sable runtime guard (`-p _sable_guard`, `NO_NETWORK=1`) remains active as a second layer on a read-only mount.
- **Configuration:** Set via `sandbox.mode = auto | required | off` (default `auto`) or CLI `--sandbox [auto|required|off]`. Unknown or misspelled modes raise `ValueError` (fail closed). When set to `required`, a missing `bwrap` binary yields an immediate `VETO`. When `auto` falls back on non-Linux or systems without `bwrap`, the report states `sandbox: unavailable (ran unsandboxed)` clearly. Tested projects can never modify sandbox configuration.

## Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python main.py --project /path/to/project --docs /path/to/docs
```

Writes `reports/<run_id>.md` and `reports/<run_id>.yaml` (or into `--reports-dir`).

Process exit code: `0` = `CLEAR`, `1` = `VETO` or `ESCALATE`.

### Sample project bundled here

```bash
python main.py --project examples/sample-project --docs examples/sample-project/docs
```

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

## Mutation testing

An optional adversarial test quality audit evaluated by **Riley** after baseline test execution passes:

- **Mutation operators:** AST-level mutations for comparisons (`< <= > >= == !=`), arithmetic (`+ -`), boolean constants (`True/False`), return values (`return expr -> return None`), and boolean operators (`and/or`).
- **Isolation:** Each mutant runs in a clean working copy inside the Bubblewrap sandbox with Sable guards active.
- **Scoring:** `score = killed / (killed + survived)`. If `mutation.min_score > 0` and the score falls below the threshold, Riley issues a `VETO`.
- **Configuration:** Enabled only via system config or CLI flags `--mutation`, `--mutation-min-score [float]`, `--mutation-max-mutants [int]`. Tested projects can never modify or disable mutation settings.

### Self-test results

Running mutation testing on QA Ecosystem itself (`main.py --project . --docs README.md --mutation`, evaluating 30 deterministic mutants) yields:

- **Score:** **43.3%** (13 killed, 17 survived, 0 timeout, 0 error)
- **Surviving mutants (weak points):**
  - `agents/adapters.py:80` (`and` → `or`) and `81` (`return None` → `return True`): unasserted fallback when model completion returns empty.
  - `agents/adapters.py:120` (`or` → `and`): unasserted branch in adapter payload normalization.
  - `agents/aria.py:230` (`False` → `True`): default flag in scenario token weighting.
  - `agents/aria.py:296`, `318` (`or` → `and`): heuristic token overlap matching thresholds.
  - `agents/riley.py:54` (`False` → `True`): weak test classification fallback.
  - `agents/riley.py:289` (`return <expr>` → `return None`): unasserted helper return in review formatting.
  - `core/isolation.py:330`, `336` (`True` → `False`): check flags on git worktree prune/cleanup subprocess calls.
  - `core/orchestrator.py:41` (`True` → `False`): default flag handling in orchestrator init.
  - `core/packets.py:140` (`return <expr>` → `return None`): helper representation in packets dataclass.
  - `core/report.py:109` (`==` → `!=`): formatting boundary in report generator.
  - `main.py:127` (`return <expr>` → `return None`): exit code helper function in CLI entrypoint.
  - `policies/sable.py:140` (`+` → `-`), `176` (`!=` → `==`): string offset and AST node filter branches in AST security scanner.

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

Sable and Nova never depend on a model. OpenRouter is used only when `OPENROUTER_API_KEY` is set, `llm.provider` is `openrouter`, dry-run LLM is allowed, and `--allow-network` is on. The key is never written into reports.

## Known limits

- **Bubblewrap coverage & non-covered vectors:** On Linux with `bwrap` installed, test suites run in an unprivileged container sandbox (unshared network, mount, PID, IPC, UTS, and user namespaces, read-only system trees and `work_dir`, writable mounts restricted to `isolated_root`, `pytest_cache`, `pytest_tmp`, and `/home/sandbox`, and tmpfs `/tmp`).
  - *What bwrap covers:* Kernel-enforced denial of socket creation and outbound network connections, prevention of writes to `work_dir/_sable_guard.py` or outside the isolated project copy and designated scratch directories (`pytest_cache`, `pytest_tmp`, `/home/sandbox`), rejection of read-only mounts equal to or above `$HOME` or `/`, and isolation from host user files and credentials in `$HOME`.
  - *What bwrap does not cover:* It does not protect against host Linux kernel vulnerabilities; it does not enforce cgroup-based CPU/memory quotas; `/etc` is mounted read-only so world-readable host files under `/etc` remain readable; and dependency installation (`pip install`) runs in a preliminary step before the sandbox (though manifests are statically screened for unsafe package sources). On platforms without `bwrap` or non-Linux systems in `auto` mode, execution falls back to process-level isolation (`chmod`, `-p _sable_guard` monkeypatching, and static AST scanning) and reports `sandbox: unavailable (ran unsandboxed)`.
- **With an LLM enabled, docs and code go to the provider unmasked.** Nothing is redacted or anonymised before the API call.
- **Claim-to-test mapping is a heuristic.** Aria matches doc claims to tests by token overlap and test-body analysis. It is not formal verification.

## Project layout

```
main.py              CLI entry point (--project/--docs, --self-test)
config.yaml          safe defaults (dry_run, no network, llm.provider: none)
core/                Vesper orchestrator, isolation, packets, pytest runner, deps, report
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

## License

MIT. See [LICENSE](LICENSE).

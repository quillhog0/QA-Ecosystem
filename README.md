# QA Agent Ecosystem

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

## Isolation

- Every run works on a copy under the system temp dir: `<tmp>/qa-agent-ecosystem-isolations/<run_id>/isolated_copy`, regardless of `--reports-dir`.
- The live working tree is never checked out, written, or deleted.
- `.env`, keys, and credential filenames are skipped.
- Nova builds a venv in the run's work dir and installs the project's declared dependencies (`requirements.txt` / `pyproject.toml`). Only `pip install` may use the network; pytest runs with network off unless `--allow-network` is passed.
- Default is **dry-run**: no live actions, no secret access, logs archived.

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
| tautology_test | VETO by Riley |
| uncommitted_bug | VETO by Nova/Riley |

## Config

See `config.yaml`.

- `dry_run` (default true): skip outbound LLM calls unless `llm.call_in_dry_run` is true
- `allow_network` (default false): Nova env gets dead proxies; Sable still forbids curl/wget
- `max_iterations`: per-stage cap (default 2)
- `llm.provider`: `none` (heuristics), `openrouter`, or `local`
- `nova.install_deps`, `nova.pip_timeout`: dependency install in the isolated venv
- `lexi.doc_globs`, `lexi.ignore_identifiers`: which docs to read, which terms to ignore

Sable and Nova never depend on a model. OpenRouter is used only when `OPENROUTER_API_KEY` is set, `llm.provider` is `openrouter`, dry-run LLM is allowed, and `--allow-network` is on. The key is never written into reports.

## Known limits

- **Isolation is copy + chmod + guards, not a sandbox.** The project is copied into a work dir, files are made read-only (`chmod`, `core/isolation.py`), Sable injects a pytest plugin that blocks `socket.connect` (`policies/sable.py`), and Sable statically scans tests with AST for risky calls and imports. There is no OS container.
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

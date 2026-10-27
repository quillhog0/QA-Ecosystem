# QA Agent Ecosystem

Deterministic control plane with six roles. It takes documentation plus a Python project (or a tree that already contains tests), walks a fixed test cycle in an **isolated copy**, and writes a structured report. It does not patch code, does not run against production, does not read secrets, and does not promise coverage.

## Roles

| Role | Kind | Job |
|------|------|-----|
| **Vesper** | code | Orchestrator: isolation, packets, iteration caps, escalation |
| **Lexi** | judgment | Docs vs code vs tests. `VETO` on drift |
| **Aria** | judgment | Scenarios and acceptance criteria |
| **Riley** | judgment | Adversarial review. Technical `VETO` with root cause |
| **Sable** | policy-as-code | Pre-flight: no secrets, no live tree, pytest-only, no destructive commands. `BLOCK` on risk |
| **Nova** | code | Run pytest in the copy. Collect counts and exit code. Never repairs |

Veto order: **Sable > Riley > Lexi**. Max **2** iterations per stage. If the loop cannot close: `ESCALATE: DOMAIN_EXPERT`.

## Loop

1. Vesper → Lexi (drift)
2. Lexi OK → Aria (plan)
3. Aria → Riley (plan review)
4. Riley OK → Sable (pre-flight)
5. Sable OK → Nova (pytest in the copy)
6. Nova → Riley + Lexi (post-audit)
7. Vesper closes or escalates

## Isolation

- Prefer a detached `git worktree` of `HEAD`. Otherwise copy the tree.
- The live working tree is never checked out or written.
- `.env`, keys, and credential filenames are skipped.
- Sources in the copy are made read-only. Pytest cache and logs go under `reports/<run_id>/`.
- If `reports/` sits inside the project being copied, the isolated tree is created under a process temp directory so the copy cannot nest into itself.
- Default is **dry-run**: no live actions, no secret access, logs archived. Live trees are never deleted.

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

Writes `reports/<run_id>.md` and `reports/<run_id>.yaml`.

Process exit code: `0` = `CLEAR`, `1` = `VETO` or `ESCALATE`.

### Sample project bundled here

```bash
python main.py --project examples/sample-project --docs examples/sample-project/docs
```

## Testy a benchmark

```bash
pytest
python benchmark/run_benchmark.py
python main.py --self-test
```

- `pytest` runs `tests/unit/` and the fixture benchmark (`tests/test_benchmark.py`).
- `python benchmark/run_benchmark.py` walks `benchmark/fixtures/`, compares each run to `expected.yaml`, prints a table, and writes `reports/benchmark-<ts>.md`. Exit `0` only when every fixture matches.
- `python main.py --self-test` runs the QA cycle on this repository (`project=.`, docs = `README.md` + `schemas/`). The isolated copy is placed outside this tree’s `reports/`.

## Config

See `config.yaml`.

- `dry_run` (default true): skip outbound LLM calls unless `llm.call_in_dry_run` is true
- `allow_network` (default false): Nova env gets dead proxies; Sable still forbids curl/wget
- `max_iterations`: per-stage cap (default 2)
- `llm.provider`: `none` (heuristics), `openrouter`, or `local`

Judgment roles (Lexi, Aria, Riley) use heuristics when no LLM is configured. Sable and Nova never depend on a model.

OpenRouter is used only when `OPENROUTER_API_KEY` is set, `llm.provider` is `openrouter`, dry-run LLM is allowed, and `--allow-network` is on. The key is not written into reports.

## What this is not

- Not a fixer, not a coverage tool, not a production runner
- Not a single-chat role-play: each role has its own prompt or policy and only structured packets move between them

# Findings priority mapping

Every run writes `findings.json` (schema: `findings.schema.json`) and a short
`findings.md` next to the Markdown and YAML reports. The findings document is a
pure *projection* of what the roles already decided: producing it never changes
a run verdict, and the pipeline (Sable pre-flight, Nova execution, Lexi drift,
Riley post-audit) remains the only authority on CLEAR / VETO / BLOCKED_ENV.

## Priorities

| Priority | Meaning | Where it comes from |
|---|---|---|
| P0 | data loss, money or security | Sable failures of the secret/secret-files/live-tree/isolated-target/AST/network checks; a failing product test whose name or message mentions a money/security term (`payment`, `refund`, `charge`, `payout`, `withdraw`, `deposit`, `secret`, `credential`, `password`, `api_key`, `private_key`, `sandbox`, `escape`, `data_loss`, `corrupt`, `wipe`, `exfil`) |
| P1 | product test failure, high drift, or a hollow test that cannot fail | any Nova failure classified as product that is not P0; Sable `pytest_only` / `isolated_exists` failures; Lexi drift with severity `high`; a leftover `.only`; a literal tautology (`assert True`, `assert 1 == 1`); a `pass` / `...` / docstring-only body; or a `try/except` that catches `AssertionError` / `Exception` / `BaseException` (or a bare `except:`) and does not re-raise — always with `file:line` evidence |
| P2 | medium drift, ordinary weak test, unproven claim | Lexi drift `medium`; Riley skip/trivial/snapshot weak tests and plan-quality gaps; a test with no explicit assert that still calls code (and can raise); a narrow `except` (e.g. `UnicodeDecodeError`) around a live assert; Riley claim gaps and Aria claims with no covering test (these never VETO); Nova failures classified as environment; Nova pip-install failures; Sable non-blocking check failures (`collected_gt_zero`) |
| P3 | low or historical items and nits | Lexi drift `low` (historical records, vocabulary notes); Sable warnings (sandbox-conditional relaxations) |

Environment findings always use `type=environment` and are never P0 or P1: a
missing tool or blocked capability proves nothing about the product, so it can
only ever carry the run-wide BLOCKED_ENV that the pipeline already raised.

Drift severity maps deterministically: `high` -> P1, `medium` -> P2, `low` -> P3.

## Finding types

`drift` (docs promise without code), `claim` (documented claim unproven or only
weakly covered), `weak_test` (a test that does not really check behaviour),
`policy` (a Sable gate), `test_failure` (a red product test), `environment`
(a precondition that prevented judgement).

## Verdict rule

A finding's `verdict_effect` is `VETO` only when its priority is at or above
`fail-on` (default P1, so P0/P1) *and* its `evidence` is non-empty. P3 findings
are `INFO`; everything else is `WARN`. Claim findings (`type=claim`) stay
`WARN` even when `fail-on` is P2: an uncovered or weak-only claim alone must
not VETO.

The run verdict is derived only from findings: `VETO` if and only if at least
one finding has `verdict_effect=VETO`. A `VETO` verdict without a `VETO`
finding is impossible, and a `VETO` finding always yields `VETO`.
`BLOCKED_ENV` and `ESCALATE` are kept when no such finding exists. Role-level
verdicts (including Riley post-audit) cannot set `VETO` on their own.

## Determinism

Findings are deduplicated on `(agent, type, file, line, evidence)`, ordered by
priority, then agent, then file, then line, then discovery sequence, and
numbered `F-001`, `F-002`, ... The same report dictionary always produces a
byte-identical `findings.json`.

`findings.md` is capped at about 40 lines: a per-priority count table plus the
top 10 findings, each with its `file:line` anchor and a one-line impact.
For Lexi drift findings the anchor is the doc line that makes the promise
(post-audit contradictions included), so an IDE can jump straight to the
sentence that needs fixing.

Every finding also carries optional `code_file`/`code_line`: the nearest real
code candidate the tool can name. A `missing_tests` drift finding points at the
source file whose public surface has no covering test; a `missing_code` or
`domain_term` drift finding has no code candidate by construction, so both
fields are `null` there. Non-drift findings currently do not populate them.

## Output layout

Each run's findings live under `<findings-dir>/<run_id>/`:
`findings.json` and `findings.md`. The same pair is mirrored to
`<findings-dir>/latest/` so an IDE or dashboard can watch a single stable path
without caring about the run id. The two copies are always byte-identical.

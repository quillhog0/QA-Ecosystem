# QA run `{run_id}`

- **verdict:** CLEAR | VETO | BLOCKED_ENV | ESCALATE
- **dry_run:** true
- **project:** `{project_root}`
- **docs:** `{docs_root}`
- **isolated copy:** `{isolated_root}`
- **escalation:** `ESCALATE: DOMAIN_EXPERT` (only when present)
- **note:** environment blocked; the tests could not run, so this is not a pass and no defect is claimed against the project (only when the verdict is BLOCKED_ENV)

## Role results

Per-role verdicts in loop order: Vesper → Lexi → Aria → Riley → Sable → Nova → Riley + Lexi.

## Bounds

This run does not patch code, does not target production, and does not claim coverage.

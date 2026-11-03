# Exceptions

An *exception* lets a maintainer deliberately accept a known finding for a
bounded time, without weakening the control-plane verdict. Exceptions are a
property of the QA operator, never of the project under test.

## Where they live

Exceptions live in a file in the QA repository or an operator config directory,
**never inside the project under test**. This is the same guarantee that keeps a
tested project from changing the effective config: a project cannot add or widen
its own exceptions.

By default the file is resolved by the tested project's directory name:

```
exceptions/<project-name>.yaml
```

An operator can point at an explicit file instead with `config.exceptions.file`
(relative paths resolve against the QA repository root), or move the whole
directory with `config.exceptions.dir`. A missing file simply means "no
exceptions" and is not an error.

## Entry shape

A file is either a top-level list of entries or a mapping with an `exceptions:`
list. Every entry needs:

| field       | required | meaning |
|-------------|----------|---------|
| `match`     | yes      | A finding **fingerprint** (see below). No line number, so it survives small edits. |
| `reason`    | yes      | Non-empty free text explaining why the finding is accepted. |
| `expires`   | yes      | ISO date at most `max_days` (default **90**) days ahead. |
| `added_by`  | no       | Free text — who approved it. |
| `allow_p0`  | no       | Only when `true` may a **P0** finding be excepted. |

```yaml
exceptions:
  - match: "lexi:drift:docs/api.md:transfer"
    reason: "Contract redesign next sprint; tracked in OPS-1234."
    expires: 2026-12-31
    added_by: qa-oncall
```

## Fingerprint

Every finding in `findings.json` carries a stable `fingerprint` field a human can
copy straight into the exceptions file:

```
agent:type:file:term
```

`term` is the durable identity of the finding within its file — a symbol or doc
path for Lexi, a scenario id for Aria, a test nodeid for Riley and Nova, a policy
check key for Sable, or a normalised slug when only free text exists. The **line
number is deliberately excluded**, so reformatting or moving a line does not
silently break a match. A missing part is written as `-`.

## Effect

* A matching, **unexpired** exception turns the finding into a P3 / **INFO** note
  and records `excepted: <reason>` in `findings.json` and `findings.md`.
* An **expired** exception has no effect and produces a `WARN "exception expired:
  <match>"` so the operator renews or removes it.
* An **invalid** entry (missing/empty `reason`, missing/unparseable `expires`,
  `expires` more than `max_days` ahead, empty `match`) is a **startup config
  error** (`exceptions config error: ...`, exit code 1). It is never silently
  ignored, so a typo can never quietly disable a guard.

## P0 findings

A P0 is never excepted silently. It is only downgraded when the matching entry
sets `allow_p0: true`, and every excepted P0 is listed at the **top** of
`findings.md` under "Excepted P0 findings", with its fingerprint, reason, expiry
and `added_by`.

## Exceptions never change the verdict

Exceptions re-label the *findings projection* only. The control-plane verdict
(CLEAR / VETO / BLOCKED_ENV) is decided by the roles and is never softened here —
an excepted finding still cannot turn a VETO into a pass, and an unexcepted P0/P1
with evidence still vetoes exactly as before.

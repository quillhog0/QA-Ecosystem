# TypeScript and JavaScript Support

QA Ecosystem v0.4.0 adds full, first-class support for TypeScript and JavaScript repositories alongside Python, including mixed/polyglot repositories.

## 1. Toolchain & Language Detection

Detection is automatic and zero-config:
- **TypeScript / JavaScript:** Detected when `package.json` is present.
- **Python:** Detected when `pyproject.toml`, `setup.py`, `setup.cfg`, or `requirements*.txt` is present.
- **Polyglot (Both):** When both manifests exist, both runners execute in isolation and their findings, test results, and execution metrics are merged into a unified report.

### Node Engine Verification
Before executing, QA Ecosystem verifies that the environment satisfies the project's runtime requirements:
- `engines.node` declared in `package.json`.
- `.nvmrc` in the repository root.
If the active Node.js runtime does not satisfy the specified semver range, the run stops with `BLOCKED_ENV` (exit code 2) and records an environmental explanation rather than penalizing the project.

## 2. Package Managers & Frozen Lockfiles

Dependencies are installed before tests are run, respecting frozen lockfiles:
- `package-lock.json` &rarr; `npm ci`
- `pnpm-lock.yaml` &rarr; `pnpm install --frozen-lockfile`
- `yarn.lock` &rarr; `yarn install --frozen-lockfile`
- **Fallback:** If no lockfile exists, falls back to `npm install` and logs a `WARN` regarding the unpinned dependency state.

### Dependency Caching
Installed `node_modules` trees are content-addressed and cached under `~/.cache/qa-ecosystem/node_modules/<hash>` (where `<hash>` is derived from the lockfile digest). Repeated runs against identical lockfiles restore the cached dependencies in seconds.

## 3. Test Runners & Execution

The test runner is detected from `devDependencies` and `scripts.test`:
- `vitest` &rarr; runs `vitest run --reporter=verbose --reporter=junit --outputFile=...`
- `jest` &rarr; runs `jest --ci --reporters=default --reporters=jest-junit`
- `mocha` &rarr; runs `mocha --reporter mocha-junit-reporter`
- `node:test` &rarr; runs `node --test` with TAP parsing
- Fallback: `npm test`

### Typechecking
If `tsconfig.json` exists in the repository, `tsc --noEmit` is executed automatically. Any TypeScript compiler errors are registered as **P1** findings.

## 4. Test Quality Analysis (Riley TS)

Riley analyzes test files using the TypeScript compiler API AST to detect hollow, fake, or weakened tests:
- **P1 Findings (Veto):**
  - Tests containing no assertions.
  - Tautological assertions (e.g., `expect(true).toBe(true)`, `expect(1).toBe(1)`).
  - Assertions on mock calls or constants only, without checking actual outcomes.
  - Swallowed errors inside `catch` blocks without re-throwing or failing.
  - Committed `.only` calls (`describe.only`, `it.only`, `test.only`).
- **P2 Findings:**
  - Committed `.skip` or `.todo` tests.
  - Snapshot-only assertions without behavioral assertions.
  - Trivial assertions (e.g. `expect(result).toBeDefined()`).

## 5. Security & Policy Scanning (Sable TS)

Sable statically inspects test files for unsafe behavior:
- Spawning child processes (`child_process.exec`, `spawn`, `execSync`).
- Filesystem modifications outside temporary directories (`os.tmpdir()`).
- Unmocked network requests (`fetch`, `axios`, `http.request`, `WebSocket`).
- Secrets leaked into `process.env`.
- Dynamic code execution (`eval`, `new Function`).

When Bubblewrap (`bwrap`) sandboxing is active, security detections produce a `WARN`. When running unsandboxed, they immediately produce a `BLOCK` (`VETO`).

## 6. Documentation Claims (Lexi)

Lexi validates documented promises against TypeScript/JavaScript code:
- **CLI Commands and Flags:** Subcommands and options shown in docs (e.g. `cli sync --force`) must exist in CLI definitions (supporting Commander, Yargs, CAC, or `process.argv` parsing). Missing definitions produce a **P1** finding.
- **Public Call-Forms:** Methods called in docs examples (e.g. `client.fetch()`) must resolve to public exports declared in `package.json` (`main`, `exports`, `bin`). Missing symbols produce a **P1** finding.
- **Environment Variables:** Documented environment variables must be referenced in code via `process.env`.


#!/usr/bin/env node
/**
 * TS/JS test and policy analyzer using TypeScript Compiler API.
 * Detects weak test patterns (Riley) and safety risks (Sable).
 */

const fs = require("fs");
const path = require("path");

// Resolve typescript compiler API
let ts;
const homeDir = process.env.HOME || process.env.USERPROFILE || "";

function tryRequire(p) {
  try {
    return require(p);
  } catch (_) {
    return null;
  }
}

function resolveTypeScript() {
  if (ts) return ts;
  const toolingTs = process.env.QA_TOOLING_DIR
    ? path.join(process.env.QA_TOOLING_DIR, "typescript.js")
    : path.join(homeDir, ".cache", "qa-ecosystem", "tooling", "typescript.js");

  ts = tryRequire(toolingTs);
  if (ts) return ts;

  const vendorTs = path.join(__dirname, "vendor", "typescript.js");
  ts = tryRequire(vendorTs);
  if (ts) return ts;

  return null;
}

resolveTypeScript();

const SKIP_DIRS = new Set([
  "node_modules",
  "vendor",
  "dist",
  "build",
  ".git",
  ".venv",
  "venv",
  "coverage",
  "htmlcov",
  "reports",
  ".pytest_cache",
  "fixtures",
  "test_fixtures",
  "benchmark",
]);

const TEST_FILE_RE = /\.(?:test|spec)\.[cm]?[jt]sx?$|^test_.*\.py$|.*_test\.py$/;
const JS_TS_SUFFIX_RE = /\.[cm]?[jt]sx?$/;

function isTestPath(relPath) {
  const parts = relPath.split(path.sep).map((p) => p.toLowerCase());
  const base = path.basename(relPath).toLowerCase();
  if (parts.includes("tests") || parts.includes("test") || parts.includes("__tests__")) {
    return true;
  }
  return TEST_FILE_RE.test(base);
}

function collectSourceFiles(rootDir, onlyPaths = null) {
  const files = [];
  function walk(currentDir) {
    const rawEntries = fs.readdirSync(currentDir, { withFileTypes: true });
    const entries = rawEntries.slice().sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      if (entry.isDirectory()) {
        if (SKIP_DIRS.has(entry.name)) continue;
        walk(path.join(currentDir, entry.name));
      } else if (entry.isFile()) {
        const full = path.join(currentDir, entry.name);
        const rel = path.relative(rootDir, full).split(path.sep).join("/");
        if (!JS_TS_SUFFIX_RE.test(entry.name)) continue;
        if (onlyPaths && !onlyPaths.has(rel)) continue;
        files.push({ full, rel, isTest: isTestPath(rel) });
      }
    }
  }
  if (fs.existsSync(rootDir)) {
    walk(rootDir);
  }
  files.sort((a, b) => a.rel.localeCompare(b.rel));
  return files;
}

// Secret environment variable fragments
const SECRET_ENV_FRAGMENTS = [
  "SECRET",
  "TOKEN",
  "PASSWORD",
  "PASSWD",
  "API_KEY",
  "PRIVATE_KEY",
  "CREDENTIAL",
  "ACCESS_KEY",
  "AUTH",
];

function isSecretEnvName(name) {
  const upper = name.toUpperCase();
  return SECRET_ENV_FRAGMENTS.some((frag) => upper.includes(frag)) || upper.includes("KEY");
}

function collectDeclaredHelpers(sf) {
  const declaredHelpers = new Set();
  function visit(node) {
    if (
      ts.isFunctionDeclaration(node) ||
      ts.isFunctionExpression(node) ||
      ts.isArrowFunction(node)
    ) {
      let name = "";
      if (ts.isFunctionDeclaration(node) && node.name) {
        name = node.name.text;
      } else if (node.parent && ts.isVariableDeclaration(node.parent) && node.parent.name) {
        name = node.parent.name.getText(sf);
      }
      if (name && !name.startsWith("test") && !name.startsWith("it")) {
        let hasAssert = false;
        function findAssert(n) {
          if (ts.isCallExpression(n)) {
            const callee = n.expression.getText(sf);
            if (
              callee === "expect" ||
              callee.startsWith("expect(") ||
              callee.startsWith("assert") ||
              callee.includes("assert.")
            ) {
              hasAssert = true;
            }
          }
          if (!hasAssert) ts.forEachChild(n, findAssert);
        }
        findAssert(node);
        if (hasAssert) {
          declaredHelpers.add(name);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sf);
  return declaredHelpers;
}

function analyzeSourceFile(fileInfo, sf, allTestHelpers) {
  const weakTests = [];
  const sableFindings = [];
  const relPath = fileInfo.rel;

  let hasMockSetup = false;
  const declaredHelpers = collectDeclaredHelpers(sf);

  // Look for mock frameworks
  function checkMocks(node) {
    // Check mock setups: vi.mock, jest.mock, msw, nock
    if (ts.isCallExpression(node)) {
      const text = node.expression.getText(sf);
      if (
        text.includes("vi.mock") ||
        text.includes("jest.mock") ||
        text.includes("nock") ||
        text.includes("server.use") ||
        text.includes("mockResolvedValue") ||
        text.includes("mockReturnValue")
      ) {
        hasMockSetup = true;
      }
    }
    ts.forEachChild(node, checkMocks);
  }
  checkMocks(sf);

  // Helper check
  function isAssertionCall(callNode) {
    const text = callNode.expression.getText(sf);
    if (text === "expect" && callNode.parent && ts.isPropertyAccessExpression(callNode.parent)) {
      return false; // Receiver of .toBe(...) etc
    }
    if (
      text === "expect" ||
      text.startsWith("expect(") ||
      text.startsWith("assert") ||
      text.includes(".should") ||
      text.includes("should.")
    ) {
      return true;
    }
    const calleeName = callNode.expression.getText(sf).split(".")[0];
    if (declaredHelpers.has(calleeName) || allTestHelpers.has(calleeName)) {
      return true;
    }
    return false;
  }

  // Check test functions
  function checkTestCall(callNode) {
    const expr = callNode.expression;
    let fullCallName = expr.getText(sf); // e.g. test, it, test.only, it.skip
    let isTest = false;
    let isOnly = false;
    let isSkip = false;

    if (ts.isIdentifier(expr)) {
      const name = expr.text;
      if (name === "test" || name === "it") isTest = true;
      if (name === "xit" || name === "xtest") {
        isTest = true;
        isSkip = true;
      }
    } else if (ts.isPropertyAccessExpression(expr)) {
      const obj = expr.expression.getText(sf);
      const prop = expr.name.text;
      if (obj === "test" || obj === "it" || obj === "describe" || obj === "suite") {
        isTest = true;
        if (prop === "only") isOnly = true;
        if (prop === "skip" || prop === "todo") isSkip = true;
      }
    }

    if (!isTest) return;

    // Line number
    const { line } = sf.getLineAndCharacterOfPosition(callNode.getStart(sf));
    const lineNum = line + 1;

    // Test name
    let testName = "unnamed test";
    if (callNode.arguments.length > 0 && ts.isStringLiteralLike(callNode.arguments[0])) {
      testName = callNode.arguments[0].text;
    }

    const testId = `${relPath}::${testName}`;

    if (isOnly) {
      weakTests.push({
        test_id: testId,
        file: relPath,
        line: lineNum,
        name: testName,
        reason: "committed .only focus flag",
        priority: "P1",
      });
    }

    if (isSkip) {
      weakTests.push({
        test_id: testId,
        file: relPath,
        line: lineNum,
        name: testName,
        reason: "skipped test",
        priority: "P2",
      });
      return;
    }

    // Inspect callback
    const callback = callNode.arguments.find(
      (arg) => ts.isArrowFunction(arg) || ts.isFunctionExpression(arg)
    );
    if (!callback) {
      if (!isSkip && !isOnly) {
        weakTests.push({
          test_id: testId,
          file: relPath,
          line: lineNum,
          name: testName,
          reason: "no assertions",
          priority: "P1",
        });
      }
      return;
    }

    // Analyze body of callback
    let assertionCalls = [];
    let assertionsInsideSwallowedTry = [];
    let assertionsOutsideSwallowedTry = [];
    let swallowedTryStatements = [];
    let tautologyCount = 0;
    let snapshotCount = 0;
    let trivialObjectAssertCount = 0;
    let localVars = new Map(); // name -> { isConstant: bool, isMock: bool }
    let callsOutsideExpect = 0;
    let activeSwallowedTryDepth = 0;

    function isCatchSwallowing(catchClause) {
      if (!catchClause || !catchClause.block) return true;
      let rethrowsOrFails = false;
      function check(n) {
        if (ts.isThrowStatement(n)) rethrowsOrFails = true;
        if (ts.isCallExpression(n)) {
          const ct = n.expression.getText(sf);
          if (ct.includes("assert.fail") || ct.includes("expect.fail")) {
            rethrowsOrFails = true;
          }
        }
        if (!rethrowsOrFails) ts.forEachChild(n, check);
      }
      check(catchClause.block);
      return !rethrowsOrFails;
    }

    // Inspect statements
    function analyzeBody(node) {
      let enteredSwallowedTry = false;
      // Swallowed catch
      if (ts.isTryStatement(node)) {
        if (node.catchClause && isCatchSwallowing(node.catchClause)) {
          swallowedTryStatements.push(node);
          enteredSwallowedTry = true;
          activeSwallowedTryDepth++;
        }
      }

      // Promise .catch(() => {})
      if (ts.isCallExpression(node)) {
        if (ts.isPropertyAccessExpression(node.expression)) {
          if (node.expression.name.text === "catch") {
            const catchArg = node.arguments[0];
            if (catchArg && (ts.isArrowFunction(catchArg) || ts.isFunctionExpression(catchArg))) {
              const body = catchArg.body;
              if (ts.isBlock(body)) {
                if (body.statements.length === 0) swallowedTryStatements.push(node);
              }
            }
          }
        }
      }

      // Variable declaration tracking
      if (ts.isVariableDeclaration(node) && node.name && ts.isIdentifier(node.name)) {
        const vname = node.name.text;
        let isMock = false;
        let isConst = false;
        if (node.initializer) {
          const initText = node.initializer.getText(sf);
          if (initText.includes("vi.fn") || initText.includes("jest.fn")) isMock = true;
          if (ts.isLiteralExpression(node.initializer) || node.initializer.kind === ts.SyntaxKind.TrueKeyword || node.initializer.kind === ts.SyntaxKind.FalseKeyword) {
            isConst = true;
          }
        }
        localVars.set(vname, { isMock, isConst });
      }

      // Assertions check
      if (ts.isCallExpression(node)) {
        if (isAssertionCall(node)) {
          assertionCalls.push(node);
          if (activeSwallowedTryDepth > 0) {
            assertionsInsideSwallowedTry.push(node);
          } else {
            assertionsOutsideSwallowedTry.push(node);
          }
          const fullText = node.getText(sf);

          // Check tautology: expect(true).toBe(true), expect(1).toBe(1), expect(x).toBe(x)
          if (ts.isPropertyAccessExpression(node.expression)) {
            const matcher = node.expression.name.text;
            const parentCall = node.expression.expression;
            if (ts.isCallExpression(parentCall) && parentCall.expression.getText(sf) === "expect") {
              const expectArg = parentCall.arguments[0];
              const matcherArg = node.arguments[0];
              if (matcher === "toBe" || matcher === "toEqual" || matcher === "toStrictEqual") {
                if (expectArg && matcherArg) {
                  const expText = expectArg.getText(sf).trim();
                  const matText = matcherArg.getText(sf).trim();
                  if (expText === matText) {
                    tautologyCount++;
                  }
                }
              } else if (matcher === "toBeTruthy") {
                if (expectArg && (expectArg.kind === ts.SyntaxKind.TrueKeyword || expectArg.getText(sf) === "true")) {
                  tautologyCount++;
                }
              } else if (matcher === "toBeFalsy") {
                if (expectArg && (expectArg.kind === ts.SyntaxKind.FalseKeyword || expectArg.getText(sf) === "false")) {
                  tautologyCount++;
                }
              } else if (matcher === "toBeDefined") {
                if (expectArg) {
                  const argText = expectArg.getText(sf);
                  const varInfo = localVars.get(argText);
                  if (varInfo && (varInfo.isConst || varInfo.isMock)) {
                    trivialObjectAssertCount++;
                  } else if (expectArg.kind === ts.SyntaxKind.ObjectLiteralExpression) {
                    trivialObjectAssertCount++;
                  }
                }
              }

              if (matcher.includes("Snapshot")) {
                snapshotCount++;
              }
            }
          }
        } else {
          // A call that is NOT an assertion call
          const cText = node.expression.getText(sf);
          if (!cText.startsWith("describe") && !cText.startsWith("before") && !cText.startsWith("after")) {
            callsOutsideExpect++;
          }
        }
      }

      ts.forEachChild(node, analyzeBody);
      if (enteredSwallowedTry) {
        activeSwallowedTryDepth--;
      }
    }
    analyzeBody(callback);

    const hasSwallowedCatch = (
      swallowedTryStatements.length > 0 &&
      assertionsOutsideSwallowedTry.length === 0 &&
      (assertionsInsideSwallowedTry.length > 0 || assertionCalls.length === 0)
    );

    if (hasSwallowedCatch) {
      weakTests.push({
        test_id: testId,
        file: relPath,
        line: lineNum,
        name: testName,
        reason: "assertion failures swallowed by try/catch",
        priority: "P1",
      });
      return;
    }

    if (assertionCalls.length === 0) {
      weakTests.push({
        test_id: testId,
        file: relPath,
        line: lineNum,
        name: testName,
        reason: "no assertions",
        priority: "P1",
      });
      return;
    }

    if (tautologyCount > 0 && tautologyCount === assertionCalls.length) {
      weakTests.push({
        test_id: testId,
        file: relPath,
        line: lineNum,
        name: testName,
        reason: "tautology assertion",
        priority: "P1",
      });
      return;
    }

    if (snapshotCount > 0 && snapshotCount === assertionCalls.length) {
      weakTests.push({
        test_id: testId,
        file: relPath,
        line: lineNum,
        name: testName,
        reason: "snapshot-only test",
        priority: "P2",
      });
      return;
    }

    if (trivialObjectAssertCount > 0 && trivialObjectAssertCount === assertionCalls.length && callsOutsideExpect === 0) {
      weakTests.push({
        test_id: testId,
        file: relPath,
        line: lineNum,
        name: testName,
        reason: "only trivial assertions",
        priority: "P2",
      });
      return;
    }
  }

  // Sable security pass
  function checkSable(node) {
    const { line } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
    const lineNum = line + 1;

    // 1. child_process
    if (ts.isImportDeclaration(node) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      const mod = node.moduleSpecifier.text;
      if (mod === "child_process" || mod === "node:child_process") {
        sableFindings.push({
          file: relPath,
          line: lineNum,
          call: "child_process",
          category: "child_process",
          reason: "child_process module imported",
          is_test: fileInfo.isTest,
        });
      }
    }
    if (ts.isCallExpression(node)) {
      const exprText = node.expression.getText(sf);
      if (
        exprText === "exec" ||
        exprText === "execSync" ||
        exprText === "spawn" ||
        exprText === "spawnSync" ||
        exprText === "execFile" ||
        exprText.startsWith("child_process.")
      ) {
        sableFindings.push({
          file: relPath,
          line: lineNum,
          call: exprText,
          category: "child_process",
          reason: `child_process execution call: ${exprText}`,
          is_test: fileInfo.isTest,
        });
      }
    }

    // 2. fs writes outside os.tmpdir()
    if (ts.isCallExpression(node)) {
      const callText = node.expression.getText(sf);
      const fsWriteMethods = [
        "writeFile", "writeFileSync", "appendFile", "appendFileSync",
        "createWriteStream", "mkdir", "mkdirSync", "rm", "rmSync",
        "rmdir", "rmdirSync", "unlink", "unlinkSync",
      ];
      for (const m of fsWriteMethods) {
        if (callText === m || callText === `fs.${m}` || callText === `fsPromises.${m}` || callText === `promises.${m}`) {
          // Check argument for tmp
          let isTmp = false;
          if (node.arguments.length > 0) {
            const argText = node.arguments[0].getText(sf).toLowerCase();
            if (argText.includes("tmpdir") || argText.includes("tmp") || argText.includes("temp")) {
              isTmp = true;
            }
          }
          if (!isTmp) {
            sableFindings.push({
              file: relPath,
              line: lineNum,
              call: callText,
              category: "fs_write",
              reason: `fs write outside os.tmpdir(): ${callText}`,
              is_test: fileInfo.isTest,
            });
          }
        }
      }
    }

    // 3. Real network without mock
    if (ts.isCallExpression(node)) {
      const callText = node.expression.getText(sf);
      if (
        callText === "fetch" ||
        callText === "axios" ||
        callText.startsWith("axios.") ||
        callText === "http.request" ||
        callText === "http.get" ||
        callText === "https.request" ||
        callText === "https.get"
      ) {
        if (!hasMockSetup) {
          sableFindings.push({
            file: relPath,
            line: lineNum,
            call: callText,
            category: "network",
            reason: `real network call without mock: ${callText}`,
            is_test: fileInfo.isTest,
          });
        }
      }
    }
    if (ts.isNewExpression(node)) {
      const text = node.expression.getText(sf);
      if (text === "WebSocket" || text === "ws") {
        if (!hasMockSetup) {
          sableFindings.push({
            file: relPath,
            line: lineNum,
            call: `new ${text}`,
            category: "network",
            reason: `WebSocket connection without mock: new ${text}`,
            is_test: fileInfo.isTest,
          });
        }
      }
    }

    // 4. process.env secret reads
    if (ts.isPropertyAccessExpression(node)) {
      const full = node.getText(sf);
      if (full.startsWith("process.env.")) {
        const prop = node.name.text;
        if (isSecretEnvName(prop)) {
          sableFindings.push({
            file: relPath,
            line: lineNum,
            call: full,
            category: "secret_env",
            reason: `process.env reads secret-looking variable: ${prop}`,
            is_test: fileInfo.isTest,
          });
        }
      }
    } else if (ts.isElementAccessExpression(node)) {
      const expr = node.expression.getText(sf);
      if (expr === "process.env" && node.argumentExpression && ts.isStringLiteral(node.argumentExpression)) {
        const prop = node.argumentExpression.text;
        if (isSecretEnvName(prop)) {
          sableFindings.push({
            file: relPath,
            line: lineNum,
            call: `process.env['${prop}']`,
            category: "secret_env",
            reason: `process.env reads secret-looking variable: ${prop}`,
            is_test: fileInfo.isTest,
          });
        }
      }
    }

    // 5. eval / new Function
    if (ts.isCallExpression(node) && node.expression.getText(sf) === "eval") {
      sableFindings.push({
        file: relPath,
        line: lineNum,
        call: "eval",
        category: "eval",
        reason: "eval invocation",
        is_test: fileInfo.isTest,
      });
    }
    if (ts.isNewExpression(node) && node.expression.getText(sf) === "Function") {
      sableFindings.push({
        file: relPath,
        line: lineNum,
        call: "new Function",
        category: "eval",
        reason: "new Function invocation",
        is_test: fileInfo.isTest,
      });
    }

    ts.forEachChild(node, checkSable);
  }

  // Run test checker on test files
  if (fileInfo.isTest) {
    function visitTest(n) {
      if (ts.isCallExpression(n)) {
        checkTestCall(n);
      }
      ts.forEachChild(n, visitTest);
    }
    visitTest(sf);
  }

  // Run security checker
  checkSable(sf);

  return { weakTests, sableFindings, helpers: declaredHelpers };
}

function main() {
  const args = process.argv.slice(2);
  let projectDir = process.cwd();
  let onlyPaths = null;

  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--project" && i + 1 < args.length) {
      projectDir = path.resolve(args[++i]);
    } else if (args[i] === "--only-paths" && i + 1 < args.length) {
      onlyPaths = new Set(args[++i].split(",").map((s) => s.trim()).filter(Boolean));
    }
  }

  if (!resolveTypeScript()) {
    console.error("TypeScript compiler API not found in QA tooling or vendor");
    process.exit(1);
  }

  const files = collectSourceFiles(projectDir, onlyPaths);
  const allWeakTests = [];
  const allSableFindings = [];
  const globalHelpers = new Set();

  // Pass 1: Parse all files and collect declared assertion helpers across all files
  const parsedFiles = [];
  for (const f of files) {
    try {
      const code = fs.readFileSync(f.full, "utf-8");
      const sf = ts.createSourceFile(f.rel, code, ts.ScriptTarget.Latest, true);
      parsedFiles.push({ fileInfo: f, sf });
      const helpers = collectDeclaredHelpers(sf);
      for (const h of helpers) globalHelpers.add(h);
    } catch (err) {
      // ignore read/parse errors
    }
  }

  // Pass 2: Analyze each file using the complete set of globalHelpers
  for (const { fileInfo, sf } of parsedFiles) {
    const { weakTests, sableFindings } = analyzeSourceFile(
      fileInfo,
      sf,
      globalHelpers
    );
    allWeakTests.push(...weakTests);
    allSableFindings.push(...sableFindings);
  }

  const output = {
    weak_tests: allWeakTests,
    sable_findings: allSableFindings,
  };

  console.log(JSON.stringify(output, null, 2));
}

if (require.main === module) {
  main();
}

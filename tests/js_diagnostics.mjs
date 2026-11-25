import test from "node:test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const PROJECT_ROOT = path.resolve(__dirname, "..");
const LOG_DIR = path.join(__dirname, "logs");
export const JS_LOG_FILE = path.join(LOG_DIR, "frontend_failures.log");
export const JS_JSON_FILE = path.join(LOG_DIR, "frontend_failure_report.json");

const _runStartedAt = new Date().toISOString();
const _failures = [];

export function resetFrontendLogs() {
  fs.mkdirSync(LOG_DIR, { recursive: true });
  _failures.length = 0;
  fs.writeFileSync(
    JS_LOG_FILE,
    `ClassPulse Frontend JS Failure Diagnostics Log\nRun Started: ${_runStartedAt}\n${"=".repeat(78)}\n`,
    "utf8"
  );
  fs.writeFileSync(
    JS_JSON_FILE,
    JSON.stringify({ run_started_at: _runStartedAt, total_failures: 0, failures: [] }, null, 2),
    "utf8"
  );
}

resetFrontendLogs();

function _resolveFsPath(rawPath) {
  const s = String(rawPath || "");
  if (s.startsWith("file://")) {
    try {
      return fileURLToPath(s);
    } catch {
      return decodeURIComponent(s.replace(/^file:\/\/\//, ""));
    }
  }
  return s;
}

function _toRel(rawPath) {
  const abs = _resolveFsPath(rawPath);
  try {
    return path.relative(PROJECT_ROOT, abs).replace(/\\/g, "/");
  } catch {
    return String(abs || "");
  }
}

function _parseStackLocation(stack) {
  const frames = [];
  for (const rawLine of String(stack || "").split(/\r?\n/)) {
    const m = rawLine.match(/\(?((?:file:\/\/\/)?[A-Za-z]?:?[^():\n]+?\.(?:mjs|js)):(\d+):(\d+)\)?/);
    if (!m) continue;
    const rawFile = m[1];
    if (rawFile.includes("node:internal") || rawFile.includes("js_diagnostics.mjs")) continue;
    const absFile = _resolveFsPath(rawFile);
    const rel = _toRel(absFile);
    frames.push({
      file: rel,
      absFile,
      line: Number(m[2]),
      col: Number(m[3]),
    });
  }
  const testFrame = frames.find((f) => f.file.startsWith("tests/")) || frames[0] || null;
  const appFrame = frames.find((f) => f.file.startsWith("app/")) || null;
  return { primary: testFrame, appFrame };
}

function _readSnippet(absFile, targetLine, radius = 3) {
  if (!absFile || !targetLine) return [];
  let fixed = absFile;
  if (!fs.existsSync(fixed) && /^[A-Za-z]:/.test(fixed) === false && fixed.startsWith("/")) {
    fixed = fixed.slice(1);
  }
  if (!fs.existsSync(fixed)) return [];
  const lines = fs.readFileSync(fixed, "utf8").split(/\r?\n/);
  const start = Math.max(1, targetLine - radius);
  const end = Math.min(lines.length, targetLine + radius);
  const out = [];
  for (let ln = start; ln <= end; ln += 1) {
    out.push({ line: ln, is_target: ln === targetLine, code: lines[ln - 1] ?? "" });
  }
  return out;
}

function _classifyJsError(err, testName, appFrame, primary) {
  const name = String((err && err.name) || "Error");
  const msg = String((err && err.message) || err || "");
  const code = String((err && err.code) || "");
  const loc = primary ? `${primary.file}:${primary.line}` : "tests/test_frontend_js.mjs";
  const culprit = appFrame ? `${appFrame.file}:${appFrame.line}` : loc;

  if (code === "ERR_ASSERTION" || name.includes("AssertionError")) {
    return {
      category: "Assertion Mismatch",
      summary: `Frontend assertion failed (${err.operator || "assert"}): ${msg.split(/\r?\n/)[0]}`,
      likely_culprit: culprit,
    };
  }
  if (/timeout|timed out|abort/i.test(msg)) {
    return {
      category: "Timeout / Async Hang",
      summary: `Async operation timed out or aborted unexpectedly: ${msg.split(/\r?\n/)[0]}`,
      likely_culprit: culprit,
    };
  }
  if (name === "TypeError" || name === "ReferenceError") {
    return {
      category: "Missing Key / Schema Error",
      summary: `${name} encountered in JS helper or test DOM stub: ${msg.split(/\r?\n/)[0]}`,
      likely_culprit: culprit,
    };
  }
  return {
    category: `Unhandled ${name}`,
    summary: `Exception in JS test '${testName}': ${msg.split(/\r?\n/)[0]}`,
    likely_culprit: culprit,
  };
}

function _formatJsBlock(record) {
  const bar = "=".repeat(78);
  const thin = "-".repeat(78);
  const lines = [
    bar,
    `[FRONTEND JS FAILURE DIAGNOSTIC] ${record.test_name}`,
    thin,
    `Location  : ${record.location}`,
  ];
  if (record.app_culprit) {
    lines.push(`App Frame : ${record.app_culprit}`);
  }
  lines.push(`Exception : ${record.exception_type}: ${record.error_message.split(/\r?\n/)[0]}`);

  if (record.expected !== undefined || record.actual !== undefined) {
    lines.push("");
    lines.push("Expected vs. Actual:");
    lines.push(`  Operator: ${record.operator || "assert"}`);
    if (record.expected !== undefined) lines.push(`  Expected: ${record.expected}`);
    if (record.actual !== undefined) lines.push(`  Actual  : ${record.actual}`);
  }

  if (record.code_snippet.length > 0) {
    lines.push("");
    lines.push("Code Snippet:");
    for (const row of record.code_snippet) {
      const marker = row.is_target ? ">>>" : "   ";
      lines.push(`  ${marker} ${String(row.line).padStart(4, " ")} | ${row.code}`);
    }
  }

  lines.push("");
  lines.push("Root-Cause Analysis:");
  lines.push(`  Category      : ${record.root_cause_analysis.category}`);
  lines.push(`  Summary       : ${record.root_cause_analysis.summary}`);
  lines.push(`  Likely Culprit: ${record.root_cause_analysis.likely_culprit}`);
  lines.push(bar);
  return lines.join("\n");
}

export function diagnosticTest(name, fn) {
  return test(name, async (t) => {
    try {
      await fn(t);
    } catch (err) {
      const { primary, appFrame } = _parseStackLocation(err && err.stack);
      const location = primary ? `${primary.file}:${primary.line}:${primary.col}` : "unknown:0";
      const snippet = primary ? _readSnippet(primary.absFile, primary.line) : [];
      const rca = _classifyJsError(err, name, appFrame, primary);
      const hasExpected = err && Object.prototype.hasOwnProperty.call(err, "expected");
      const hasActual = err && Object.prototype.hasOwnProperty.call(err, "actual");
      const record = {
        timestamp: new Date().toISOString(),
        test_name: name,
        file: primary ? primary.file : "unknown",
        line: primary ? primary.line : 0,
        location,
        app_culprit: appFrame ? `${appFrame.file}:${appFrame.line}` : "",
        code_snippet: snippet,
        exception_type: String((err && err.name) || "Error"),
        error_message: String((err && err.message) || err || ""),
        operator: err && err.operator ? String(err.operator) : "",
        expected: hasExpected ? JSON.stringify(err.expected) : undefined,
        actual: hasActual ? JSON.stringify(err.actual) : undefined,
        root_cause_analysis: rca,
      };
      _failures.push(record);
      const block = _formatJsBlock(record);
      console.error("\n" + block);
      fs.mkdirSync(LOG_DIR, { recursive: true });
      fs.appendFileSync(JS_LOG_FILE, block + "\n\n", "utf8");
      fs.writeFileSync(
        JS_JSON_FILE,
        JSON.stringify(
          {
            run_started_at: _runStartedAt,
            total_failures: _failures.length,
            failures: _failures,
          },
          null,
          2
        ),
        "utf8"
      );
      throw err;
    }
  });
}

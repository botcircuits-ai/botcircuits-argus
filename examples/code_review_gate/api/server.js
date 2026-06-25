// Mock code-quality scan API for the `code_review_gate` workflow example.
//
// Zero-dependency Node.js HTTP server. Run with: node server.js
//
// Simulates the kind of signals a PR merge gate inspects before deciding
// whether each changed file is mergeable as-is: lint errors/warnings,
// security findings (count + worst severity), clone/duplicate detection,
// cyclomatic complexity, lines changed, and test-coverage delta. It also
// exposes a global scanner-status endpoint used by the pre-flight gate.
//
// Endpoints:
//   GET /v1/scan?file=<path>   -> per-file static-analysis/lint/security scan
//   GET /v1/scanner-status     -> global code-quality-service status
//
// The response for /v1/scan is chosen from the file path's prefix (the
// leading path segment before the first `/`) so every branch of the
// workflow can be exercised deterministically:
//
//   clean_...  -> no findings, low complexity, no coverage drop  -> approve
//   style_...  -> lint warnings over budget + coverage drop       -> review
//   warn_...   -> lint warnings over budget + coverage drop       -> review
//   sec_...    -> a HIGH severity security finding                -> needs_fix
//   crit_...   -> a CRITICAL security finding (and lint errors)   -> block
//   dup_...    -> flagged as a clone of another file               -> needs_fix
//   big_...    -> huge diff (lines_changed over budget)            -> large_diff
//   (anything else / unknown prefix)                               -> 404 not_found
//
// Special trigger:
//   FAIL_...   -> server replies 500 (simulate a scanner-backend outage)

const http = require("http");
const { URL } = require("url");

const PORT = process.env.PORT || 4300;

// Flip the global code-quality-service status via env so the pre-flight
// maintenance-mode branch is testable:
//   SCANNER_DOWN=1 node server.js   -> /v1/scanner-status reports an outage
const SCANNER_DOWN = process.env.SCANNER_DOWN === "1";

// Map a changed-file prefix to a mock scan record.
function recordFor(filePath) {
  const segment = filePath.split("/")[0];
  const prefix = segment.split(/[-_]/)[0].toLowerCase();

  switch (prefix) {
    case "clean":
      return {
        lines_changed: 24,
        complexity_score: 4,
        lint_errors: 0,
        lint_warnings: 0,
        security_findings: 0,
        worst_security_severity: "",
        is_duplicate_of: "",
        test_coverage_delta: 0.5,
        has_baseline: true,
      };
    case "style":
      return {
        lines_changed: 60,
        complexity_score: 7,
        lint_errors: 0,
        lint_warnings: 9, // over the 5-warning budget
        security_findings: 0,
        worst_security_severity: "",
        is_duplicate_of: "",
        test_coverage_delta: -1.2, // coverage dropped
        has_baseline: true,
      };
    case "warn":
      return {
        lines_changed: 48,
        complexity_score: 6,
        lint_errors: 0,
        lint_warnings: 11, // over the 5-warning budget
        security_findings: 0,
        worst_security_severity: "",
        is_duplicate_of: "",
        test_coverage_delta: -2.4, // coverage dropped
        has_baseline: true,
      };
    case "sec":
      return {
        lines_changed: 32,
        complexity_score: 9,
        lint_errors: 0,
        lint_warnings: 1,
        security_findings: 1,
        worst_security_severity: "high", // over budget => needs_fix
        is_duplicate_of: "",
        test_coverage_delta: 0.0,
        has_baseline: true,
      };
    case "crit":
      return {
        lines_changed: 18,
        complexity_score: 11,
        lint_errors: 2,
        lint_warnings: 3,
        security_findings: 1,
        worst_security_severity: "critical", // => block
        is_duplicate_of: "",
        test_coverage_delta: -0.3,
        has_baseline: true,
      };
    case "dup":
      return {
        lines_changed: 40,
        complexity_score: 5,
        lint_errors: 0,
        lint_warnings: 2,
        security_findings: 0,
        worst_security_severity: "",
        is_duplicate_of: "src/utils/format.py", // clone detected => needs_fix
        test_coverage_delta: 0.0,
        has_baseline: false,
      };
    case "big":
      return {
        lines_changed: 880, // over the 400-line "huge diff" budget
        complexity_score: 13,
        lint_errors: 0,
        lint_warnings: 2,
        security_findings: 0,
        worst_security_severity: "",
        is_duplicate_of: "",
        test_coverage_delta: 0.2,
        has_baseline: true,
      };
    default:
      return null; // => 404 not_found
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Global code-quality-service status.
  if (req.method === "GET" && url.pathname === "/v1/scanner-status") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        online: !SCANNER_DOWN,
        reason: SCANNER_DOWN
          ? "Code-quality service is in maintenance mode."
          : "",
      })
    );
    return;
  }

  if (req.method !== "GET" || url.pathname !== "/v1/scan") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not_found", message: "Unknown route." }));
    return;
  }

  const file = (url.searchParams.get("file") || "").trim();

  if (!file) {
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "missing_parameter",
        message: "Query parameter `file` is required.",
      })
    );
    return;
  }

  // Simulate a scanner-backend outage so the workflow's failure branch is testable.
  if (file.split("/")[0].slice(0, 4).toUpperCase() === "FAIL") {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "upstream_error",
        message: "Code-quality scanner backend unavailable.",
      })
    );
    return;
  }

  const record = recordFor(file);

  if (!record) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "not_found",
        message: `File ${file} is not present in the scan catalog.`,
        file,
      })
    );
    return;
  }

  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ file, ...record }));
});

server.listen(PORT, () => {
  console.log(`Mock code-quality scan API listening on http://localhost:${PORT}`);
  console.log(`Scanner online: ${!SCANNER_DOWN}`);
  console.log(`Try: http://localhost:${PORT}/v1/scan?file=clean_src/utils/format.py`);
});

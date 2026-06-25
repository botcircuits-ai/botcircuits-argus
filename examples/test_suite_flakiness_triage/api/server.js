// Mock test-analytics API for the `test_suite_flakiness_triage` workflow example.
//
// Zero-dependency Node.js HTTP server. Run with: node server.js
//
// Simulates the kind of signals a flakiness-triage process inspects before
// deciding what to do with each test in the suite: its historical pass/fail
// counts, computed flakiness rate, whether it's already quarantined, its
// failure pattern (timing/race-condition, environment-specific, consistently
// failing, or none), whether failures are runner-specific, and whether it has
// enough run history to judge at all. It also exposes a global test-analytics
// pipeline status endpoint (freshness + outage) used by the pre-flight gate.
//
// Endpoints:
//   GET /v1/testhealth?test=<test_id>   -> per-test historical pass/fail signal
//   GET /v1/analytics-status            -> global pipeline freshness + outage status
//
// The response for /v1/testhealth is chosen from the test id's prefix so every
// branch of the workflow can be exercised deterministically:
//
//   stable_...      -> low flakiness, not quarantined              -> stable
//   flaky_...       -> flakiness rate over threshold, not quarantined -> quarantine_new
//   quarantined_...  -> currently quarantined, now stable           -> release_from_quarantine
//   timing_...       -> consistent race-condition failure pattern   -> timing_issue
//   envdep_...       -> fails only on specific runners               -> environment_issue
//   newtest_...      -> no run history yet                          -> needs_baseline
//   alwaysfail_...    -> consistently failing (real bug, not flaky)  -> real_bug
//   (anything else / unknown prefix)                                -> 404 not_found
//
// Special trigger:
//   FAIL_...  -> server replies 500 (simulate a test-analytics backend outage)

const http = require("http");
const { URL } = require("url");

const PORT = process.env.PORT || 4700;

// Flip the global analytics pipeline status via env so the pre-flight abort
// branches are testable:
//   ANALYTICS_DOWN=1 node server.js  -> /v1/analytics-status reports an outage
//   STALE_DATA=1 node server.js      -> /v1/analytics-status reports stale data
const ANALYTICS_DOWN = process.env.ANALYTICS_DOWN === "1";
const STALE_DATA = process.env.STALE_DATA === "1";

// Map a test-id prefix to a mock test-health record.
function healthFor(testId) {
  const prefix = testId.split(/[-_:]/)[0].toLowerCase();

  switch (prefix) {
    case "stable":
      return {
        has_history: true,
        total_runs: 120,
        pass_count: 119,
        fail_count: 1,
        flakiness_rate: 0.8, // percent, well under threshold
        currently_quarantined: false,
        failure_pattern: "none",
        runner_specific_failures: false,
      };
    case "flaky":
      return {
        has_history: true,
        total_runs: 120,
        pass_count: 96,
        fail_count: 24,
        flakiness_rate: 20.0, // percent, over the 8% threshold
        currently_quarantined: false,
        failure_pattern: "intermittent",
        runner_specific_failures: false,
      };
    case "quarantined":
      return {
        has_history: true,
        total_runs: 80,
        pass_count: 78,
        fail_count: 2,
        flakiness_rate: 2.5, // now stable, below threshold => release
        currently_quarantined: true,
        failure_pattern: "none",
        runner_specific_failures: false,
      };
    case "timing":
      return {
        has_history: true,
        total_runs: 100,
        pass_count: 82,
        fail_count: 18,
        flakiness_rate: 18.0,
        currently_quarantined: false,
        failure_pattern: "timing", // race-condition pattern => timing_issue
        runner_specific_failures: false,
      };
    case "envdep":
      return {
        has_history: true,
        total_runs: 100,
        pass_count: 88,
        fail_count: 12,
        flakiness_rate: 12.0,
        currently_quarantined: false,
        failure_pattern: "environment", // fails only on certain runners
        runner_specific_failures: true,
      };
    case "newtest":
      return {
        has_history: false, // just added, no baseline yet => needs_baseline
        total_runs: 2,
        pass_count: 2,
        fail_count: 0,
        flakiness_rate: 0.0,
        currently_quarantined: false,
        failure_pattern: "none",
        runner_specific_failures: false,
      };
    case "alwaysfail":
      return {
        has_history: true,
        total_runs: 60,
        pass_count: 0,
        fail_count: 60,
        flakiness_rate: 0.0, // consistent, not flaky -> real bug
        currently_quarantined: false,
        failure_pattern: "consistent",
        runner_specific_failures: false,
      };
    default:
      return null; // => 404 not_found
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Global test-analytics pipeline status (freshness + outage).
  if (req.method === "GET" && url.pathname === "/v1/analytics-status") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        outage: ANALYTICS_DOWN,
        data_freshness_hours: STALE_DATA ? 26 : 1,
        reason: ANALYTICS_DOWN
          ? "Test-analytics backend is currently down for maintenance."
          : STALE_DATA
          ? "Analytics warehouse ETL has not run within the freshness window."
          : "",
      })
    );
    return;
  }

  if (req.method !== "GET" || url.pathname !== "/v1/testhealth") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not_found", message: "Unknown route." }));
    return;
  }

  const testId = (url.searchParams.get("test") || "").trim();

  if (!testId) {
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "missing_parameter",
        message: "Query parameter `test` is required.",
      })
    );
    return;
  }

  // Simulate a test-analytics backend outage so the workflow's failure branch
  // is testable.
  if (testId.slice(0, 4).toUpperCase() === "FAIL") {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "upstream_error",
        message: "Test-analytics backend unavailable.",
      })
    );
    return;
  }

  const health = healthFor(testId);

  if (!health) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "not_found",
        message: `Test ${testId} is unknown to the test-analytics service.`,
        test: testId,
      })
    );
    return;
  }

  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ test: testId, ...health }));
});

server.listen(PORT, () => {
  console.log(`Mock test-analytics API listening on http://localhost:${PORT}`);
  console.log(`Analytics outage: ${ANALYTICS_DOWN}`);
  console.log(`Stale data: ${STALE_DATA}`);
  console.log(`Try: http://localhost:${PORT}/v1/testhealth?test=stable_checkout::test_apply_discount_code`);
});

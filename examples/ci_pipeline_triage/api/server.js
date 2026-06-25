// Mock CI metrics API for the `ci_pipeline_triage` workflow.
//
// Zero-dependency Node.js HTTP server. Run with: node server.js
//
// Simulates the kind of signals an overnight CI triage sweep inspects for each
// failing job/build: the failure reason, exit code, duration, retry count,
// historical flakiness rate, whether it's an infra fault vs. a code problem,
// whether it OOM'd, and whether the pipeline has a failure-history baseline at
// all (brand-new pipelines don't).
//
// Endpoints:
//   GET /v1/job?id=<job_id>   -> per-job CI failure metrics
//   GET /v1/fleet             -> runner fleet capacity / maintenance status
//   GET /v1/status            -> global CI provider backend status
//
// The response for /v1/job is chosen from the job id's prefix so every branch
// of the workflow can be exercised deterministically:
//
//   flaky_...    -> high historical_failure_rate, not a compile error -> flaky
//   compile_...  -> deterministic compile/build failure                -> compile_error
//   build_...    -> deterministic compile/build failure                -> build_failure
//   oom_...      -> memory_exceeded = true                              -> oom
//   timeout_...  -> failure_reason = "timeout"                         -> timeout
//   infra_...    -> is_infra_error = true                              -> infra_issue
//   lint_...     -> failure_reason = "lint"                            -> lint_failure
//   newpipe_...  -> has_baseline = false (no history yet)              -> needs_baseline
//   (anything else / unknown prefix)                                  -> 404 not_found
//
// Special trigger:
//   FAIL_...  -> server replies 500 (simulate a CI metrics-backend outage)

const http = require("http");
const { URL } = require("url");

const PORT = process.env.PORT || 4400;

// Flip the global CI provider outage status via env so the pre-flight abort
// branch is testable:
//   CI_DOWN=1 node server.js   -> /v1/status reports provider_down = true
const PROVIDER_DOWN = process.env.CI_DOWN === "1";

// Flip the runner fleet into maintenance / over-capacity via env so the
// pre-flight capacity abort branch is testable:
//   FLEET_DOWN=1 node server.js   -> /v1/fleet reports in_maintenance = true
const FLEET_DOWN = process.env.FLEET_DOWN === "1";

// Map a job-id prefix to a mock CI metrics record.
function metricsFor(jobId) {
  const prefix = jobId.split("_")[0].toLowerCase();

  switch (prefix) {
    case "flaky":
      return {
        failure_reason: "assertion_error",
        exit_code: 1,
        duration_seconds: 42,
        retry_count: 1,
        historical_failure_rate: 0.55, // over the 0.3 flaky threshold
        is_infra_error: false,
        memory_exceeded: false,
        has_baseline: true,
      };
    case "compile":
      return {
        failure_reason: "compile_error",
        exit_code: 2,
        duration_seconds: 18,
        retry_count: 0,
        historical_failure_rate: 0.02,
        is_infra_error: false,
        memory_exceeded: false,
        has_baseline: true,
      };
    case "build":
      return {
        failure_reason: "build_failure",
        exit_code: 2,
        duration_seconds: 25,
        retry_count: 0,
        historical_failure_rate: 0.01,
        is_infra_error: false,
        memory_exceeded: false,
        has_baseline: true,
      };
    case "oom":
      return {
        failure_reason: "out_of_memory",
        exit_code: 137,
        duration_seconds: 305,
        retry_count: 0,
        historical_failure_rate: 0.08,
        is_infra_error: false,
        memory_exceeded: true, // over memory limit => oom
        has_baseline: true,
      };
    case "timeout":
      return {
        failure_reason: "timeout",
        exit_code: 124,
        duration_seconds: 1800,
        retry_count: 1,
        historical_failure_rate: 0.12,
        is_infra_error: false,
        memory_exceeded: false,
        has_baseline: true,
      };
    case "infra":
      return {
        failure_reason: "runner_disconnected",
        exit_code: 1,
        duration_seconds: 9,
        retry_count: 0,
        historical_failure_rate: 0.05,
        is_infra_error: true, // CI infra fault, not a code problem
        memory_exceeded: false,
        has_baseline: true,
      };
    case "lint":
      return {
        failure_reason: "lint",
        exit_code: 1,
        duration_seconds: 6,
        retry_count: 0,
        historical_failure_rate: 0.03,
        is_infra_error: false,
        memory_exceeded: false,
        has_baseline: true,
      };
    case "newpipe":
      return {
        failure_reason: "assertion_error",
        exit_code: 1,
        duration_seconds: 30,
        retry_count: 0,
        historical_failure_rate: 0.0,
        is_infra_error: false,
        memory_exceeded: false,
        has_baseline: false, // brand new pipeline, no history yet => needs_baseline
      };
    default:
      return null; // => 404 not_found
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Runner fleet capacity / maintenance status.
  if (req.method === "GET" && url.pathname === "/v1/fleet") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        in_maintenance: FLEET_DOWN,
        online_runners: FLEET_DOWN ? 0 : 48,
        queue_depth: FLEET_DOWN ? 0 : 37,
        reason: FLEET_DOWN ? "Runner fleet is in scheduled maintenance." : "",
      })
    );
    return;
  }

  // Global CI provider backend status.
  if (req.method === "GET" && url.pathname === "/v1/status") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        provider_down: PROVIDER_DOWN,
        reason: PROVIDER_DOWN ? "CI provider backend outage in progress." : "",
      })
    );
    return;
  }

  if (req.method !== "GET" || url.pathname !== "/v1/job") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not_found", message: "Unknown route." }));
    return;
  }

  const jobId = (url.searchParams.get("id") || "").trim();

  if (!jobId) {
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "missing_parameter",
        message: "Query parameter `id` is required.",
      })
    );
    return;
  }

  // Simulate a CI metrics-backend outage so the workflow's failure branch is testable.
  if (jobId.slice(0, 4).toUpperCase() === "FAIL") {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "upstream_error",
        message: "CI metrics backend unavailable.",
      })
    );
    return;
  }

  const metrics = metricsFor(jobId);

  if (!metrics) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "not_found",
        message: `Job ${jobId} is not known to the CI metrics backend.`,
        job_id: jobId,
      })
    );
    return;
  }

  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ job_id: jobId, ...metrics }));
});

server.listen(PORT, () => {
  console.log(`Mock CI metrics API listening on http://localhost:${PORT}`);
  console.log(`Runner fleet in maintenance: ${FLEET_DOWN}`);
  console.log(`CI provider down: ${PROVIDER_DOWN}`);
  console.log(`Try: http://localhost:${PORT}/v1/job?id=flaky_checkout_e2e`);
});

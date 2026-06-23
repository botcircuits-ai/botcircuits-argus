// Mock DevOps deployment/health API for the `deployment_release_gate` workflow.
//
// Zero-dependency Node.js HTTP server. Run with: node server.js
//
// Simulates the kind of signals a release gate inspects before promoting a
// build of each microservice to production: the result of its latest CI
// pipeline, error rate, p95 latency, open SEV incidents, and whether the
// target environment is frozen for changes.
//
// Endpoints:
//   GET /v1/health?service=<name>   -> per-service deployment/health metrics
//   GET /v1/freeze                  -> global change-freeze window status
//
// The response for /v1/health is chosen from the service name's prefix so every
// branch of the workflow can be exercised deterministically:
//
//   ok-...    -> healthy, CI passed, low errors            -> promote
//   slow-...  -> CI passed but p95 latency over budget      -> hold (perf)
//   err-...   -> CI passed but error rate over budget       -> hold (errors)
//   ci-...    -> latest CI pipeline FAILED                  -> block
//   sev-...   -> an open SEV1/SEV2 incident on the service  -> block + page
//   new-...   -> never deployed before (no baseline)        -> canary
//   (anything else / unknown prefix)                        -> 404 not_found
//
// Special trigger:
//   FAIL-...  -> server replies 500 (simulate a metrics-backend outage)

const http = require("http");
const { URL } = require("url");

const PORT = process.env.PORT || 4100;

// Flip the change-freeze window via env so the freeze branch is testable:
//   FREEZE=1 node server.js   -> /v1/freeze reports an active freeze
const FREEZE_ACTIVE = process.env.FREEZE === "1";

// Map a service-name prefix to a mock metrics record.
function metricsFor(service) {
  const prefix = service.split(/[-_]/)[0].toLowerCase();

  switch (prefix) {
    case "ok":
      return {
        ci_status: "passed",
        error_rate: 0.2, // percent
        p95_latency_ms: 180,
        open_sev_incidents: 0,
        has_baseline: true,
        last_deploy_age_hours: 36,
      };
    case "slow":
      return {
        ci_status: "passed",
        error_rate: 0.4,
        p95_latency_ms: 950, // over the 500ms budget
        open_sev_incidents: 0,
        has_baseline: true,
        last_deploy_age_hours: 12,
      };
    case "err":
      return {
        ci_status: "passed",
        error_rate: 4.5, // over the 1% budget
        p95_latency_ms: 220,
        open_sev_incidents: 0,
        has_baseline: true,
        last_deploy_age_hours: 5,
      };
    case "ci":
      return {
        ci_status: "failed",
        error_rate: 0.1,
        p95_latency_ms: 160,
        open_sev_incidents: 0,
        has_baseline: true,
        last_deploy_age_hours: 72,
      };
    case "sev":
      return {
        ci_status: "passed",
        error_rate: 0.6,
        p95_latency_ms: 240,
        open_sev_incidents: 1, // active incident => block + page
        has_baseline: true,
        last_deploy_age_hours: 1,
      };
    case "new":
      return {
        ci_status: "passed",
        error_rate: 0.0,
        p95_latency_ms: 200,
        open_sev_incidents: 0,
        has_baseline: false, // never deployed => canary
        last_deploy_age_hours: null,
      };
    default:
      return null; // => 404 not_found
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Global change-freeze status.
  if (req.method === "GET" && url.pathname === "/v1/freeze") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        freeze_active: FREEZE_ACTIVE,
        reason: FREEZE_ACTIVE ? "End-of-quarter production change freeze." : "",
      })
    );
    return;
  }

  if (req.method !== "GET" || url.pathname !== "/v1/health") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not_found", message: "Unknown route." }));
    return;
  }

  const service = (url.searchParams.get("service") || "").trim();

  if (!service) {
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "missing_parameter",
        message: "Query parameter `service` is required.",
      })
    );
    return;
  }

  // Simulate a metrics-backend outage so the workflow's failure branch is testable.
  if (service.slice(0, 4).toUpperCase() === "FAIL") {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "upstream_error",
        message: "Metrics backend unavailable.",
      })
    );
    return;
  }

  const metrics = metricsFor(service);

  if (!metrics) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "not_found",
        message: `Service ${service} is not registered in the deploy catalog.`,
        service,
      })
    );
    return;
  }

  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ service, ...metrics }));
});

server.listen(PORT, () => {
  console.log(`Mock DevOps deploy/health API listening on http://localhost:${PORT}`);
  console.log(`Freeze window active: ${FREEZE_ACTIVE}`);
  console.log(`Try: http://localhost:${PORT}/v1/health?service=ok-payments`);
});

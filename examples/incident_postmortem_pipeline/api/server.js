// Mock incident-management API for the `incident_postmortem_pipeline` workflow
// example.
//
// Zero-dependency Node.js HTTP server. Run with: node server.js
//
// Simulates the signals an on-call postmortem-triage process inspects after a
// shift ends: incident severity, resolution status, customer impact, how long
// the incident ran, whether it was acknowledged within the severity's SLA,
// whether it matches a known recurring pattern, whether it is security/
// compliance-sensitive, and whether an engineer was ever assigned. It also
// exposes a global incident-management-system (IMS) platform status endpoint
// used by the pre-flight gate.
//
// Endpoints:
//   GET /v1/incident?id=<incident_id>   -> per-incident severity + context
//   GET /v1/ims-status                  -> global IMS platform outage status
//
// The response for /v1/incident is chosen from the incident-id prefix so every
// branch of the workflow can be exercised deterministically:
//
//   sev1_...        -> Sev1, customer-facing, acked in SLA      -> mandatory_postmortem
//   p1_...           -> Sev1, customer-facing, acked in SLA      -> mandatory_postmortem
//   sev2_...         -> Sev2, customer-facing, acked in SLA      -> mandatory_postmortem
//   sev3_...         -> Sev3, no customer impact, acked in SLA   -> quick_writeup
//   minor_...        -> Sev3, no customer impact, acked in SLA   -> quick_writeup
//   recur_...        -> Sev3, no customer impact, recurring      -> recurring_escalation
//   selfresolved_... -> auto-resolved, no customer impact        -> no_action
//   databreach_...   -> security/compliance incident             -> security_review
//   security_...     -> security/compliance incident             -> security_review
//   noeng_...        -> Sev3, no customer impact, no engineer    -> process_gap
//   breach_...       -> Sev3 but ack time blew the SLA           -> sla_breach
//   (anything else / unknown prefix)                             -> 404 not_found
//
// Special trigger:
//   FAIL_...  -> server replies 500 (simulate an IMS API failure)

const http = require("http");
const { URL } = require("url");

const PORT = process.env.PORT || 4600;

// Flip the global IMS platform status via env so the pre-flight abort branch
// is testable:  IMS_DOWN=1 node server.js  -> /v1/ims-status reports an outage.
const IMS_DOWN = process.env.IMS_DOWN === "1";

// Map an incident-id prefix to a mock incident record.
function incidentFor(id) {
  const prefix = id.split("_")[0].toLowerCase();

  switch (prefix) {
    case "sev1":
    case "p1":
      return {
        severity: "sev1",
        status: "resolved",
        customer_impact: true,
        duration_minutes: 95,
        is_recurring: false,
        is_security_related: false,
        assigned_engineer: "R. Kim",
        time_to_acknowledge_minutes: 8, // within the 15-min Sev1 SLA
      };
    case "sev2":
      return {
        severity: "sev2",
        status: "resolved",
        customer_impact: true,
        duration_minutes: 60,
        is_recurring: false,
        is_security_related: false,
        assigned_engineer: "T. Nguyen",
        time_to_acknowledge_minutes: 20, // within the 30-min Sev2 SLA
      };
    case "sev3":
    case "minor":
      return {
        severity: "sev3",
        status: "resolved",
        customer_impact: false,
        duration_minutes: 25,
        is_recurring: false,
        is_security_related: false,
        assigned_engineer: "M. Osei",
        time_to_acknowledge_minutes: 40, // within the 120-min Sev3 SLA
      };
    case "recur":
      return {
        severity: "sev3",
        status: "resolved",
        customer_impact: false,
        duration_minutes: 50,
        is_recurring: true, // matches a known prior incident signature
        is_security_related: false,
        assigned_engineer: "T. Nguyen",
        time_to_acknowledge_minutes: 18, // within the 120-min Sev3 SLA
      };
    case "selfresolved":
      return {
        severity: "sev3",
        status: "auto-resolved",
        customer_impact: false,
        duration_minutes: 4,
        is_recurring: false,
        is_security_related: false,
        assigned_engineer: "",
        time_to_acknowledge_minutes: 0,
      };
    case "databreach":
    case "security":
      return {
        severity: "sev1",
        status: "resolved",
        customer_impact: true,
        duration_minutes: 130,
        is_recurring: false,
        is_security_related: true, // legal/compliance routing
        assigned_engineer: "S. Whitfield",
        time_to_acknowledge_minutes: 12,
      };
    case "noeng":
      return {
        severity: "sev3",
        status: "open",
        customer_impact: false,
        duration_minutes: 70,
        is_recurring: false,
        is_security_related: false,
        assigned_engineer: "", // never assigned -> process gap
        time_to_acknowledge_minutes: 45, // within the 120-min Sev3 SLA
      };
    case "breach":
      return {
        severity: "sev3",
        status: "resolved",
        customer_impact: false,
        duration_minutes: 200,
        is_recurring: false,
        is_security_related: false,
        assigned_engineer: "M. Osei",
        time_to_acknowledge_minutes: 180, // blows the 120-min Sev3 SLA
      };
    default:
      return null; // => 404 not_found
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Global incident-management-platform status.
  if (req.method === "GET" && url.pathname === "/v1/ims-status") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        online: !IMS_DOWN,
        reason: IMS_DOWN ? "Incident-management platform outage — data feed untrusted." : "",
      })
    );
    return;
  }

  if (req.method !== "GET" || url.pathname !== "/v1/incident") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not_found", message: "Unknown route." }));
    return;
  }

  const id = (url.searchParams.get("id") || "").trim();

  if (!id) {
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "missing_parameter",
        message: "Query parameter `id` is required.",
      })
    );
    return;
  }

  // Simulate an IMS API failure so the workflow's failure branch is testable.
  if (id.slice(0, 4).toUpperCase() === "FAIL") {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "upstream_error",
        message: "Incident-management API unavailable.",
      })
    );
    return;
  }

  const incident = incidentFor(id);

  if (!incident) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "not_found",
        message: `Incident ${id} is not on file.`,
        id,
      })
    );
    return;
  }

  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ id, ...incident }));
});

server.listen(PORT, () => {
  console.log(`Mock incident-management API listening on http://localhost:${PORT}`);
  console.log(`IMS online: ${!IMS_DOWN}`);
  console.log(`Try: http://localhost:${PORT}/v1/incident?id=sev1_payments_outage`);
});

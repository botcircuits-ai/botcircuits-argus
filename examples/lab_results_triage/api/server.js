// Mock clinical lab / EHR API for the `lab_results_triage` workflow example.
//
// Zero-dependency Node.js HTTP server. Run with: node server.js
//
// Simulates the signals a clinical lab-results triage process inspects before
// deciding what to do with each patient's completed lab panel: whether the
// panel resulted, any critical (panic) values, abnormal-but-non-critical flags,
// known drug/allergy interactions for the ordered follow-up, and whether the
// patient still has an active care episode. It also exposes a global
// lab-information-system (LIS) status endpoint used by the pre-flight gate.
//
// Endpoints:
//   GET /v1/labresult?order=<order_id>   -> per-order lab panel + patient context
//   GET /v1/lis-status                   -> global LIS / interface-engine status
//
// The response for /v1/labresult is chosen from the order-id prefix so every
// branch of the workflow can be exercised deterministically:
//
//   NORM-... -> all results within reference range            -> routine
//   ABNL-... -> abnormal flags, none critical                 -> review
//   CRIT-... -> a critical (panic) value present              -> critical (page provider)
//   STAT-... -> critical value AND patient flagged unstable    -> critical (page provider)
//   INTX-... -> abnormal + a drug/allergy interaction on f/u   -> review + interaction alert
//   PEND-... -> panel ordered but not yet resulted            -> pending (defer)
//   DISC-... -> patient has no active care episode            -> no_episode (hold)
//   (anything else / unknown prefix)                          -> 404 not_found
//
// Special trigger:
//   FAIL-...  -> server replies 500 (simulate a LIS / EHR outage)

const http = require("http");
const { URL } = require("url");

const PORT = process.env.PORT || 4200;

// Flip the LIS interface status via env so the pre-flight abort branch is
// testable:  LIS_DOWN=1 node server.js  -> /v1/lis-status reports an outage.
const LIS_DOWN = process.env.LIS_DOWN === "1";

// Map an order-id prefix to a mock lab-panel + patient-context record.
function resultFor(order) {
  const prefix = order.split(/[-_]/)[0].toUpperCase();

  switch (prefix) {
    case "NORM":
      return {
        panel: "Basic Metabolic Panel",
        resulted: true,
        critical_value: false,
        abnormal_flags: 0,
        worst_flag: "",
        drug_interaction: false,
        active_episode: true,
        patient_unstable: false,
      };
    case "ABNL":
      return {
        panel: "Lipid Panel",
        resulted: true,
        critical_value: false,
        abnormal_flags: 2, // high LDL, high triglycerides
        worst_flag: "LDL 190 mg/dL (high)",
        drug_interaction: false,
        active_episode: true,
        patient_unstable: false,
      };
    case "CRIT":
      return {
        panel: "Basic Metabolic Panel",
        resulted: true,
        critical_value: true, // e.g. potassium 6.9 mmol/L
        abnormal_flags: 3,
        worst_flag: "Potassium 6.9 mmol/L (critical high)",
        drug_interaction: false,
        active_episode: true,
        patient_unstable: false,
      };
    case "STAT":
      return {
        panel: "Cardiac Enzymes",
        resulted: true,
        critical_value: true, // troponin critical
        abnormal_flags: 2,
        worst_flag: "Troponin I 4.2 ng/mL (critical high)",
        drug_interaction: false,
        active_episode: true,
        patient_unstable: true, // unstable => still critical, expedited page
      };
    case "INTX":
      return {
        panel: "Coagulation (INR)",
        resulted: true,
        critical_value: false,
        abnormal_flags: 1,
        worst_flag: "INR 3.8 (high)",
        drug_interaction: true, // warfarin dose adjustment interacts w/ allergy list
        active_episode: true,
        patient_unstable: false,
      };
    case "PEND":
      return {
        panel: "Comprehensive Metabolic Panel",
        resulted: false, // not yet resulted => defer
        critical_value: false,
        abnormal_flags: 0,
        worst_flag: "",
        drug_interaction: false,
        active_episode: true,
        patient_unstable: false,
      };
    case "DISC":
      return {
        panel: "Hemoglobin A1c",
        resulted: true,
        critical_value: false,
        abnormal_flags: 1,
        worst_flag: "A1c 7.4% (high)",
        drug_interaction: false,
        active_episode: false, // discharged / no active episode => hold
        patient_unstable: false,
      };
    default:
      return null; // => 404 not_found
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  // Global LIS / interface-engine status.
  if (req.method === "GET" && url.pathname === "/v1/lis-status") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        online: !LIS_DOWN,
        reason: LIS_DOWN ? "LIS interface engine offline for maintenance." : "",
      })
    );
    return;
  }

  if (req.method !== "GET" || url.pathname !== "/v1/labresult") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not_found", message: "Unknown route." }));
    return;
  }

  const order = (url.searchParams.get("order") || "").trim();

  if (!order) {
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "missing_parameter",
        message: "Query parameter `order` is required.",
      })
    );
    return;
  }

  // Simulate a LIS / EHR outage so the workflow's failure branch is testable.
  if (order.slice(0, 4).toUpperCase() === "FAIL") {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "upstream_error",
        message: "Lab information system unavailable.",
      })
    );
    return;
  }

  const result = resultFor(order);

  if (!result) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "not_found",
        message: `Lab order ${order} is not on file.`,
        order,
      })
    );
    return;
  }

  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ order, ...result }));
});

server.listen(PORT, () => {
  console.log(`Mock clinical lab / EHR API listening on http://localhost:${PORT}`);
  console.log(`LIS online: ${!LIS_DOWN}`);
  console.log(`Try: http://localhost:${PORT}/v1/labresult?order=NORM-1001`);
});

// Mock carrier tracking API for the `shipment_tracking` workflow example.
//
// Zero-dependency Node.js HTTP server. Run with: node server.js
//
// Endpoint:
//   GET /v1/track?number=<tracking_number>
//
// The response is chosen from the tracking number's prefix so every branch of
// the workflow can be exercised deterministically:
//
//   DLV...  -> delivered
//   OFD...  -> out for delivery
//   TRN...  -> in transit, arriving soon (normal update)
//   DLY...  -> in transit, estimated delivery > 7 days away (delayed)
//   EXC...  -> exception
//   RET...  -> returned
//   LST...  -> lost
//   UNK...  -> unknown status (workflow should default to an in-transit update)
//   (anything not matching a known prefix) -> 404 not_found
//
// Special trigger:
//   FAIL... -> server replies 500 (simulate a fetch failure / upstream error)

const http = require("http");
const { URL } = require("url");

const PORT = process.env.PORT || 4000;

// Return an ISO date `days` from now.
function daysFromNow(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

// Map a tracking-number prefix to a mock tracking record.
function recordFor(number) {
  const prefix = number.slice(0, 3).toUpperCase();

  switch (prefix) {
    case "DLV":
      return {
        status: "delivered",
        last_location: "Front porch, 12 Maple St, Springfield",
        estimated_delivery: daysFromNow(-1),
      };
    case "OFD":
      return {
        status: "out for delivery",
        last_location: "Springfield Local Depot",
        estimated_delivery: daysFromNow(0),
      };
    case "TRN":
      return {
        status: "in transit",
        last_location: "Chicago Regional Hub",
        estimated_delivery: daysFromNow(2),
      };
    case "DLY":
      return {
        status: "in transit",
        last_location: "Customs — Port of Newark",
        estimated_delivery: daysFromNow(12), // > 7 days => delayed branch
      };
    case "EXC":
      return {
        status: "exception",
        last_location: "Memphis Sort Facility",
        estimated_delivery: null,
      };
    case "RET":
      return {
        status: "returned",
        last_location: "Returned to sender",
        estimated_delivery: null,
      };
    case "LST":
      return {
        status: "lost",
        last_location: "Unknown",
        estimated_delivery: null,
      };
    case "UNK":
      return {
        status: "weather_hold", // unrecognized => workflow default branch
        last_location: "Denver Hub",
        estimated_delivery: daysFromNow(3),
      };
    default:
      return null; // => 404 not_found
  }
}

const server = http.createServer((req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);

  if (req.method !== "GET" || url.pathname !== "/v1/track") {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ error: "not_found", message: "Unknown route." }));
    return;
  }

  const number = (url.searchParams.get("number") || "").trim();

  if (!number) {
    res.writeHead(400, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "missing_parameter",
        message: "Query parameter `number` is required.",
      })
    );
    return;
  }

  // Simulate an upstream failure so the workflow's failure branch is testable.
  if (number.slice(0, 4).toUpperCase() === "FAIL") {
    res.writeHead(500, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({ error: "upstream_error", message: "Carrier unavailable." })
    );
    return;
  }

  const record = recordFor(number);

  if (!record) {
    res.writeHead(404, { "Content-Type": "application/json" });
    res.end(
      JSON.stringify({
        error: "not_found",
        message: `Tracking number ${number} is not recognized.`,
        tracking_number: number,
      })
    );
    return;
  }

  res.writeHead(200, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ tracking_number: number, ...record }));
});

server.listen(PORT, () => {
  console.log(`Mock carrier tracking API listening on http://localhost:${PORT}`);
  console.log(`Try: http://localhost:${PORT}/v1/track?number=TRN12345`);
});

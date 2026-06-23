# Shipment Tracking — Workflow Example

A complete example of a **complex BotCircuits workflow** that uses a **web fetch**
(an HTTP API call) plus **multiple branching conditions**.

Because a real carrier tracking API isn't available, this example ships a small
**mock API** (`api/`) that returns deterministic responses so every branch of
the workflow can be exercised.

## Contents

| Path | What it is |
|------|------------|
| [instructions.md](instructions.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [tracking-ids.txt](tracking-ids.txt) | Sample batch input — one tracking number per line, covering every branch. |
| [api/server.js](api/server.js) | Zero-dependency Node.js mock carrier tracking API. |
| [api/package.json](api/package.json) | `npm start` runner for the mock API. |

## 1. Start the mock API

The mock API has **no dependencies** — plain Node.js. From this folder:

```bash
cd api
node server.js
# or: npm start
```

It listens on `http://localhost:4000` (override with `PORT=xxxx node server.js`).

Endpoint:

```
GET /v1/track?number=<tracking_number>
```

Quick check:

```bash
curl "http://localhost:4000/v1/track?number=TRN12345"
```

## 2. Tracking numbers → responses

The response is chosen from the tracking number's **prefix**, so you can drive
the workflow down any path on demand:

| Prefix | Resulting `status` | Workflow branch it exercises |
|--------|--------------------|------------------------------|
| `DLV…` | `delivered` | Confirm delivery, stop |
| `OFD…` | `out for delivery` | "Arrives today" update |
| `TRN…` | `in transit` (ETA ~2 days) | Normal in-transit update |
| `DLY…` | `in transit` (ETA ~12 days) | Delayed → escalation |
| `EXC…` | `exception` | Straight to escalation |
| `RET…` | `returned` | Straight to escalation |
| `LST…` | `lost` | Straight to escalation |
| `UNK…` | `weather_hold` (unrecognized) | Default branch (treated as in-transit) |
| `FAIL…` | HTTP **500** | Fetch-failure branch |
| anything else | HTTP **404** `not_found` | Not-found / invalid branch |

Examples:

```bash
curl "http://localhost:4000/v1/track?number=DLV0001"   # delivered
curl "http://localhost:4000/v1/track?number=DLY0001"   # delayed (>7 days)
curl "http://localhost:4000/v1/track?number=FAIL01"    # 500 error
curl "http://localhost:4000/v1/track?number=ZZZ0001"   # 404 not found
```

## 3. Author the workflow

With the API running, paste the prompt in [instructions.md](instructions.md)
into the `botcircuits-workflow-authoring` skill. It generates the
`shipment_tracking` workflow, which:

1. reads tracking numbers from [tracking-ids.txt](tracking-ids.txt) (one per line),
2. fetches `http://localhost:4000/v1/track?number={tracking_number}` for each,
3. classifies every parcel using the branches above, and
4. writes all results to `tracking-status-<current_date>.json`
   (e.g. `tracking-status-2026-06-21.json`) with a per-outcome summary.

Build it, then run it (workflow-running skill). The sample input file mixes the
prefixes from the table above so a single run walks every path.

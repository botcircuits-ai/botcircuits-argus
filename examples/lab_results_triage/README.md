# Lab Results Triage — Workflow Example

A complete example of a **complex BotCircuits workflow** in the **healthcare**
domain. It decides — fully unattended — what to do with each patient's completed
lab panel on a night shift. It uses an **API/web fetch** (HTTP calls to a
clinical lab / EHR API), **file reads** (a triage protocol + a list of lab
orders), **multiple branching conditions**, and a **file write** (the triage
report).

> **Scale:** the source workflow has **11 steps** and **14 branch conditions**
> (a 3-stage pre-flight pipeline, an 8-way per-order `listDecision`, a tally
> gate, page/escalate/queue actions, and a report/abort split).

Because a real lab-information-system (LIS) + EHR backend isn't available, this
example ships a small **mock API** (`api/`) that returns deterministic responses
so every branch of the workflow can be exercised.

> ⚠️ This is a synthetic demo with fabricated, non-real patient data. It is a
> workflow-authoring example only — not a validated clinical decision-support
> device.

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [config/protocol.json](config/protocol.json) | The triage protocol (read by the pre-flight steps): approval, CLIA accreditation, on-call provider, abnormal threshold, min results. |
| [lab-orders.txt](lab-orders.txt) | Sample batch input — one lab order id per line, covering every triage branch. |
| [bin/triage_check.py](bin/triage_check.py) | Deterministic per-order lookup: pulls the resulted panel + patient context, applies the rules, emits one flat JSON fact object. |
| [api/server.js](api/server.js) | Zero-dependency Node.js mock clinical lab / EHR API. |
| [api/package.json](api/package.json) | `npm start` runner for the mock API. |

## 1. Start the mock API

The mock API has **no dependencies** — plain Node.js. From this folder:

```bash
cd api
node server.js
# or: npm start
```

It listens on `http://localhost:4200` (override with `PORT=xxxx node server.js`).

Endpoints:

```
GET /v1/labresult?order=<order_id>   # per-order resulted panel + patient context
GET /v1/lis-status                   # global lab-information-system status
```

Quick check:

```bash
curl "http://localhost:4200/v1/labresult?order=NORM-1001"
curl "http://localhost:4200/v1/lis-status"
```

To simulate a **lab-information-system outage** (so the pre-flight abort branch
fires), start the server with `LIS_DOWN=1`:

```bash
LIS_DOWN=1 node server.js
```

## 2. Order ids → triage outcomes

The result is chosen from the order id's **prefix**, so you can drive any order
down any path on demand:

| Prefix | Signals | Triage outcome |
|--------|---------|----------------|
| `NORM-…` | all results within reference range | `routine` |
| `ABNL-…` | abnormal flags, none critical | `review` (queue for provider) |
| `CRIT-…` | a critical (panic) value present | `critical` + page provider |
| `STAT-…` | critical value, patient unstable | `critical` + page provider |
| `INTX-…` | abnormal + a drug/allergy interaction | `interaction` (pharmacy + provider) |
| `PEND-…` | panel ordered but not yet resulted | `pending` (defer) |
| `DISC-…` | patient has no active care episode | `no_episode` (hold) |
| `FAIL-…` | API returns HTTP **500** | `error` (lookup failure) |
| anything else | HTTP **404** not on file | `not_found` |

Examples:

```bash
curl "http://localhost:4200/v1/labresult?order=CRIT-1003"   # critical -> page
curl "http://localhost:4200/v1/labresult?order=INTX-1005"   # interaction -> review
curl "http://localhost:4200/v1/labresult?order=FAIL-1008"   # 500 error
curl "http://localhost:4200/v1/labresult?order=GHOST-1009"  # 404 not found
```

## 3. Author the workflow

With the API running, paste the prompt in [TASK.md](TASK.md) into the
`botcircuits-workflow-authoring` skill. It generates the `lab_results_triage`
workflow, which:

1. reads the triage protocol [config/protocol.json](config/protocol.json),
2. runs pre-flight gates (approval → CLIA accreditation → LIS status); any
   failure aborts the batch and writes an abort report,
3. reads the lab orders from [lab-orders.txt](lab-orders.txt) (one per line),
4. for each, runs [bin/triage_check.py](bin/triage_check.py) against
   `http://localhost:4200/v1` and classifies a triage outcome,
5. tallies the batch (aborting if too few resulted, paging on any criticals),
6. escalates the criticals and queues the abnormal/interaction reviews, and
7. writes all results to `triage-report-<current_date>.json`
   (e.g. `triage-report-2026-06-22.json`) with a per-outcome summary.

Build it, then run it (workflow-running skill). The sample input file mixes the
prefixes from the table above so a single run walks every triage path.

### Drive the abort branches

- **LIS outage:** start the API with `LIS_DOWN=1` → the `check_lis_status`
  pre-flight aborts the whole batch.
- **Not approved / bad accreditation:** set `"approved": false` (or
  `"clia_valid": false`) in [config/protocol.json](config/protocol.json).
- **Too few resulted:** raise `min_results_required` above the number of
  resulting (non-pending/error/not_found) orders in
  [lab-orders.txt](lab-orders.txt).
```

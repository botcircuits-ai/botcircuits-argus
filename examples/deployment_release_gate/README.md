# Deployment Release Gate — Workflow Example

A complete example of a **complex BotCircuits workflow** in the **DevOps**
domain. It decides — fully unattended — which microservices in a release are
safe to promote to production. It uses an **API/web fetch** (HTTP calls to a
DevOps metrics API), **file reads** (a release manifest + a list of services),
**multiple branching conditions**, and a **file write** (the release report).

> **Scale:** the source workflow has **11 steps** and **13 branch conditions**
> (a 3-stage pre-flight pipeline, a 7-way per-service `listDecision`, a tally
> gate, notify/promote/canary actions, and a report/abort split).

Because a real CI/CD + observability backend isn't available, this example ships
a small **mock API** (`api/`) that returns deterministic responses so every
branch of the workflow can be exercised.

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [config/release.json](config/release.json) | The release manifest (read by the pre-flight steps): approval, change ticket, rollback plan, budgets, min services. |
| [services.txt](services.txt) | Sample batch input — one microservice name per line, covering every gate branch. |
| [bin/gate_check.py](bin/gate_check.py) | Deterministic per-service lookup: pulls CI/health/incident metrics, applies thresholds, emits one flat JSON fact object. |
| [api/server.js](api/server.js) | Zero-dependency Node.js mock DevOps deploy/health API. |
| [api/package.json](api/package.json) | `npm start` runner for the mock API. |

## 1. Start the mock API

The mock API has **no dependencies** — plain Node.js. From this folder:

```bash
cd api
node server.js
# or: npm start
```

It listens on `http://localhost:4100` (override with `PORT=xxxx node server.js`).

Endpoints:

```
GET /v1/health?service=<service_name>   # per-service CI/health/incident metrics
GET /v1/freeze                           # global change-freeze window status
```

Quick check:

```bash
curl "http://localhost:4100/v1/health?service=ok-payments-api"
curl "http://localhost:4100/v1/freeze"
```

To simulate an **active production change freeze** (so the pre-flight abort
branch fires), start the server with `FREEZE=1`:

```bash
FREEZE=1 node server.js
```

## 2. Service names → gate outcomes

The metrics are chosen from the service name's **prefix**, so you can drive any
service down any path on demand:

| Prefix | Signals | Gate outcome |
|--------|---------|--------------|
| `ok-…`   | CI passed, low errors, low latency, no incidents | `promote` |
| `slow-…` | CI passed but p95 latency over budget            | `hold` (perf) |
| `err-…`  | CI passed but error rate over budget             | `hold` (errors) |
| `ci-…`   | latest CI pipeline **failed**                    | `block` |
| `sev-…`  | an open SEV incident                             | `block` + page on-call |
| `new-…`  | never deployed before (no baseline)              | `canary` |
| `FAIL-…` | API returns HTTP **500**                         | `error` (fetch failure) |
| anything else | HTTP **404** not registered                 | `not_found` |

Examples:

```bash
curl "http://localhost:4100/v1/health?service=ci-inventory-sync"    # CI failed -> block
curl "http://localhost:4100/v1/health?service=sev-auth-gateway"     # incident  -> block + page
curl "http://localhost:4100/v1/health?service=FAIL-billing-worker"  # 500 error
curl "http://localhost:4100/v1/health?service=ghost-legacy-cron"    # 404 not found
```

## 3. Author the workflow

With the API running, paste the prompt in [TASK.md](TASK.md) into the
`botcircuits-workflow-authoring` skill. It generates the
`deployment_release_gate` workflow, which:

1. reads the release manifest [config/release.json](config/release.json),
2. runs pre-flight gates (approval → rollback plan → change-freeze window);
   any failure aborts the release and writes an abort report,
3. reads the services from [services.txt](services.txt) (one per line),
4. for each, runs [bin/gate_check.py](bin/gate_check.py) against
   `http://localhost:4100/v1` and classifies a gate outcome,
5. tallies the batch (aborting if too few are promotable, notifying on blockers),
6. promotes the green services and canaries the first-time ones, and
7. writes all results to `release-report-<current_date>.json`
   (e.g. `release-report-2026-06-22.json`) with a per-outcome summary.

Build it, then run it (workflow-running skill). The sample input file mixes the
prefixes from the table above so a single run walks every gate path.

### Drive the abort branches

- **Change freeze:** start the API with `FREEZE=1` → the `check_freeze_window`
  pre-flight aborts the whole release.
- **Not approved / no rollback plan:** set `"approved": false` (or blank
  `rollback_plan`) in [config/release.json](config/release.json).
- **Too few promotable:** raise `min_services_required` above the number of
  `ok-`/`new-` services in [services.txt](services.txt).

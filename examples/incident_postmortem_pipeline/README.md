# Incident Postmortem Pipeline — Workflow Example

A complete example of a **complex BotCircuits workflow** in the **software
engineering / on-call** domain. It decides — fully unattended — what
postmortem/follow-up process applies to every open incident ticket from an
on-call shift. It uses an **API/web fetch** (HTTP calls to an incident-
management API), **file reads** (an incident-response policy + a list of
incident ids), **multiple branching conditions**, and a **file write** (the
postmortem report).

> **Scale:** the source workflow has **12 steps** and **22+ branch
> conditions** (a 3-stage pre-flight pipeline, a 9-way per-incident
> `listDecision`, a 3-way tally gate, 7 per-outcome action branches, and a
> report/abort split).

Because a real incident-management system (PagerDuty/Opsgenie-style) backend
isn't available, this example ships a small **mock API** (`api/`) that returns
deterministic responses so every branch of the workflow can be exercised.

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [config/incident_policy.json](config/incident_policy.json) | The incident-response policy (read by the pre-flight steps): approval, on-call period, incident commander, SLA map, escalation budgets, min incidents. |
| [incidents.txt](incidents.txt) | Sample batch input — one incident id per line, covering every triage branch. |
| [bin/incident_check.py](bin/incident_check.py) | Deterministic per-incident lookup: pulls severity/status/context, applies thresholds, emits one flat JSON fact object. |
| [api/server.js](api/server.js) | Zero-dependency Node.js mock incident-management API. |
| [api/package.json](api/package.json) | `npm start` runner for the mock API. |

## 1. Start the mock API

The mock API has **no dependencies** — plain Node.js. From this folder:

```bash
cd api
node server.js
# or: npm start
```

It listens on `http://localhost:4600` (override with `PORT=xxxx node server.js`).

Endpoints:

```
GET /v1/incident?id=<incident_id>   # per-incident severity, status, and context
GET /v1/ims-status                  # global incident-management platform status
```

Quick check:

```bash
curl "http://localhost:4600/v1/incident?id=sev1_payments_outage"
curl "http://localhost:4600/v1/ims-status"
```

To simulate an **incident-management platform outage** (so the pre-flight
abort branch fires), start the server with `IMS_DOWN=1`:

```bash
IMS_DOWN=1 node server.js
```

## 2. Incident ids → postmortem outcomes

The record is chosen from the incident id's **prefix**, so you can drive any
incident down any path on demand:

| Prefix | Signals | Postmortem outcome |
|--------|---------|---------------------|
| `sev1_…` / `p1_…` | Sev1, customer-facing, acked in SLA | `mandatory_postmortem` |
| `sev2_…` | Sev2, customer-facing, acked in SLA | `mandatory_postmortem` |
| `sev3_…` / `minor_…` | Sev3, no customer impact, acked in SLA | `quick_writeup` |
| `recur_…` | matches a known recurring incident signature | `recurring_escalation` |
| `selfresolved_…` | auto-resolved, no customer impact | `no_action` |
| `databreach_…` / `security_…` | security/compliance incident | `security_review` |
| `noeng_…` | Sev3, no engineer was ever assigned | `process_gap` |
| `breach_…` | Sev3 but ack time blew the SLA | `sla_breach` |
| `FAIL_…` | API returns HTTP **500** | `error` (lookup failure) |
| anything else | HTTP **404** not on file | `not_found` |

Examples:

```bash
curl "http://localhost:4600/v1/incident?id=sev1_payments_outage"   # mandatory postmortem
curl "http://localhost:4600/v1/incident?id=recur_db_failover"      # recurring -> RCA escalation
curl "http://localhost:4600/v1/incident?id=databreach_customer_pii" # security -> legal/compliance
curl "http://localhost:4600/v1/incident?id=FAIL_billing_pipeline"  # 500 error
curl "http://localhost:4600/v1/incident?id=ghost_unknown_ticket"   # 404 not found
```

## 3. Author the workflow

With the API running, paste the prompt in [TASK.md](TASK.md) into the
`botcircuits-workflow-authoring` skill. It generates the
`incident_postmortem_pipeline` workflow, which:

1. reads the incident-response policy [config/incident_policy.json](config/incident_policy.json),
2. runs pre-flight gates (process approval → on-call schedule → IMS platform
   status); any failure aborts the whole run and writes an abort report,
3. reads the incident ids from [incidents.txt](incidents.txt) (one per line),
4. for each, runs [bin/incident_check.py](bin/incident_check.py) against
   `http://localhost:4600/v1` and classifies a postmortem outcome,
5. tallies the batch (aborting if too few resolved, alerting the incident
   commander on a serious-incident trend, notifying legal/compliance on any
   security incidents),
6. schedules postmortems, opens RCA tickets, routes security incidents, and
   flags process gaps, and
7. writes all results to `postmortem-report-<current_date>.json`
   (e.g. `postmortem-report-2026-06-25.json`) with a per-outcome summary.

Build it, then run it (workflow-running skill). The sample input file mixes
the prefixes from the table above so a single run walks every triage path.

### Drive the abort branches

- **IMS outage:** start the API with `IMS_DOWN=1` → the `check_ims_status`
  pre-flight aborts the whole run.
- **Not approved / no on-call schedule:** set `"process_approved": false` in
  [config/incident_policy.json](config/incident_policy.json) (or simulate a
  missing on-call schedule for the `oncall_period_id`).
- **Too few incidents:** raise `min_incidents_required` above the number of
  resolving (non-error/not_found) incidents in [incidents.txt](incidents.txt).
- **Trend alert:** lower `mandatory_postmortem_budget` and
  `recurring_escalation_budget` below the number of `mandatory_postmortem` +
  `recurring_escalation` incidents in the batch.

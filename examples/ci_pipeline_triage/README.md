# CI Pipeline Triage — Workflow Example

A complete example of a **complex BotCircuits workflow** in the **software
engineering / DevOps** domain. It decides — fully unattended — what to do with
every failing build/job across an org's repos in an overnight CI sweep. It uses
an **API/web fetch** (HTTP calls to a CI metrics API), **file reads** (a CI
triage policy + a list of failing job ids), **multiple branching conditions**,
and a **file write** (the triage report).

> **Scale:** the source workflow has **12 steps** and **21 branch conditions**
> (a 3-stage pre-flight pipeline, a 9-way per-job `listDecision`, a tally
> gate, retry/quarantine/page/log actions, and a report/abort split).

Because a real CI/CD provider backend isn't available, this example ships a
small **mock API** (`api/`) that returns deterministic responses so every
branch of the workflow can be exercised.

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [config/ci_policy.json](config/ci_policy.json) | The CI triage policy (read by the pre-flight steps): approval, on-call engineer, retry budget, queue depth, min jobs. |
| [failing-jobs.txt](failing-jobs.txt) | Sample batch input — one failing job/build id per line, covering every triage branch. |
| [bin/job_check.py](bin/job_check.py) | Deterministic per-job lookup: pulls CI failure metrics, applies thresholds, emits one flat JSON fact object. |
| [api/server.js](api/server.js) | Zero-dependency Node.js mock CI metrics API. |
| [api/package.json](api/package.json) | `npm start` runner for the mock API. |

## 1. Start the mock API

The mock API has **no dependencies** — plain Node.js. From this folder:

```bash
cd api
node server.js
# or: npm start
```

It listens on `http://localhost:4400` (override with `PORT=xxxx node server.js`).

Endpoints:

```
GET /v1/job?id=<job_id>   # per-job CI failure metrics
GET /v1/fleet              # runner fleet capacity / maintenance status
GET /v1/status             # global CI provider backend status
```


Quick check:

```bash
curl "http://localhost:4400/v1/job?id=flaky_checkout_e2e"
curl "http://localhost:4400/v1/fleet"
curl "http://localhost:4400/v1/status"
```

To simulate a **runner fleet in maintenance / over capacity** (so the
pre-flight capacity abort branch fires), start the server with
`FLEET_DOWN=1`:

```bash
FLEET_DOWN=1 node server.js
```

To simulate a **CI provider backend outage** (so the pre-flight abort branch
fires), start the server with `CI_DOWN=1`:

```bash
CI_DOWN=1 node server.js
```

## 2. Job ids → triage outcomes

The metrics are chosen from the job id's **prefix**, so you can drive any job
down any path on demand:

| Prefix | Signals | Triage outcome |
|--------|---------|-----------------|
| `flaky_…`   | high historical failure rate, not a compile error | `flaky` (retry + quarantine candidate) |
| `compile_…` | deterministic compile failure                     | `compile_error` (blocks merge) |
| `build_…`   | deterministic build failure                       | `build_failure` (blocks merge) |
| `oom_…`     | memory limit exceeded                             | `oom` (needs resource bump) |
| `timeout_…` | time limit exceeded                               | `timeout` |
| `infra_…`   | CI runner/infra fault, not a code problem          | `infra_issue` (auto-retry) |
| `lint_…`    | lint/style failure                                | `lint_failure` (auto-fixable) |
| `newpipe_…` | brand-new pipeline, no failure-history baseline    | `needs_baseline` (log only) |
| `FAIL_…`    | API returns HTTP **500**                          | `error` (lookup failure) |
| anything else | HTTP **404** not known                          | `not_found` |

Examples:

```bash
curl "http://localhost:4400/v1/job?id=flaky_search_integration"     # flaky -> retry + quarantine
curl "http://localhost:4400/v1/job?id=compile_billing_service"      # compile error -> blocks merge
curl "http://localhost:4400/v1/job?id=FAIL_payments_webhook"        # 500 error
curl "http://localhost:4400/v1/job?id=ghost_unknown_job"            # 404 not found
```

## 3. Author the workflow

With the API running, paste the prompt in [TASK.md](TASK.md) into the
`botcircuits-workflow-authoring` skill. It generates the `ci_pipeline_triage`
workflow, which:

1. reads the CI triage policy [config/ci_policy.json](config/ci_policy.json),
2. runs pre-flight gates (approval/enabled → runner fleet capacity → CI
   provider status); any failure aborts the whole run and writes an abort
   report,
3. reads the failing job ids from [failing-jobs.txt](failing-jobs.txt) (one
   per line),
4. for each, runs [bin/job_check.py](bin/job_check.py) against
   `http://localhost:4400/v1` and classifies a triage outcome,
5. tallies the batch (escalating to on-call if too many build/infra blockers,
   queuing chronic flaky jobs for quarantine),
6. auto-retries the infra/flaky jobs up to the retry budget, opens quarantine
   tickets, and pages on-call for build-blocking failures, and
7. writes all results to `ci-triage-report-<current_date>.json`
   (e.g. `ci-triage-report-2026-06-25.json`) with a per-outcome summary.

Build it, then run it (workflow-running skill). The sample input file mixes
the prefixes from the table above so a single run walks every triage path.

### Drive the abort branches

- **CI provider outage:** start the API with `CI_DOWN=1` → the
  `check_provider_status` pre-flight aborts the whole run.
- **Not approved / triage disabled:** set `"approved": false` (or
  `"triage_enabled": false`) in [config/ci_policy.json](config/ci_policy.json).
- **Runner fleet at/over capacity:** start the API with `FLEET_DOWN=1` (fleet
  reports `in_maintenance: true`), or lower `max_queue_depth` in
  [config/ci_policy.json](config/ci_policy.json) below the mock fleet's
  `queue_depth` (37), to force the `check_runner_fleet` pre-flight to abort.
- **Too few jobs:** raise `min_jobs_required` above the number of lines in
  [failing-jobs.txt](failing-jobs.txt).

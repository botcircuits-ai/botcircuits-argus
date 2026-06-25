# Test Suite Flakiness Triage — Workflow Example

A complete example of a **complex BotCircuits workflow** in the **software
engineering / DevEx** domain. It decides — fully unattended — what to do with
every test in a test-health report: quarantine it, release it from
quarantine, flag it as a real bug, route it to the right owner, or leave it
alone. It uses an **API/web fetch** (HTTP calls to a test-analytics API),
**file reads** (a flakiness-triage policy + a list of test ids), **multiple
branching conditions**, and a **file write** (the flakiness report).

> **Scale:** the source workflow has **at least 20 branch conditions** (a
> 3-stage pre-flight pipeline, a 9-way per-test `listDecision`, a tally gate
> with budget/escalation checks, quarantine/release/ticket actions, and a
> report/abort split).

Because a real test-analytics warehouse + CI backend isn't available, this
example ships a small **mock API** (`api/`) that returns deterministic
responses so every branch of the workflow can be exercised.

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [config/flakiness_policy.json](config/flakiness_policy.json) | The flakiness-triage policy (read by the pre-flight steps): approval, QA lead contact, flakiness-rate threshold, quarantine budget, min tests required. |
| [tests.txt](tests.txt) | Sample batch input — one test id per line, covering every triage branch. |
| [bin/testhealth_check.py](bin/testhealth_check.py) | Deterministic per-test lookup: pulls historical pass/fail signal, applies the flakiness-rate threshold, emits one flat JSON fact object. |
| [api/server.js](api/server.js) | Zero-dependency Node.js mock test-analytics API. |
| [api/package.json](api/package.json) | `npm start` runner for the mock API. |

## 1. Start the mock API

The mock API has **no dependencies** — plain Node.js. From this folder:

```bash
cd api
node server.js
# or: npm start
```

It listens on `http://localhost:4700` (override with `PORT=xxxx node server.js`).

Endpoints:

```
GET /v1/testhealth?test=<test_id>   # per-test historical pass/fail signal
GET /v1/analytics-status            # global test-analytics pipeline status
```

Quick check:

```bash
curl "http://localhost:4700/v1/testhealth?test=stable_checkout::test_apply_discount_code"
curl "http://localhost:4700/v1/analytics-status"
```

To simulate a **test-analytics backend outage** (so the pre-flight abort
branch fires), start the server with `ANALYTICS_DOWN=1`:

```bash
ANALYTICS_DOWN=1 node server.js
```

To simulate **stale warehouse data** (so the data-freshness pre-flight abort
fires instead), start the server with `STALE_DATA=1`:

```bash
STALE_DATA=1 node server.js
```

## 2. Test ids → triage outcomes

The historical signal is chosen from the test id's **prefix**, so you can
drive any test down any path on demand:

| Prefix | Signals | Triage outcome |
|--------|---------|-----------------|
| `stable_…`      | low flakiness, not quarantined                  | `stable` |
| `flaky_…`       | flakiness rate over threshold, not quarantined    | `quarantine_new` |
| `quarantined_…`  | currently quarantined, now stable                | `release_from_quarantine` |
| `timing_…`       | consistent race-condition failure pattern        | `timing_issue` |
| `envdep_…`       | fails only on specific runners                   | `environment_issue` |
| `newtest_…`      | no run history yet                               | `needs_baseline` |
| `alwaysfail_…`    | consistently failing (real bug, not flaky)       | `real_bug` |
| `FAIL_…`         | API returns HTTP **500**                          | `error` (lookup failure) |
| anything else    | HTTP **404** unknown to the analytics service     | `not_found` |

Examples:

```bash
curl "http://localhost:4700/v1/testhealth?test=flaky_checkout::test_async_cart_sync"        # over threshold -> quarantine_new
curl "http://localhost:4700/v1/testhealth?test=alwaysfail_payments::test_refund_partial_amount"  # consistent -> real_bug
curl "http://localhost:4700/v1/testhealth?test=FAIL_search::test_typeahead_suggestions"     # 500 error
curl "http://localhost:4700/v1/testhealth?test=ghost_module::test_does_not_exist"            # 404 not found
```

## 3. Author the workflow

With the API running, paste the prompt in [TASK.md](TASK.md) into the
`botcircuits-workflow-authoring` skill. It generates the
`test_suite_flakiness_triage` workflow, which:

1. reads the flakiness-triage policy [config/flakiness_policy.json](config/flakiness_policy.json),
2. runs pre-flight gates (approval → data freshness → analytics-service
   outage); any failure aborts the whole run and writes an abort report,
3. reads the test ids from [tests.txt](tests.txt) (one per line),
4. for each, runs [bin/testhealth_check.py](bin/testhealth_check.py) against
   `http://localhost:4700/v1` and classifies a triage outcome,
5. tallies the batch (escalating to the QA lead if the quarantine budget
   would be exceeded, notifying the owning team on any real bugs),
6. quarantines the newly-flaky tests, releases the now-stable ones, and opens
   bug/infra/test-fix tickets for the rest, and
7. writes all results to `flakiness-report-<current_date>.json`
   (e.g. `flakiness-report-2026-06-25.json`) with a per-outcome summary.

Build it, then run it (workflow-running skill). The sample input file mixes
the prefixes from the table above so a single run walks every triage path.

### Drive the abort branches

- **Analytics outage:** start the API with `ANALYTICS_DOWN=1` → the
  `check_analytics_status` pre-flight aborts the whole run.
- **Stale data:** start the API with `STALE_DATA=1` → the
  `verify_data_freshness` pre-flight aborts the whole run.
- **Not approved:** set `"triage_approved": false` in
  [config/flakiness_policy.json](config/flakiness_policy.json).
- **Quarantine budget exceeded:** lower `quarantine_budget` below the number
  of `flaky_…` tests in [tests.txt](tests.txt).
- **Too few tests:** raise `min_tests_required` above the number of
  resulting (non-error/not_found) tests in [tests.txt](tests.txt).

# Code Review Gate — Workflow Example

A complete example of a **complex BotCircuits workflow** in the **software
engineering** domain. It decides — fully unattended — which findings from a
static-analysis/lint/security scan on a pull request are mergeable as-is,
need a fix, or must block the merge. It uses an **API/web fetch** (HTTP calls
to a code-quality scan API), **file reads** (a review policy + a list of
changed files), **multiple branching conditions**, and a **file write** (the
review report).

> **Scale:** the source workflow has **11 steps** and **14 branch conditions**
> (a 3-stage pre-flight pipeline, an 8-way per-file `listDecision`, a tally
> gate, request-changes/queue-review actions, and a report/abort split).

Because a real CI code-quality backend isn't available, this example ships a
small **mock API** (`api/`) that returns deterministic responses so every
branch of the workflow can be exercised.

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [config/review_policy.json](config/review_policy.json) | The review policy (read by the pre-flight steps): approval, approvals received, ready-for-merge flag, severity budgets, min files. |
| [changed-files.txt](changed-files.txt) | Sample batch input — one changed file path per line, covering every gate branch. |
| [bin/scan_check.py](bin/scan_check.py) | Deterministic per-file lookup: pulls the lint/security/complexity scan, applies thresholds, emits one flat JSON fact object. |
| [api/server.js](api/server.js) | Zero-dependency Node.js mock code-quality scan API. |
| [api/package.json](api/package.json) | `npm start` runner for the mock API. |

## 1. Start the mock API

The mock API has **no dependencies** — plain Node.js. From this folder:

```bash
cd api
node server.js
# or: npm start
```

It listens on `http://localhost:4300` (override with `PORT=xxxx node server.js`).

Endpoints:

```
GET /v1/scan?file=<path>   # per-file lint/security/complexity scan
GET /v1/scanner-status     # global code-quality-service status
```

Quick check:

```bash
curl "http://localhost:4300/v1/scan?file=clean_src/utils/format.py"
curl "http://localhost:4300/v1/scanner-status"
```

To simulate the **code-quality service being in maintenance mode** (so the
pre-flight abort branch fires), start the server with `SCANNER_DOWN=1`:

```bash
SCANNER_DOWN=1 node server.js
```

## 2. File prefixes → review outcomes

The scan result is chosen from the changed file's **path prefix**, so you can
drive any file down any path on demand:

| Prefix | Signals | Review outcome |
|--------|---------|-----------------|
| `clean_…` | no findings, low complexity, no coverage drop      | `approve` |
| `style_…` | lint warnings over budget + a coverage drop         | `review` |
| `warn_…`  | lint warnings over budget + a coverage drop         | `review` |
| `sec_…`   | a **high**-severity security finding                | `needs_fix` |
| `crit_…`  | a **critical** security finding (+ lint errors)     | `block` |
| `dup_…`   | flagged as a clone of another file                  | `needs_fix` (duplicate) |
| `big_…`   | huge diff — lines changed over budget               | `large_diff` |
| `FAIL_…`  | API returns HTTP **500**                            | `error` (scan failure) |
| anything else | HTTP **404** not in the scan catalog            | `not_found` |

Examples:

```bash
curl "http://localhost:4300/v1/scan?file=crit_src/auth/login.py"             # critical -> block
curl "http://localhost:4300/v1/scan?file=sec_src/payments/charge.py"         # high sev -> needs_fix
curl "http://localhost:4300/v1/scan?file=dup_src/utils/format_copy.py"       # clone -> needs_fix
curl "http://localhost:4300/v1/scan?file=FAIL_src/worker/queue_consumer.py"  # 500 error
curl "http://localhost:4300/v1/scan?file=ghost_src/legacy/unused_helper.py"  # 404 not found
```

## 3. Author the workflow

With the API running, paste the prompt in [TASK.md](TASK.md) into the
`botcircuits-workflow-authoring` skill. It generates the `code_review_gate`
workflow, which:

1. reads the review policy [config/review_policy.json](config/review_policy.json),
2. runs pre-flight gates (policy approved → ready-for-merge/approvals →
   scanner status); any failure aborts the whole run and writes an abort
   report,
3. reads the changed files from [changed-files.txt](changed-files.txt) (one
   per line),
4. for each, runs [bin/scan_check.py](bin/scan_check.py) against
   `http://localhost:4300/v1` and classifies a review outcome,
5. tallies the batch (aborting if too many blockers, requesting changes on
   any blocks),
6. requests changes on the `block` files and queues the `needs_fix` /
   `review` / `large_diff` files for manual review, and
7. writes all results to `review-report-<current_date>.json`
   (e.g. `review-report-2026-06-25.json`) with a per-outcome summary.

Build it, then run it (workflow-running skill). The sample input file mixes
the prefixes from the table above so a single run walks every gate path.

### Drive the abort branches

- **Scanner down:** start the API with `SCANNER_DOWN=1` → the
  `check_scanner_status` pre-flight aborts the whole review.
- **Not approved / not ready for merge:** set `"policy_approved": false` (or
  `"ready_for_merge": false`) in [config/review_policy.json](config/review_policy.json).
- **Too few files reviewed:** raise `min_files_required` above the number of
  files in [changed-files.txt](changed-files.txt).

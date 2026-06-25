# Workflow name:  `ci_pipeline_triage`

---

# Instruction Prompt

> Create a **CI/CD pipeline failure triage** workflow that decides, fully
> unattended, what to do with every failing build/job across an org's repos in
> an overnight CI sweep. It reads a CI triage policy and a list of failing job
> ids, runs some pre-flight safety checks, decides an **outcome for every
> job**, acts on the retries/escalations, and writes a single triage report at
> the end. This is an unattended batch run — never ask the user anything.
>
> Use `http://localhost:4400/v1` as the CI metrics API host (a local mock —
> see `api/` and the README to start it).
>
> **What it should do:**
>
> 1. **Start.** Take a `ci_policy_file` input (default
>    `examples/ci_pipeline_triage/config/ci_policy.json`) and a `jobs_file`
>    input (default `examples/ci_pipeline_triage/failing-jobs.txt`, one
>    failing job/build id per line).
>
> 2. **Pre-flight, in order — any failure aborts the whole run:**
>    - **Load the policy.** Read org, pipeline_run_id, the `triage_enabled`
>      flag, the `approved` flag, the on-call engineer, the
>      `flaky_retry_budget`, `max_queue_depth`, `min_jobs_required`, and the
>      `compile_blocker_budget` / `infra_blocker_budget`. If triage is **not
>      enabled** or **not approved**, abort.
>    - **Verify the CI runner fleet.** Query `http://localhost:4400/v1/fleet`;
>      if the fleet is **in maintenance** or its `queue_depth` **exceeds**
>      `max_queue_depth`, abort — there's no spare capacity to act on triage
>      decisions.
>    - **Check the CI provider status.** Query
>      `http://localhost:4400/v1/status`; if the CI provider backend is
>      **down**, abort — triage data from a down backend can't be trusted.
>
> 3. **Decide an `outcome` for every failing job in the list.** For each job
>    the per-item facts come from a deterministic lookup: query
>    `http://localhost:4400/v1/job?id={job_id}` and read the failure reason,
>    exit code, duration, retry count, historical failure rate, whether it's
>    an infra fault, whether memory was exceeded, and whether the pipeline has
>    a failure-history baseline; also note whether the lookup failed or the
>    job was unknown. Gather these facts **per item** — do not have the model
>    decide the outcomes itself.
>
> 4. **Classification rules** (per job; check in this order, first match
>    wins):
>    - Lookup fails / non-200 / unreadable → `outcome = "error"`.
>    - Job id **not known** to the CI metrics backend → `outcome = "not_found"`.
>    - **CI infra fault** (`is_infra_error`) → `outcome = "infra_issue"`
>      (auto-retry; not a code problem).
>    - **Historical failure rate over the flaky threshold** and not a
>      deterministic compile error → `outcome = "flaky"` (retry recommended,
>      quarantine candidate).
>    - **Memory exceeded** → `outcome = "oom"` (needs a resource bump).
>    - **Timed out** → `outcome = "timeout"`.
>    - **Deterministic compile error** → `outcome = "compile_error"` (blocks
>      merge, needs a code fix).
>    - **Deterministic build failure** → `outcome = "build_failure"` (blocks
>      merge, needs a code fix).
>    - **Lint/style failure** → `outcome = "lint_failure"` (auto-fixable /
>      needs a fix).
>    - **No failure-history baseline** (brand-new pipeline) →
>      `outcome = "needs_baseline"` (first failure — just log it).
>    - Otherwise → `outcome = "pass"` (resolved on its own / nothing to do).
>
> 5. **Each decided record** holds: `job_id`, `outcome`, `failure_reason`,
>    `exit_code`, `duration_seconds`, `historical_failure_rate`, and a short
>    human-readable `note`. For `compile_error`, `build_failure`, `oom`, and
>    `infra_issue` outcomes, also set `needs_attention: true`. Collect all
>    decided records into a single list.
>
> 6. **Tally.** Count resolved jobs (everything except `error` and
>    `not_found`), retryable jobs (`infra_issue` + `flaky`), blockers
>    (`compile_error` + `build_failure`), and the attention items
>    (`needs_attention` count). If the combined **compile/build blocker
>    count exceeds `compile_blocker_budget`**, or the **infra-issue count
>    exceeds `infra_blocker_budget`**, abort and escalate to the on-call
>    engineer instead of acting — something systemic is wrong. If there are
>    **any `flaky` jobs**, queue them for quarantine review before continuing.
>
> 7. **Act.**
>    - For `infra_issue` and `flaky` jobs, auto-retry them, up to
>      `flaky_retry_budget` retries per job (skip jobs already at/over the
>      budget's `retry_count` and log them instead).
>    - For jobs that have been retried at/over the budget and are still
>      `flaky`, open a quarantine ticket (chronic flaky candidate).
>    - For `compile_error` and `build_failure` jobs, page the on-call engineer
>      — these block the main branch.
>    - For `oom` jobs, flag them for a resource-limit bump (no retry until the
>      limit is raised).
>    - For `needs_baseline` jobs, just log them — no action, first failure on
>      a new pipeline.
>    - `lint_failure`, `timeout`, `pass`, `error`, and `not_found` jobs need no
>      escalation here.
>
> 8. **Report.** Write a `ci-triage-report-<today>.json` (today's date,
>    `YYYY-MM-DD`) with a `summary` (org, pipeline_run_id, totals, a
>    per-outcome `counts` map) and the full per-job `results` list. An aborted
>    run instead writes `ci-triage-abort-<today>.json` with the abort reason.
>
> Keep the CI API host, the input file names, and the flaky/queue-depth/
> blocker-budget/min-jobs thresholds easy to change. A single bad job must
> produce an `error` / `not_found` record for that item — it must never stop
> the rest of the batch.

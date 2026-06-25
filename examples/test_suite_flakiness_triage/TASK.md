# Workflow name:  `test_suite_flakiness_triage`

---

# Instruction Prompt

> Create a **test-suite flakiness detection and quarantine** workflow that
> decides, fully unattended, what to do with every test in a test-health
> report based on its historical pass/fail signal: quarantine it, release it
> from quarantine, flag it as a real bug, route it to the right owner, or
> leave it alone. It reads a flakiness-triage policy and a list of test ids,
> runs some pre-flight checks, decides an **outcome for every test**, acts on
> the results, and writes a single report at the end. This is an unattended
> batch run — never ask the user anything.
>
> Use `http://localhost:4700/v1` as the test-analytics API host (a local
> mock — see `api/` and the README to start it).
>
> **What it should do (scale: at least 20 branch conditions across pre-flight,
> per-test classification, the tally gate, and the action stage):**
>
> 1. **Start.** Take a `policy_config_file` input (default
>    `examples/test_suite_flakiness_triage/config/flakiness_policy.json`) and a
>    `tests_file` input (default
>    `examples/test_suite_flakiness_triage/tests.txt`, one test id per line,
>    e.g. `module::test_name`).
>
> 2. **Pre-flight, in order — any failure aborts the whole run:**
>    - **Load the policy.** Read org, repo, `analysis_window_days`, the
>      `triage_approved` flag, the QA lead contact, the
>      `flakiness_rate_threshold_pct`, the `quarantine_budget`, and
>      `min_tests_required`. If the policy is **not approved**
>      (`triage_approved` is false), abort.
>    - **Verify data freshness.** Query `http://localhost:4700/v1/analytics-status`
>      and compare `data_freshness_hours` against the policy's
>      `data_freshness_max_hours`. If the test-history warehouse data is **too
>      stale to trust**, abort.
>    - **Check the test-analytics service status.** From the same
>      `/v1/analytics-status` response, read the `outage` flag; if the
>      analytics backend is **down**, abort.
>
> 3. **Decide an `outcome` for every test in the list.** For each test the
>    per-item facts come from a deterministic lookup: query
>    `http://localhost:4700/v1/testhealth?test={test_id}` and read
>    `total_runs`, `pass_count`, `fail_count`, the computed `flakiness_rate`,
>    `currently_quarantined`, `failure_pattern` (`consistent` / `timing` /
>    `environment` / `intermittent` / `none`), `runner_specific_failures`, and
>    `has_history`. The lookup also compares the flakiness rate against the
>    policy threshold and decides whether the failure is a real bug
>    (consistently failing, not flaky), genuinely flaky, a timing/race-condition
>    pattern, or environment-specific; also note whether the lookup failed or
>    the test was unknown. Gather these facts **per item** — do not have the
>    model decide the outcomes itself.
>
> 4. **Classification rules** (per test; check in this priority order):
>    - Lookup fails / non-200 / unreadable → `outcome = "error"`.
>    - Test **unknown to the analytics service** → `outcome = "not_found"`.
>    - **Consistently failing** (every run fails, pattern is `consistent`) →
>      `outcome = "real_bug"` — a real fix is needed; never quarantine this.
>    - **Currently quarantined** but now stable / below threshold →
>      `outcome = "release_from_quarantine"`.
>    - **Flakiness rate over the threshold** and not yet quarantined →
>      `outcome = "quarantine_new"`.
>    - Failure pattern is **`environment`** (or `runner_specific_failures` is
>      true) → `outcome = "environment_issue"` — infra ticket, not a test/code
>      bug.
>    - Failure pattern is **`timing`** (race-condition) →
>      `outcome = "timing_issue"` — needs a code-level fix to the test.
>    - **No run history yet** (`has_history` is false) →
>      `outcome = "needs_baseline"`.
>    - Otherwise (low flakiness, no action needed) → `outcome = "stable"`.
>
> 5. **Each decided record** holds: `test`, `outcome`, `total_runs`,
>    `flakiness_rate`, `failure_pattern`, and a short human-readable `note`.
>    For `real_bug`, `quarantine_new`, `timing_issue`, and `environment_issue`
>    outcomes, also set `needs_attention: true`. Collect all decided records
>    into a single list.
>
> 6. **Tally.** Count `quarantine_new`, `release_from_quarantine`, `real_bug`,
>    `environment_issue`, `timing_issue`, `needs_baseline`, `stable`, `error`,
>    and `not_found` tests. If the number of `quarantine_new` tests would
>    **exceed the policy's `quarantine_budget`**, abort and escalate to the QA
>    lead instead of quarantining indiscriminately. If there are **any
>    `real_bug`** tests, notify the owning team before continuing. If **fewer
>    than `min_tests_required`** tests resulted in a usable outcome (excluding
>    `error` / `not_found`), abort.
>
> 7. **Act.**
>    - Quarantine the `quarantine_new` tests (mark skip/disable in the suite).
>    - Release the `release_from_quarantine` tests back into the active suite.
>    - Open a bug ticket for each `real_bug` test, routed to the owning team.
>    - Open an infra ticket for each `environment_issue` test.
>    - Open a test-fix ticket for each `timing_issue` test.
>    - Log `stable` and `needs_baseline` tests with no action.
>    (`error` and `not_found` tests need no action beyond appearing in the
>    report.)
>
> 8. **Report.** Write a `flakiness-report-<today>.json` (today's date,
>    `YYYY-MM-DD`) with a `summary` (org, repo, totals, a per-outcome `counts`
>    map) and the full per-test `results` list. An aborted run instead writes
>    `flakiness-abort-<today>.json` with the abort reason.
>
> Keep the test-analytics API host, the input file names, and the
> flakiness-rate / quarantine-budget / min-tests thresholds easy to change. A
> single bad test id must produce an `error` / `not_found` record for that
> item — it must never stop the rest of the batch.

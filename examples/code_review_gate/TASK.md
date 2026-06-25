# Workflow name:  `code_review_gate`

---

# Instruction Prompt

> Create a **code review / PR merge gate** workflow that decides, fully
> unattended, which findings from a static-analysis/lint/security scan on a
> pull request are mergeable as-is, need a fix, or must block the merge. It
> reads a review policy and a list of changed files, runs some pre-flight
> checks, decides an **outcome for every changed file**, acts on the
> blockers, and writes a single report at the end. This is an unattended
> batch run — never ask the user anything.
>
> Use `http://localhost:4300/v1` as the code-quality scan API host (a local
> mock — see `api/` and the README to start it).
>
> **What it should do:**
>
> 1. **Start.** Take a `review_config_file` input (default
>    `examples/code_review_gate/config/review_policy.json`) and a
>    `changed_files_file` input (default
>    `examples/code_review_gate/changed-files.txt`, one changed file path per
>    line).
>
> 2. **Pre-flight, in order — any failure aborts the whole run:**
>    - **Load the policy.** Read repo, pr_number, base_branch,
>      required_approvals, approvals_received, the `policy_approved` flag,
>      the severity budgets (`max_critical_findings`, `max_high_findings`,
>      `max_lint_warnings`), and `min_files_required`. If the policy is **not
>      approved**, abort.
>    - **Verify merge readiness.** If the PR is **not `ready_for_merge`**, or
>      `approvals_received` is **below `required_approvals`**, abort — the
>      gate must not run on a PR that hasn't cleared human review sign-off.
>    - **Check the scanner status.** Query
>      `http://localhost:4300/v1/scanner-status`; if the code-quality service
>      is **offline / in maintenance mode**, abort — scan results can't be
>      trusted mid-outage.
>
> 3. **Decide an `outcome` for every changed file in the list.** For each file
>    the per-item facts come from a deterministic lookup: query
>    `http://localhost:4300/v1/scan?file={file_path}` and read the lint-error
>    count, lint-warning count, security-findings count and worst severity,
>    whether the file is flagged as a duplicate/clone (and of what), lines
>    changed, complexity score, test-coverage delta, and whether the file has
>    a prior baseline scan; also note whether the lookup failed or the file
>    was unknown. Gather these facts **per item** — do not have the model
>    decide the outcomes itself.
>
> 4. **Classification rules** (per file; check failures first):
>    - Lookup fails / non-200 / unreadable → `outcome = "error"`.
>    - File **not in the scan catalog** → `outcome = "not_found"`.
>    - A **critical security finding** present, OR **lint errors** present →
>      `outcome = "block"`.
>    - A **high-severity security finding** present → `outcome = "needs_fix"`.
>    - **Complexity score over budget** → `outcome = "needs_fix"`.
>    - File **flagged as a duplicate/clone** of another file →
>      `outcome = "needs_fix"` (de-duplicate).
>    - **Lint warnings over budget AND a test-coverage drop** on the same
>      file → `outcome = "review"` (queue for manual review).
>    - **Lines changed over the large-diff threshold** (and none of the above
>      apply) → `outcome = "large_diff"` (needs manual review).
>    - Otherwise (within every budget) → `outcome = "approve"`.
>
> 5. **Each decided record** holds: `file`, `outcome`, `lint_errors`,
>    `lint_warnings`, `security_findings`, `worst_security_severity`,
>    `lines_changed`, `complexity_score`, and a short human-readable `note`.
>    For `block`, `needs_fix`, `review`, and `large_diff` outcomes, also set
>    `needs_attention: true`. Collect all decided records into a single list.
>
> 6. **Tally.** Count mergeable (approve), blockers (block), and the
>    needs-attention queue (needs_fix + review + large_diff). If the blocker
>    count **exceeds the policy's blocker budget** (more blockers than
>    `max_critical_findings + max_high_findings` allows, i.e. any blocker when
>    both budgets are zero), abort the whole gate and escalate. If there are
>    **any `block`** files (and the run isn't aborted), send a "request
>    changes" notification before continuing.
>
> 7. **Act.** For the `block` files, request changes / comment on the PR. For
>    the `needs_fix`, `review`, and `large_diff` files, queue a manual-review
>    task. (Approved files need no action here.)
>
> 8. **Report.** Write a `review-report-<today>.json` (today's date,
>    `YYYY-MM-DD`) with a `summary` (repo, pr_number, totals, a per-outcome
>    `counts` map) and the full per-file `results` list. An aborted run
>    instead writes `review-abort-<today>.json` with the abort reason.
>
> Keep the scan API host, the input file names, and the complexity/
> lint-warning/large-diff/min-files thresholds easy to change. A single bad
> file must produce an `error` / `not_found` record for that item — it must
> never stop the rest of the batch.

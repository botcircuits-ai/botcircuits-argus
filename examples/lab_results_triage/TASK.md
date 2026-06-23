# Workflow name:  `lab_results_triage`

---

# Instruction Prompt

> Create a **clinical lab-results triage** workflow that decides, fully
> unattended, what to do with each patient's completed lab panel on a night
> shift. It reads a triage protocol and a list of lab orders, runs some
> pre-flight safety checks, decides an **outcome for every lab order**, acts on
> the criticals, and writes a single triage report at the end. This is an
> unattended batch run — never ask the user anything.
>
> Use `http://localhost:4200/v1` as the clinical lab / EHR API host (a local
> mock — see `api/` and the README to start it).
>
> **What it should do:**
>
> 1. **Start.** Take a `protocol_config_file` input (default
>    `examples/lab_results_triage/config/protocol.json`) and a `orders_file`
>    input (default `examples/lab_results_triage/lab-orders.txt`, one lab order
>    id per line).
>
> 2. **Pre-flight, in order — any failure aborts the whole batch:**
>    - **Load the protocol.** Read protocol_id, facility, the CLIA certificate
>      and its `clia_valid` flag, the `approved` flag, the on-call provider, the
>      `abnormal_flag_threshold`, and `min_results_required`. If the protocol is
>      **not approved**, abort.
>    - **Verify lab accreditation.** If the CLIA certificate is **not valid**
>      (`clia_valid` is false or the certificate is blank), abort — results may
>      not be released.
>    - **Check the lab information system.** Query
>      `http://localhost:4200/v1/lis-status`; if the LIS is **offline**, abort —
>      results cannot be trusted mid-outage.
>
> 3. **Decide an `outcome` for every lab order in the list.** For each order the
>    per-item facts come from a deterministic lookup: query
>    `http://localhost:4200/v1/labresult?order={order_id}` and read the panel
>    name, whether it has resulted, whether a critical (panic) value is present,
>    the abnormal-flag count and worst flag, whether a drug/allergy interaction
>    applies to the ordered follow-up, and whether the patient still has an
>    active care episode; also note whether the lookup failed or the order was
>    unknown. Gather these facts **per item** — do not have the model decide the
>    outcomes itself.
>
> 4. **Classification rules** (per order; check failures first):
>    - Lookup fails / non-200 / unreadable → `outcome = "error"`.
>    - Order **not on file** in the lab system → `outcome = "not_found"`.
>    - Panel **not yet resulted** → `outcome = "pending"` (defer to next batch).
>    - A **critical (panic) value** present → `outcome = "critical"` (page the
>      ordering provider).
>    - Patient has **no active care episode** → `outcome = "no_episode"` (hold,
>      route to records).
>    - **Abnormal flags at/above the threshold AND a drug/allergy interaction**
>      on the follow-up → `outcome = "interaction"` (pharmacy + provider review).
>    - **Abnormal flags at/above the threshold** (no interaction) →
>      `outcome = "review"` (queue for provider review).
>    - Otherwise (all within reference range) → `outcome = "routine"`.
>
> 5. **Each decided record** holds: `order`, `outcome`, `panel`, `worst_flag`,
>    `abnormal_flags`, and a short human-readable `note`. For `critical`,
>    `interaction`, and `review` outcomes, also set `needs_attention: true`.
>    Collect all decided records into a single list.
>
> 6. **Tally.** Count resulted records (everything except `pending`, `error`,
>    `not_found`), criticals, review-queue (review + interaction), and the
>    attention items (critical + interaction + review). If **fewer than
>    `min_results_required`** orders resulted, abort. If there are **any
>    critical** results, send a critical-value page before continuing.
>
> 7. **Act.** For the `critical` orders, open a critical-value escalation to the
>    on-call provider. For the `interaction` and `review` orders, queue a
>    provider-review task. (Routine, pending, no_episode, error, and not_found
>    orders need no action here.)
>
> 8. **Report.** Write a `triage-report-<today>.json` (today's date,
>    `YYYY-MM-DD`) with a `summary` (protocol_id, facility, totals, a per-outcome
>    `counts` map) and the full per-order `results` list. An aborted run instead
>    writes `triage-abort-<today>.json` with the abort reason.
>
> Keep the lab API host, the input file names, and the abnormal-flag /
> min-results thresholds easy to change. A single bad lab order must produce an
> `error` / `not_found` record for that item — it must never stop the rest of
> the batch.

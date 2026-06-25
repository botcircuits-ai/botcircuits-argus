# Workflow name:  `incident_postmortem_pipeline`

---

# Instruction Prompt

> Create a **production-incident postmortem-triage** workflow that decides,
> fully unattended, what postmortem/follow-up process applies to every open
> incident ticket from an on-call shift. It reads an incident-response policy
> and a list of incident ids, runs some pre-flight safety checks, decides an
> **outcome for every incident**, acts on the ones that need escalation, and
> writes a single report at the end. This is an unattended batch run — never
> ask the user anything.
>
> Use `http://localhost:4600/v1` as the incident-management API host (a local
> mock — see `api/` and the README to start it).
>
> **Scale note:** this workflow is large on purpose — the pre-flight pipeline,
> the per-incident classification, the tally gate, and the action/report split
> together should total **at least 20 branch conditions**. Do not collapse the
> classification rules into fewer branches than listed below.
>
> **What it should do:**
>
> 1. **Start.** Take a `incident_policy_file` input (default
>    `examples/incident_postmortem_pipeline/config/incident_policy.json`) and an
>    `incidents_file` input (default
>    `examples/incident_postmortem_pipeline/incidents.txt`, one incident id per
>    line).
>
> 2. **Pre-flight, in order — any failure aborts the whole run:**
>    - **Load the policy.** Read org, oncall_period_id, the `process_approved`
>      flag, the incident commander / on-call lead contact, the legal/compliance
>      contact, the `sla_minutes_by_severity` map, `min_incidents_required`, and
>      the escalation budgets. If the process is **not approved**, abort.
>    - **Verify the on-call schedule.** Confirm an active on-call schedule is
>      loaded for the `oncall_period_id` — if there is **no on-call schedule
>      configured** for the period, abort.
>    - **Check the incident-management platform status.** Query
>      `http://localhost:4600/v1/ims-status`; if the **IMS backend itself is
>      down**, abort — the incident data can't be trusted while it's down.
>
> 3. **Decide an `outcome` for every incident in the list.** For each incident
>    the per-item facts come from a deterministic lookup: query
>    `http://localhost:4600/v1/incident?id={incident_id}` and read severity,
>    status (resolved / open / auto-resolved), customer impact, duration,
>    whether it matches a known recurring pattern, whether it's security/
>    compliance-related, the assigned engineer (or blank), and the time to
>    acknowledge. Apply the severity's SLA minutes from the policy to flag an
>    SLA breach. Also note whether the lookup failed or the incident was
>    unknown. Gather these facts **per item** — do not have the model decide
>    the outcomes itself.
>
> 4. **Classification rules** (per incident; check in this exact priority order
>    — first match wins):
>    - Lookup fails / non-200 / unreadable → `outcome = "error"`.
>    - Incident **not on file** in the incident-management system →
>      `outcome = "not_found"`.
>    - Incident is **security/compliance-related** (e.g. a data breach or
>      credential leak) → `outcome = "security_review"` (special legal-aware
>      process — never just a normal postmortem).
>    - **Sev1/Sev2 severity OR customer-facing impact** →
>      `outcome = "mandatory_postmortem"` (full blameless postmortem required).
>    - Incident **matches a recurring pattern** (a known prior incident
>      signature) → `outcome = "recurring_escalation"` (root-cause escalation to
>      engineering leadership).
>    - Incident is **auto-resolved with no customer impact** →
>      `outcome = "no_action"` (closed itself out cleanly — minimal logging
>      only). Check this before the no-engineer rule so a clean auto-resolution
>      is never mistaken for a staffing gap.
>    - **No engineer was ever assigned** → `outcome = "process_gap"` (flag the
>      on-call process itself to the on-call manager).
>    - **Acknowledgment time exceeded the severity's SLA** →
>      `outcome = "sla_breach"` (escalate regardless of severity).
>    - Otherwise (Sev3/minor, non-recurring, no breach) →
>      `outcome = "quick_writeup"` (lightweight writeup only).
>
> 5. **Each decided record** holds: `incident_id`, `outcome`, `severity`,
>    `status`, `customer_impact`, `is_recurring`, `is_security_related`,
>    `assigned_engineer`, `time_to_acknowledge_minutes`, `sla_breached`, and a
>    short human-readable `note`. For `security_review`, `mandatory_postmortem`,
>    `recurring_escalation`, `process_gap`, and `sla_breach` outcomes, also set
>    `needs_attention: true`. Collect all decided records into a single list.
>
> 6. **Tally.** Count incidents by outcome. If the count of `mandatory_postmortem`
>    plus `recurring_escalation` exceeds the policy's
>    `mandatory_postmortem_budget` plus `recurring_escalation_budget` combined,
>    escalate the whole batch to the incident commander as a trend alert (too
>    many serious incidents this on-call period). If there are **any**
>    `security_review` items, notify the legal/compliance contact before
>    continuing. If **fewer than `min_incidents_required`** incidents resolved
>    (i.e., total processed minus error/not_found), abort.
>
> 7. **Act**, per outcome:
>    - `mandatory_postmortem` → schedule a full blameless postmortem meeting.
>    - `recurring_escalation` → open a root-cause-analysis ticket and notify
>      engineering leadership.
>    - `security_review` → route to the security/legal queue.
>    - `process_gap` → flag the incident to the on-call manager.
>    - `quick_writeup` → log a lightweight writeup with minimal follow-up.
>    - `no_action` → log with no further follow-up.
>    - `sla_breach` → escalate to the incident commander regardless of
>      severity.
>    - `error` and `not_found` need no action beyond being recorded.
>
> 8. **Report.** Write a `postmortem-report-<today>.json` (today's date,
>    `YYYY-MM-DD`) with a `summary` (org, oncall_period_id, totals, a per-
>    outcome `counts` map) and the full per-incident `results` list. An aborted
>    run instead writes `postmortem-abort-<today>.json` with the abort reason.
>
> Keep the incident-management API host, the input file names, and the SLA /
> escalation-budget / min-incidents thresholds easy to change. A single bad
> incident must produce an `error` / `not_found` record for that item — it must
> never stop the rest of the batch.

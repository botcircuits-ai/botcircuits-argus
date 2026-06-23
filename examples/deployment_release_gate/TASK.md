# Workflow name:  `deployment_release_gate`

---

# Instruction Prompt

> Create a **DevOps release-gate** workflow that decides, fully unattended,
> which microservices in a release are safe to promote to production. It reads a
> release manifest and a list of services, runs some pre-flight checks, decides
> an **outcome for every service**, and writes a single report at the end. This
> is an unattended batch run — never ask the user anything.
>
> Use `http://localhost:4100/v1` as the DevOps API host (a local mock — see
> `api/` and the README to start it).
>
> **What it should do:**
>
> 1. **Start.** Take a `release_config_file` input (default
>    `examples/deployment_release_gate/config/release.json`) and a
>    `services_file` input (default `examples/deployment_release_gate/services.txt`,
>    one microservice name per line).
>
> 2. **Pre-flight, in order — any failure aborts the whole release:**
>    - **Load the manifest.** Read release_id, target_environment, change_ticket,
>      the `approved` flag, the rollback plan, the error/latency budgets, and
>      `min_services_required`. If the release is **not approved**, abort.
>    - **Verify the change ticket.** If **no rollback plan** is documented, abort.
>    - **Check the change-freeze window.** Query
>      `http://localhost:4100/v1/freeze`; if a **freeze is active**, abort.
>
> 3. **Decide an `outcome` for every service in the list.** For each service the
>    per-item facts come from a deterministic lookup: query
>    `http://localhost:4100/v1/health?service={service}` (plus the freeze
>    endpoint) and read CI status, error rate, p95 latency, open SEV incidents,
>    and whether the service has a prior baseline; also note whether the lookup
>    failed or the service was unknown. Gather these facts **per item** — do not
>    have the model decide the outcomes itself.
>
> 4. **Classification rules** (per service; check failures first):
>    - Lookup fails / non-200 / unreadable → `outcome = "error"`.
>    - Service **not in the deploy catalog** → `outcome = "not_found"`.
>    - Latest **CI pipeline failed** → `outcome = "block"`.
>    - An **open SEV incident** → `outcome = "block"` (page on-call).
>    - **Error rate over budget** → `outcome = "hold"`.
>    - **p95 latency over budget** → `outcome = "hold"`.
>    - **No prior baseline** (first-time deploy) → `outcome = "canary"`.
>    - Otherwise → `outcome = "promote"`.
>
> 5. **Tally.** Count promotable (promote + canary), canary, and blocker
>    (block / hold / error / not_found) services. If **fewer than
>    `min_services_required`** are promotable, abort. If there are **any
>    blockers**, send a blocker notification before continuing.
>
> 6. **Act.** Promote the `promote` services (blue/green). If there are any
>    `canary` services, start a canary rollout for them.
>
> 7. **Report.** Write a `release-report-<today>.json` (today's date,
>    `YYYY-MM-DD`) with a `summary` (release_id, environment, totals, a per-
>    outcome `counts` map) and the full per-service `results` list. An aborted
>    run instead writes `release-abort-<today>.json` with the abort reason.
>
> Keep the DevOps API host, the input file names, and the error/latency/
> min-services thresholds easy to change. A single bad service must produce an
> `error` / `not_found` record for that item — it must never stop the rest of
> the batch.

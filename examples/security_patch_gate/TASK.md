# Workflow name:  `security_patch_gate`

---

# Instruction Prompt

> Create a **security-finding auto-patch** workflow that takes ONE reported
> vulnerability finding for a small internal application, decides whether
> it's approved for an unattended auto-patch, has the agent apply the fix
> directly to the application's source file, and writes a single patch
> report at the end. This is an unattended run — never ask the user
> anything.
>
> **What it should do:**
>
> 1. **Start.** Take a `finding_file` input (default
>    `examples/security_patch_gate/config/finding.json`) — a single JSON
>    object describing one security finding: `finding_id`, `repo`, `file`
>    (the vulnerable source file, relative to
>    `examples/security_patch_gate/`), `function`, `severity`, `title`,
>    `description`, `remediation_hint`, and an `approved_for_auto_patch`
>    flag.
>
> 2. **Load the finding.** Read every field above from `finding_file`.
>
> 3. **Gate on approval.** If `approved_for_auto_patch` is **false**, abort
>    the whole run and write an abort report — never patch a finding nobody
>    signed off on for unattended remediation.
>
> 4. **Patch.** If approved, open the vulnerable `file` and apply a fix for
>    the described vulnerability, guided by `description` and
>    `remediation_hint` — e.g. replacing an unsafe pattern-matching /
>    coercion-based comparison with an exact, type-checked equality check.
>    Make the smallest change that closes the hole; do not restructure
>    unrelated code. Save the file in place.
>
> 5. **Verify.** This step is automatic and not something you author: the
>    build's generated verification gate runs the application's OWN unit
>    test suite (`npm test` inside `examples/security_patch_gate/app/`) as
>    a blocking check — it must pass both the pre-existing behavioral tests
>    (legitimate logins still work) and the security regression tests
>    (the bypass is closed) for the run to be considered successful.
>
> 6. **Report.** Write a `patch-report-<current_date>.json` (today's date,
>    `YYYY-MM-DD`, in `examples/security_patch_gate/`) with the
>    `finding_id`, `file` patched, a short human-readable `summary` of the
>    change made, and `status: "patched"`. An aborted run (not approved)
>    instead writes `patch-abort-<today>.json` with `status: "aborted"` and
>    the abort reason.
>
> Keep the finding file's path and its field names easy to change. There is
> only one finding per run — no batch/list processing needed here.

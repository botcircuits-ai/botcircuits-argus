---
name: botcircuits-coding-workflow-authoring
description: Author a TASK-SPECIFIC coding workflow for a software implementation task and wire it into the safe_agentic_workflow delivery pipeline. Use whenever the user asks to implement, build, add, or fix a software feature as a coding task (e.g. "implement user login", "add a password-reset endpoint") in a project that has a .botcircuits/workflows directory — or whenever the safe_agentic_workflow's author_coding_workflow step dispatches to you. Includes the task-size triage: trivial one-step requests are executed directly, without generating a workflow.
---

# Authoring a Coding Workflow

When the user asks for a **coding task** (implement / build / add / fix a
feature), do NOT free-form the implementation. Instead, generate a
**task-specific coding workflow** and run it through the standing
`safe_agentic_workflow` delivery pipeline. This extends the general
`botcircuits-workflow-authoring` skill; everything there (file shape, step
types, build command) applies here too — this document adds the
coding-task-specific contract.

> **Use ONLY this skill (plus botcircuits-workflow-authoring for the base
> mechanics). Do NOT read the BotCircuits sources (`src/`, `tests/`).** This
> document is the contract.

## The two-workflow architecture

- **`safe_agentic_workflow`** (already in `.botcircuits/workflows/`) is the
  STATIC, task-agnostic delivery pipeline: requirements gate → author coding
  workflow → run coding workflow → QAS gate → PR shepherd → architect review →
  automated review → human approval → docs. You never regenerate it per task.
- **The coding workflow** is what YOU author per task: the concrete, ordered
  code-generation steps for exactly this task. `safe_agentic_workflow` calls
  it (via `botcircuits workflow run`) from its `run_coding_workflow` step and
  re-runs it with findings whenever a review gate rejects.

## Step 0 — task-size triage (ALWAYS do this first)

Not every coding request deserves a workflow. Before authoring anything,
mentally decompose the request into implementation steps, then apply this
deterministic rule:

| Signal | Route |
| --- | --- |
| Decomposes into **3+ ordered deliverables**, or touches **2+ layers** (db / backend / UI), or introduces a new data model or API surface | **Workflow path** — continue with this skill. |
| Decomposes into **1–2 steps**, single file/layer, one imperative sentence (e.g. "fix the API exception handler", "add an env variable to the Docker image", "change the CSS background color") | **Direct path** — just do the task now, in this session. NO coding workflow, NO pipeline run. |
| **Investigation, not construction** (e.g. "check the logs and find the bug", "why is X failing?") | **Direct path** — investigate and report. If the *fix* then turns out to be feature-sized, re-enter this skill with the fix as the task. |

Rules for the direct path: do the work with normal care (read the code,
make the change, verify), and do NOT create anything under
`.botcircuits/workflows/coding/` or touch the task registry. The pipeline's
review gates exist to de-risk multi-step feature work; for a one-step change
they only add cost.

When in doubt (borderline 2–3 steps), prefer the direct path — a task that
grows can always be promoted to a workflow later; an unnecessary workflow is
pure overhead.

## Task registry — binding a task to its workflow (and to the conversation)

`.botcircuits/workflows/coding/tasks.json` is the registry that aligns one
coding workflow file with one user task across conversation turns and
sessions. Shape (one entry per task, keyed by task slug):

```json
{
  "user_login": {
    "workflow": "user_login_workflow",
    "status": "in_progress",
    "request": "implement user login with email + password",
    "created": "2026-07-04",
    "updated": "2026-07-04"
  }
}
```

`status` is one of `in_progress`, `done` (human approved / merged), or
`abandoned`.

**On every workflow-path request, resolve against the registry BEFORE
authoring:**

1. Read `tasks.json` (treat a missing file as `{}`).
2. If the request matches an existing entry (same slug, or clearly the same
   feature as its `request` — "continue the login task", "the login form
   should also…"), this conversation **continues that task**: edit/rerun its
   existing workflow instead of creating a new one. If its pipeline run is
   paused (waiting on `clarify_requirements` or `human_approval`), resume it
   with `botcircuits workflow run --name safe_agentic_workflow --reply "..."`
   rather than starting a fresh run.
3. If nothing matches, this is a **new task**: create the entry with
   `status: in_progress`, then proceed to author.
4. Update `status`/`updated` when the pipeline reaches human approval
   (`done`) or the user drops the task (`abandoned`).

**One task in flight at a time.** The engine keeps a single pause/resume
cursor per workflow name, so only one `safe_agentic_workflow` run can be
active in a project. If the registry already has a different task
`in_progress` and the user starts a new one, tell them and ask whether to
finish, abandon, or switch — do not silently start a second pipeline run.

## Steps

1. **Derive the task name.** Slug-safe (`^[a-zA-Z0-9_-]+$`), from the user's
   intent: "implement user login" → `user_login`. The workflow name is always
   `<task-name>_workflow` (e.g. `user_login_workflow`).

2. **Write the coding workflow JSON** to
   `.botcircuits/workflows/coding/<task-name>_workflow.json` (create the
   `coding/` directory if missing). If that file already exists for this exact
   task, edit it instead of re-authoring from scratch.

3. **Build it:**

   ```
   botcircuits workflow build --name <task-name>_workflow
   ```

   The builder finds sources in the `coding/` subdirectory automatically.

4. **Kick off the pipeline** (unless you were dispatched FROM the pipeline's
   `author_coding_workflow` step — in that case just report
   `coding_workflow_name` and stop):

   ```
   botcircuits workflow run --name safe_agentic_workflow \
     --initial-args '{"task_description": "<the task>", "coding_workflow_name": "<task-name>_workflow"}'
   ```

## Coding workflow contract

The generated workflow MUST follow this shape (in addition to the base
authoring rules):

- **Required variables** — declare intent via step actions; the build
  aggregates `flow.variables`. Every coding workflow works with:
  - `task_description` (string) — the original task.
  - `acceptance_criteria` (string) — the definition of done, supplied by the
    pipeline.
  - `revision_findings` (string) — defect findings from a review gate. Empty
    on the first pass.
- **Revision-aware entry.** The first step after `start` must branch:
  if `revision_findings` is non-empty, jump to a `fix_findings` step
  ("Fix EXACTLY the defects described in revision_findings — nothing else,
  then stop") and terminate; otherwise proceed to the full implementation
  chain. This keeps review-gate reruns cheap and targeted.
- **Task-specific steps, one deliverable each.** Decompose the task into a
  DETERMINISTIC ordered chain of `agentAction` steps, each producing one
  concrete code deliverable, ordered by dependency. Example for
  "implement user login":

  ```
  start → check_revision → db_table_design → backend_db_config
        → auth_service → login_api_endpoint → login_ui_form
        → wire_ui_to_api → tests
  ```

  Name steps after the deliverable (`db_table_design`, `login_api_endpoint`),
  not after roles. Each `settings.action` states: what to build, in which
  file/layer, and which acceptance criterion it satisfies.
- **Deterministic as possible.** Default to a LINEAR chain. Add `conditions`
  only where the task genuinely branches on an observable fact (e.g. "table
  already exists → skip migration"). Never add exploratory/"decide what to do
  next" steps — deciding is the author's job, now, not the runtime's.
- **No `question` steps.** The coding workflow runs nested inside the
  pipeline; requirements clarification already happened at the pipeline's
  requirements gate. If information is missing, that is a requirements-gate
  failure, not a mid-coding question.
- **No review/QA steps.** QAS, architect review, automated review, and human
  approval live in `safe_agentic_workflow`. The coding workflow only builds.
  (A final `tests` step that WRITES tests is fine; a step that judges the
  implementation is not.)

## Worked example — "implement user login"

`.botcircuits/workflows/coding/user_login_workflow.json`:

```json
{
  "name": "user_login_workflow",
  "description": "Task-specific coding workflow: implement user login per the acceptance criteria.",
  "flow": {
    "start": "start",
    "steps": {
      "start": { "type": "start", "next": "check_revision" },
      "check_revision": {
        "type": "agentAction",
        "settings": { "action": "Check revision_findings. If it is non-empty, this run is a targeted fix pass; otherwise it is a full implementation pass." },
        "next": "db_table_design",
        "conditions": [
          { "condition": "revision_findings is not empty", "next": "fix_findings" }
        ]
      },
      "fix_findings": {
        "type": "agentAction",
        "settings": { "action": "Fix EXACTLY the defects described in revision_findings — modify only the files/behavior those findings name, verify the fix compiles/passes, and change nothing else." }
      },
      "db_table_design": {
        "type": "agentAction",
        "settings": { "action": "Design and create the users table (schema/migration): id, email (unique), password_hash, created_at. Satisfies: persistent user accounts." },
        "next": "backend_db_config"
      },
      "backend_db_config": {
        "type": "agentAction",
        "settings": { "action": "Configure the backend database connection and the User data-access layer for the users table." },
        "next": "login_api_endpoint"
      },
      "login_api_endpoint": {
        "type": "agentAction",
        "settings": { "action": "Implement POST /login: validate credentials against password_hash, issue a session/JWT on success, 401 on failure. Satisfies: user can authenticate." },
        "next": "login_ui_form"
      },
      "login_ui_form": {
        "type": "agentAction",
        "settings": { "action": "Implement the login form UI (email + password, client-side validation) and wire it to POST /login, storing the session on success. Satisfies: user can log in from the UI." },
        "next": "tests"
      },
      "tests": {
        "type": "agentAction",
        "settings": { "action": "Write automated tests covering: successful login, wrong password, unknown email, and session issuance — one per acceptance criterion." }
      }
    }
  }
}
```

Then:

```
botcircuits workflow build --name user_login_workflow
botcircuits workflow run --name safe_agentic_workflow \
  --initial-args '{"task_description": "implement user login", "coding_workflow_name": "user_login_workflow"}'
```

## Editing

To revise a coding workflow (task scope changed), read its file under
`.botcircuits/workflows/coding/`, rewrite the full `flow.steps` map (keep the
same `name` and `flow` wrapper), and rebuild. The build replaces the built
copy whole.

---
name: botcircuits-coding-workflow-authoring
description: Author the per-task CODING workflow that the safe_agentic_workflow pipeline generates and runs. Use ONLY from the pipeline's author_coding_workflow step — it is the contract for the generated workflow's shape. Not for general workflow authoring (that is botcircuits-workflow-authoring).
---

# Authoring a per-task Coding Workflow

The static **`safe_agentic_workflow`** pipeline (requirements → code
understanding → docs → planning → **author** → run → validate → gate loop →
docs) is the entry point for every coding task. Its `author_coding_workflow`
step generates ONE per-task coding workflow from the plan; its
`run_coding_workflow` step executes it; its `validation_gate` loops back to
`run_coding_workflow` with `revision_findings` when tests fail or the
acceptance criteria are unmet.

This skill is the contract for **what that generated coding workflow must
look like**. It is NOT for authoring arbitrary workflows — use
`botcircuits-workflow-authoring` for those.

> Everything you need is here. Do not read the engine/builder sources to
> author the coding workflow.

## Division of responsibility — keep review OUT of the coding workflow

The pipeline owns navigation and review; the generated coding workflow only
implements. This split is deliberate (determinism-first: navigation is
decided at author time, not runtime).

| Belongs in the PIPELINE (never in the coding workflow) | Belongs in the CODING workflow |
| --- | --- |
| Requirements, planning | The actual code changes |
| The acceptance / QAS / architect / human review gates | A targeted fix pass driven by `revision_findings` |
| The validate → gate → loop-back control | One coding step per relevant area from the plan |
| Documentation summary | A final self-check of its own output |

So the generated coding workflow contains **NO** `question` steps, **NO**
review/approval steps, and **NO** loop-back gates. It is a linear,
deterministic chain the pipeline calls repeatedly.

## Required shape

1. **Start with a `check_revision` branch.** The pipeline may call the
   coding workflow more than once. On a re-run it passes `revision_findings`
   (the defects the gate found). The first step branches on it:
   - `revision_findings is not empty` → a **`fix_findings`** step: a targeted
     pass that addresses ONLY those findings, then continues to the normal
     coding steps (or straight to the self-check if the fix is self-contained).
   - otherwise → the first normal coding step.

2. **One coding step per relevant area** from the plan — the count is
   **dynamic**: as many `agentAction` steps as the plan has areas (frontend,
   backend, data layer, a specific module, tests, …). Each step implements
   its area against the acceptance criteria, in plan order.

3. **End with a `self_check` step**: the workflow reviews its own output for
   obvious breakage (syntax, imports, the change actually applied) and
   reports what it did. This is a light self-check, NOT the acceptance gate —
   the pipeline's `validation_gate` runs the real tests and judges acceptance.

## Variables

- `revision_findings` — **input**: the gate's defects on a re-run; empty on
  the first pass. The `check_revision` branch tests it.
- one produced variable per coding step is optional; keep it simple.

## Authoring + build command

Write the intent-only source (NL `conditions`, no compiled mechanics — the
builder compiles them) to a file, then generate + build it INTO the coding
subdir:

```
botcircuits workflow generate \
  --from <intent_source_file> \
  --name <coding_workflow_name> \
  --subdir coding \
  --build
```

The runnable artifact lands at
`.botcircuits/workflows/.build/coding/<coding_workflow_name>.json` and runs
by its plain name: `botcircuits workflow run --name <coding_workflow_name>`.

## Skeleton (intent-only)

```json
{
  "name": "<task_slug>_workflow",
  "description": "Coding workflow for <task>: fix-on-revision, then implement each area, then self-check.",
  "flow": {
    "start": "check_revision",
    "variables": [
      { "variableName": "revision_findings", "description": "Defects the acceptance gate found on a prior pass; empty on the first run.", "input": true }
    ],
    "steps": {
      "check_revision": {
        "type": "agentAction",
        "settings": { "action": "If revision_findings is non-empty, note that this is a fix pass targeting exactly those findings." },
        "conditions": [
          { "condition": "revision_findings is not empty", "next": "fix_findings" }
        ],
        "next": "implement_area_1"
      },
      "fix_findings": {
        "type": "agentAction",
        "settings": { "action": "Address ONLY the defects in revision_findings. Make the minimal change that resolves each; do not re-do unaffected areas." },
        "next": "self_check"
      },
      "implement_area_1": {
        "type": "agentAction",
        "settings": { "action": "<implement the first area from the plan against the acceptance criteria>" },
        "next": "implement_area_2"
      },
      "implement_area_2": {
        "type": "agentAction",
        "settings": { "action": "<implement the next area; repeat one step per area — dynamic count>" },
        "next": "self_check"
      },
      "self_check": {
        "type": "agentAction",
        "settings": { "action": "Review the changes just made for obvious breakage (syntax, imports, change actually applied). Report what was implemented per area. Do NOT run the acceptance gate — the pipeline validates with the real tests." }
      }
    }
  }
}
```

## Checklist before building

- [ ] `start` is `check_revision`.
- [ ] `check_revision` branches on `revision_findings` (not-empty → `fix_findings`).
- [ ] One coding step per plan area, in order.
- [ ] A trailing `self_check` step.
- [ ] NO `question` steps, NO review/approval steps, NO loop-back gate.
- [ ] Generated with `--subdir coding --build`.

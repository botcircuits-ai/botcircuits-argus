# 5. Workflows

[← Index](00-index.md)

---

A **workflow** is a repeatable, multi-step process with a predictable outcome —
order fulfillment, loan triage, an onboarding sequence. Where a conversation lets
the model improvise, a workflow runs a defined set of steps.

## Engine-driven, not model-driven

The key idea: once a workflow starts, **the engine owns the loop**. It walks the
steps, calls the model only when a step genuinely needs language or judgment, and
makes every branch decision itself with deterministic rules.

```
conversation matches a workflow
            │
            ▼
   ┌──────────────────┐
   │  Workflow Engine │   walks steps, decides branches in code,
   │                  │   calls the model only when needed
   └──────────────────┘
            │
            ▼
   returns a result to the conversation
```

This gives two things at once:

- **Predictable** — the same input takes the same path every time. Branch
  decisions are evaluated by rules, not guessed by the model.
- **Efficient** — the model is used only where it adds value, so a workflow runs
  in far fewer tokens than letting the model drive every step.

## How a workflow is built

You describe the workflow as **intent** — steps, plain-language instructions, and
plain-language branch conditions. Then `workflow build` compiles that into a
runnable form. You never hand-write the low-level rules; the build step generates
them.

You can also start from a plain-language description and let `workflow generate`
draft the source for you, then build it — so the whole path is
**describe → generate → build → run**, with a human review point in the middle.

See the [Workflow Authoring Guide](06-workflow-authoring-guide.md) for how to
write one.

## The building blocks (overview)

| Concept | What it is |
|---|---|
| **Step** | One unit of work — do something, ask the user, or decide. |
| **Variable** | A fact the workflow tracks and branches on. |
| **Condition** | A plain-language branch rule (compiled to an exact rule). |
| **listDecision** | A step that decides an outcome for every item in a list. |
| **Result** | The structured final answer the engine returns. |

## Where work happens

A workflow can do its work three ways, cheapest first:

1. **In the engine (no AI)** — deterministic lookups and computations (read a
   file, run a script per item, compare numbers).
2. **In one model call** — when a step needs language understanding.
3. **By asking the user** — when only the user has the answer.

Pushing work toward the engine is what makes workflows both deterministic and
cheap.

Next: [Memory](07-memory.md). To author one: [Workflow Authoring Guide](06-workflow-authoring-guide.md).

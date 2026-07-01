---
name: botcircuits-workflow-authoring
description: Create or edit a BotCircuits workflow from a natural-language description. Use whenever the user asks to create, author, design, build, or edit a workflow / journey / process flow (e.g. "create an order fulfillment workflow with ...", "add a refund branch to the loan workflow").
---

# Authoring a BotCircuits Workflow

When the user asks to **create or edit** a workflow (e.g. _"create an order
fulfillment workflow with ..."_), turn their description into a runnable
workflow. A workflow is a deterministic state machine the **workflow-running**
skill later executes; authoring is generation + validation, performed by you in
this session.

> **Use ONLY this skill. Do NOT read the BotCircuits sources (`src/`, `tests/`) to author
> a workflow.** Everything you need — the file shape, every step type, the
> `listDecision` wiring, and the build command — is documented below. Reading the
> engine/builder/validator code to "check how it works" wastes tokens and is not
> required; this document is the contract. (You still read the *user's* data
> files — e.g. an `itemSource` file — to validate references, just not the
> framework internals.)

## The user speaks business, you supply the mechanics

Assume the user describes **only high-level business logic** — "check every
parcel and flag the late ones", "screen each applicant", "for each line item,
charge it or back-order it". They do NOT know the step types, `listDecision`,
`itemFacts`, condition ordering, or the self-loop pitfall, and you must NOT make
them. **It is your job to infer the technical wiring from their words.** Never
ask the user "should this be a `listDecision`?" or "exec or LLM facts?" — those
are your decisions to derive. Use this table to translate intent → wiring:

| Business-language cue | What it means technically |
| --- | --- |
| "for every / each item", "all the parcels", "go through the list", same decision repeated over a collection | A single **`listDecision`** step — NOT a `next_item → do_thing → record → loop` self-loop (see below). |
| "read a list / file of X", "one per line", "the orders in this file" | The list is the `itemSource` `{file, path}`. Plain one-per-line text → `path: ""`. |
| "look it up", "query the API", "fetch its status", "check the record" — a deterministic per-item lookup | Gather facts with **`itemFacts` (kind `exec`)** so the ENGINE does it per item with no LLM call. Only fall back to model-reported facts when no script/endpoint exists. |
| "check failures / errors / invalid first", "if it errors, stop checking" | Put failure/error/not-found `conditions` **first** — first match wins, top-to-bottom. |
| "status is A or B or C → same outcome" | Write **separate** condition entries (one comparison each) all pointing at the same decision word — never an OR-condition. |
| "otherwise", "by default", "anything else" | The `defaultNext` decision word (for `listDecision`) or the step's own `next` (for a normal branching step). |
| "ask the user", "confirm with them", "prompt for X" | A **`question`** step. |
| "unattended", "batch run", "never ask anyone" | NO `question` steps anywhere. |
| "the facts I decide on" (in_stock, is_overdue, risk_band) | The `itemVariables` the conditions test. |
| "collect the results", "write them all out", "one record per item" | `collectInto` (the result list) + `decisionKey` (the per-record outcome field). |

The worked `listDecision` example below corresponds exactly to a request like
_"check the live status of many parcels from a file and write one results file"_
— the user never says "listDecision"; you recognize the **list + per-item
decision + deterministic lookup** shape and reach for it. If a request has NONE
of these cues (a fixed linear or branching process), use ordinary
`agentAction` / `question` / `systemAction` steps instead.

## Steps

1. **Clarify if needed.** Ask ONE focused round of questions ONLY about
   *business* ambiguity — what the outcomes are, where the data lives, whether a
   bad item should stop the batch — never about technical wiring (step types,
   fact-gathering mechanism). If the business logic is clear, proceed.

2. **Write the workflow JSON** to `.botcircuits/workflows/<name>.json` (the
   human-editable source of truth). Pick a slug-safe `name`
   (`^[a-zA-Z0-9_-]+$`) from the user's intent (e.g. "order fulfillment" →
   `order_fulfillment`); it doubles as the filename and the run identifier.

3. **Build it** — this compiles natural-language `conditions` into deterministic
   `choices` + an aggregated `flow.variables` list, and writes the runnable copy
   to `.build/`:

   ```
   botcircuits workflow build --name <name>
   ```

   Only built workflows are runnable.

4. **Confirm** to the user: name, what it does, and the step/branch outline.

## Workflow shape

The `start` and `steps` live under a **`flow`** wrapper. `botcircuits workflow
build` reads `record.flow`; a file with `start`/`steps` at the top level fails
with `missing flow; nothing to index`.

```json
{
  "name": "order_fulfillment",
  "description": "when to run this workflow",
  "flow": {
    "start": "start",
    "steps": {
      "start": { "type": "start", "next": "check_stock" },
      "check_stock": {
        "type": "agentAction",
        "settings": { "action": "Check stock for the order items." },
        "next": "backorder",
        "conditions": [
          { "condition": "all items are in stock", "next": "ship" }
        ]
      },
      "ship":      { "type": "agentAction", "settings": { "action": "Ship the order." } },
      "backorder": { "type": "agentAction", "settings": { "action": "Create a backorder and notify the customer." } }
    }
  }
}
```

Rules:
- Step types: `start` (entry, no action), `agentAction` (the runtime performs
  `settings.action`), `question` (ask the user; `settings.action` is the
  question), `systemAction` (engine-side bookkeeping, no LLM), `listDecision`
  (decide an outcome for **every item in a list** — see below).
- Branching lives at the **step root** under `conditions` (sibling of
  `type`/`next`/`settings`, NOT inside `settings`). Each entry is
  `{"condition": "<NL test>", "next": "<step_id>"}`. The step's own `next` is
  the default ("otherwise") branch — do NOT add a literal "otherwise" entry.
- A branching step needs BOTH `conditions` (real branches) AND `next`
  (default). A step with neither is terminal.

## Pinning a step to a different model (optional, rarely needed)

Every step runs on the run's default model unless you say otherwise. Only
reach for this when a step genuinely benefits from a different model — a
cheap/fast model for a trivial extraction step, a stronger model for a hard
planning/judgment step, or (for `claude-code`/`codex` runtimes) routing a step
to a different subscription CLI entirely. Most workflows don't need this —
don't add it just because it's available.

Declare a top-level `agents` map (name → `{runtime?, model?}`) and reference
a name from any step's `agent` field:

```json
{
  "agents": {
    "researcher": { "runtime": "codex", "model": "o3" },
    "writer":     { "model": "claude-opus-4-7" }
  },
  "flow": {
    "steps": {
      "fetch_trends": { "type": "agentAction", "agent": "researcher", "settings": {...} },
      "write_report": { "type": "agentAction", "agent": "writer", "settings": {...} }
    }
  }
}
```

`runtime` is optional (omit it to stay on the run's own host CLI/model and
just override `model`). Every `agent` value MUST have a matching entry in the
top-level `agents` map — `botcircuits workflow build` flags an unknown
reference as a validation issue.

## Iterating over a list — use `listDecision`, NOT a self-loop

When the process applies the **same decision to every item in a collection**
(check each parcel, price each line item, screen each applicant), DO NOT build a
manual loop (`next_item → do_thing → record → next_item` with an
LLM-maintained "all processed" flag). That pattern makes the model — not the
engine — drive iteration: only the few segments the engine sees get traced, the
per-item work happens inside one LLM call, and the run is neither deterministic
nor per-item auditable.

Instead use a single **`listDecision`** step. The engine fans the step's
`conditions` across each element of the list and decides each one
deterministically, collecting one result record per item. For example,
fulfilling an order's line items (one decision per item against stock):

```json
"decide_line_items": {
  "type": "listDecision",
  "settings": { "action": "Decide each order line item against available stock." },
  "itemSource": { "file": "data/current_order.json", "path": "items" },
  "itemVariables": [
    { "variableName": "in_stock", "description": "whether the sku is in stock" },
    { "variableName": "enough", "description": "stock covers the requested qty" }
  ],
  "decisionKey": "decision",
  "collectInto": "line_results",
  "conditions": [
    { "condition": "the sku is not in stock",       "next": "reject" },
    { "condition": "in stock but not enough for qty", "next": "backorder" }
  ],
  "defaultNext": "fulfill",
  "next": "save_results"
}
```

`listDecision` rules:
- **`conditions[].next` and `defaultNext` are DECISION WORDS for the item**
  (`reject`, `backorder`, `fulfill`), **NOT** step ids. `defaultNext` is the
  fallback decision word for an item that matches no condition. The first
  matching condition wins (top-to-bottom), so order them — put failure / error
  checks first.
- **`next` is the real STEP the flow continues to after the WHOLE list is
  decided** (e.g. `save_results`), or omit it to make the `listDecision`
  terminal. This is the one place `next` is a step id rather than a decision
  word — that is why the default decision word goes in `defaultNext`, not `next`.
- **One condition → one comparison. Do NOT write OR-conditions.** A condition
  like `"status is exception or returned or lost"` compiles to a SINGLE branch
  (only `exception` matches) and silently drops the rest. Write three separate
  entries, each pointing at the same decision word:
  `{"condition": "status is exception", "next": "escalate"}`,
  `{"condition": "status is returned", "next": "escalate"}`,
  `{"condition": "status is lost", "next": "escalate"}`.
- `itemSource` `{file, path}` points at the list. `path` is a JSON path into the
  file (e.g. `"items"`), OR `""` for a plain **one-item-per-line text file** — in
  that case each line becomes an item `{"value": "<line>"}`, so an `itemFacts`
  `command` interpolates the line as `{value}` and `derive` reads it via
  `{"from_item": "value"}`. `itemVariables` are the per-item facts the
  `conditions` test.
- If each item's facts come from running a script/HTTP-style lookup
  **deterministically**, add `itemFacts` (kind `exec`) so the ENGINE gathers
  them per item with NO AI call. Omit it to have the model report the per-item
  facts in one call. Prefer deterministic where possible. Shape:
  ```json
  "itemFacts": {
    "kind": "exec",
    "command": ["python3", "bin/lookup.py", "{value}"],
    "parse": "json",
    "derive": {
      "tracking_number": { "from_output": "tracking_number" },
      "status":          { "from_output": "status" },
      "failed":          { "from_output": "failed", "default": false }
    }
  }
  ```
  `derive` rules (tiny by design): `{"from_item": "k"}` (item field),
  `{"from_output": "k", "default": d}` (parsed-stdout field), `{"literal": v}`,
  `{"ge": [a, b]}` (numeric `a >= b`, where a/b are `"item.x"`/`"output.y"` refs
  or literals). The script should emit ONE flat JSON object per item; compute
  anything non-trivial (date math, failure flags) inside the script.
- `collectInto` names the slot that receives the list of decided records;
  `decisionKey` names the field on each record holding its decision word.
  Optionally `nullOn` `{field: [decisionWords]}` blanks a field for some
  outcomes (e.g. a rejected line has no total: `{"line_total": ["reject"]}`).

One `listDecision` replaces the entire `next_item`/`do_thing`/`record`/loop-back
subgraph. The builder compiles its NL `conditions` into rule expressions just
like any other step.

## Editing

To change an existing workflow, read its raw
`.botcircuits/workflows/<name>.json`, apply the change to the full `flow.steps`
map, overwrite the file (keep the same `name` and the `flow` wrapper), then
rebuild. The build always replaces the built copy whole.

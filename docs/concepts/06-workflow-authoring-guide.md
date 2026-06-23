# Workflow Authoring Guide

How to write a BotCircuits workflow. This is the end-user reference: what a
workflow file looks like, the step types, and every property you can set.

---

## 1. How authoring works

You describe a workflow as **intent** — in plain language and simple structure.
You then run **`workflow build`**, which compiles that intent into a runnable
form. You write the *what*; build figures out the *how*.

```
your workflow.json   ──►   workflow build   ──►   .build/<name>.json
(intent: steps,            (compiles               (runnable; you
 natural-language           conditions,             never edit this)
 conditions, variables)     variables, etc.)
```

Two ways to create the source file:

- **Write it by hand** (advanced users), then `workflow build`.
- **Describe it in natural language** and let `workflow generate` produce the
  source `workflow.json` for you, then `workflow build`:

  ```
  description (.md/.txt)  ──►  workflow generate  ──►  your workflow.json
                               (one AI call;            (intent-only source;
                                intent only)             review, then build)
  ```

  The generated file is a draft you can review and tweak — it's authored at the
  same intent level, so the normal `workflow build` takes it from there.

**Golden rule:** you author *intent*. You never hand-write the compiled
mechanics (rule-expressions, value types, segments). Build generates those, and
re-generates them every build — so don't edit the `.build/` output.

---

## 2. Shape of a workflow

```jsonc
{
  "name": "order_fulfillment",
  "description": "What this workflow does and when to use it.",
  "flow": {
    "start": "first_step",            // which step runs first
    "variables": [ ... ],             // the facts the workflow tracks
    "result": { ... },                // (optional) the final answer shape
    "steps": { "first_step": { ... }, ... }
  }
}
```

- **`name`** — a short identifier.
- **`description`** — what the workflow does and when to trigger it. This is
  read by the assistant to decide when to run the workflow, so make it clear.
- **`flow.start`** — the id of the step that runs first.

---

## 3. Variables — the facts a workflow tracks

A variable is a named fact the workflow remembers and branches on.

```jsonc
{
  "variableName": "customer_tier",
  "description": "The customer's tier: one of gold, silver, bronze.",
  "dataType": "string"               // optional — build can infer it
}
```

- **`variableName`** — a short name (snake_case).
- **`description`** — explain the fact in plain language. This is the most
  important field: it tells the assistant (and build) what the value means.
- **`dataType`** *(optional)* — `string`, `number`, or `boolean`. If you omit
  it, build infers it.

### Where a variable's value comes from

You usually don't say — the assistant fills it while performing a step. But a
variable can declare that it's computed **deterministically** (no AI), via a
`resolver`. Use this for facts that are a plain lookup:

```jsonc
{
  "variableName": "is_blocked",
  "description": "true if the customer id is on the blocklist file.",
  "resolver": {
    "kind": "file_membership",
    "file": "data/blocklist.txt",
    "value_source": { "file": "data/order.json", "path": "customer_id" }
  }
}
```

`resolver.kind` options (all read files/values, no AI):

| kind | what it does |
|---|---|
| `jsonpath` | pull a value out of a JSON file (e.g. `customer_id`) |
| `enum_check` | is a value one of an allowed set? (e.g. region ∈ US/EU/APAC) |
| `file_membership` | does a value appear as a line in a file? (e.g. a blocklist) |
| `range` | is a number within min/max? |

A variable **without** a resolver is filled by the assistant when it performs
the step — it can hold **any** value (see §6, free vs. fixed values).

---

## 4. Steps — the building blocks

`flow.steps` is a map of `stepId → step`. Every step has a `type` and, usually,
a `settings.action` describing what to do in plain language.

```jsonc
"lookup_order": {
  "type": "agentAction",
  "settings": { "action": "Look up the order by its id and note its status." },
  "conditions": [
    { "condition": "the order was delivered", "next": "send_survey" }
  ],
  "next": "escalate"                 // default if no condition matches
}
```

- **`settings.action`** — a plain-language instruction. Write it the way you'd
  tell a capable assistant.
- **`conditions`** — natural-language branch rules (see §5). **You write these
  in plain language**; build compiles them.
- **`next`** — the step to go to next (the default when no condition matches, or
  the only successor for a non-branching step).

### Step types

| type | use it for |
|---|---|
| **`agentAction`** | The assistant does something — call a tool, read a file, look something up. The default step type. |
| **`question`** | Ask the user something and wait for their reply before continuing. |
| **`systemAction`** | Bookkeeping the engine does itself — no AI call, no pause. For recording a note or an automatic decision. |
| **`listDecision`** | Process a **list** of items and decide each one (see §7). For "for every order line / every applicant / every row, decide X." |

You normally only need `agentAction`, `question`, and — when you have a list —
`listDecision`.

---

## 5. Conditions — branching in plain language

To branch, add `conditions` to a step. Each is a plain-language rule plus where
to go if it's true:

```jsonc
"conditions": [
  { "condition": "the total is over 5000", "next": "manager_review" },
  { "condition": "the item is out of stock", "next": "backorder" }
]
```

- Conditions are checked **in order**; the first true one wins.
- If none match, the step's `next` is used.
- **You write the natural language.** `build` turns each condition into a precise
  rule it can evaluate. You never write the compiled rule yourself.

---

## 6. Free values vs. fixed values

A common question: *must a variable have pre-defined values?*

**No. Variables are open by default.** A variable like `customer_name`,
`complaint_reason`, or `refund_amount` holds whatever the assistant observes —
there's no fixed list. `dataType` only sets the *type* (text / number /
true-false), not the allowed values.

You only get a **fixed set of values** when you deliberately ask for one:

- an `enum_check` resolver (the value becomes e.g. `valid` / `invalid`), or
- a condition that compares against specific words (e.g. *"status is shipped"*).

So: free-text and open numeric facts are fully supported. Use a fixed set only
when the branch logic needs one.

---

## 7. listDecision — deciding many items at once

When a workflow must decide an outcome for **every item in a list** (every order
line, every applicant), use a `listDecision` step. The engine handles each item
the same way and collects the results.

```jsonc
"price_items": {
  "type": "listDecision",
  "itemSource": { "file": "data/order.json", "path": "items" },
  "settings": { "action": "For each item, look up its price and stock." },
  "itemVariables": [
    { "variableName": "in_stock",   "description": "is the item in stock" },
    { "variableName": "line_total", "description": "the item's total price" }
  ],
  "conditions": [
    { "condition": "the item is not in stock", "next": "backorder" },
    { "condition": "the line total is over 5000", "next": "review" }
  ],
  "next": "fulfill",                 // default outcome per item
  "collectInto": "decisions"         // where the per-item results go
}
```

What each part means:

- **`itemSource`** — where the list of items comes from.
- **`itemVariables`** — the facts gathered **per item** (same idea as §3
  variables, but one set per item).
- **`conditions` / `next`** — the decision rule, applied to **each** item. The
  matched `next` (or the default `next`) becomes that item's outcome.
- **`collectInto`** — the variable that holds the list of decided items.

### Optional: let the engine gather the facts too

If each item's facts come from running a script (e.g. a pricing tool), you can
have the **engine** run it per item — fully deterministic, no AI, fastest:

```jsonc
"itemFacts": {
  "kind": "exec",
  "command": ["python3", "bin/price.py", "{sku}", "{qty}"],
  "parse": "json"
}
```

The engine runs the command for each item, reads its output, and fills the item
variables itself. Use this when the per-item facts are a plain
script/tool call; omit it to have the assistant gather them.

---

## 8. result — the final answer (optional)

By default a workflow ends with a short summary. To return a **structured
answer**, declare `flow.result`:

```jsonc
"result": {
  "kind": "template",
  "value": { "customer": "{customer_id}", "decisions": "{decisions}" }
}
```

- **`kind`** — `template` (fill a shape from variables), `slots` (just list some
  variables), or `from_file` (return a file the workflow wrote).
- **`value`** — the answer shape; `{variable}` placeholders are filled from the
  workflow's variables.

The engine builds this answer itself, so the assistant doesn't have to repeat it.

---

## 9. What you write vs. what build generates

Quick reference — keep your source at the **"what you write"** level:

| You write (intent) | build generates / defaults (don't edit) |
|---|---|
| `name`, `description`, `flow.start` | the `.build/` artifact |
| step `type`, `settings.action` | branch rule-expressions (`choices`) from your `conditions` |
| `conditions` (plain language) | variable `dataType` (if omitted) |
| `next` | execution `segments` |
| variable `variableName` + `description` | the `deterministic` skip flag (when a step's branch facts all resolve) |
| `resolver` / `itemSource` / `itemFacts` (intent) | listDecision `decisionKey` / `collectInto` / `emit` defaults |
| `flow.result` shape *(optional — see below)* | `flow.result` default (when a listDecision collects a list) |

You **don't** need to write: `choices` / `expressionList`, `dataType`,
`deterministic`, a listDecision's `decisionKey` / `collectInto` / `emit`, or
`flow.result` — build fills sensible defaults. Provide them only to override.

A few fields are **intent you must write** because build can't guess them
safely: a variable's `resolver`, a listDecision's `itemSource` + `itemFacts`
(including its `derive` mapping), and `nullOn` (which decisions blank out a
field, e.g. a rejected item has no total).

If you find yourself writing rule-expressions or type annotations by hand,
that's a sign it should come from `build` instead.

---

## 10. Generating & building

### Generate a draft from a description (optional)

```bash
workflow generate --from <description.md> --name <workflow_name>
```

Reads a plain-text/Markdown description of the process and writes an intent-only
source `workflow.json` you can review. It **won't overwrite** an existing file —
pick a distinct `--name` so a generated draft never clobbers a hand-written
workflow. Add `--build` to compile it in the same step.

Add **`--validate-loop N`** to have the generator check its own draft and repair
it: each round it flags problems (a mis-pointed item-list path, an item-facts
shape mistake, a missing description, a step that pauses to ask the user for data
that's in a file, a generic outcome label) and asks the model to fix them, up to
N rounds. Worth it for generated workflows — a stronger model plus a validate
loop produces far more reliable drafts than either alone.

### Build

```bash
workflow build --name <workflow_name>
```

Reads your source, compiles it, and writes the runnable workflow. Re-run it
whenever you change the source. The `--no-optimize` flag skips the automatic
tidy-up pass (terser action text, merged screens) if you want the build to stay
literal.

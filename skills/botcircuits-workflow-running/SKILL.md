---
name: botcircuits-workflow-running
description: Run a BotCircuits workflow by name as a deterministic state machine. Use whenever the user asks to run, start, execute, or kick off a named workflow or process (e.g. "run order fulfillment", "start the loan workflow", "process this order").
---

# Running a BotCircuits Workflow

Your job is small: recognize a **run** request, find the workflow name, **start
the run**, and relay the outcome. The deterministic workflow engine does
everything else — you do not drive, loop, or perform any step.

## Start the run

Resolve the workflow name from the request (slug form, e.g. "order fulfillment"
→ `order_fulfillment`), then start it:

```
botcircuits workflow run --name <name> [--initial-args '{"order_id": "1024"}']
```

Pass any values the user already gave you in `--initial-args` as a JSON object.

The command runs the whole workflow and prints ONE JSON outcome. Parse it:

- `{"status": "success", "message": "<summary>"}` — relay the summary to the
  user. Done.
- `{"status": "failure", "message": "<reason>"}` — relay the reason. **Do not
  retry** — let the user decide what to do next.
- `{"status": "paused", "question": "<ask user>"}` — the workflow needs input
  only the user can give. Ask them the question, then resume:

  ```
  botcircuits workflow run --name <name> --reply "<their answer>"
  ```

  Repeat until the outcome is `success` or `failure`.

If the workflow isn't built yet (`.botcircuits/workflows/.build/<name>.json` is
missing), build it first with `botcircuits workflow build --name <name>` (or the
**botcircuits-workflow-authoring** skill), then start the run.

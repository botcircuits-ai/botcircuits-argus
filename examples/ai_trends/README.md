# AI Trends — Workflow Example

A minimal example of a **simple, linear BotCircuits workflow** that uses a
**web fetch** (Google Trends) and a **file write** — no branching, no batch
input, no mock API. It checks what's trending in AI, summarizes it, and writes
the summary to a dated results file. The whole run is **unattended**: it never
asks the user anything.

This is the simplest example in this folder — a good starting point before the
branching/batch examples (`shipment_tracking`, `lab_results_triage`,
`deployment_release_gate`, `pr_merge_gate`).

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |

There is **no mock API** for this example — it uses the real, public Google
Trends, so there is nothing to start first.

## Author the workflow

Paste the prompt in [TASK.md](TASK.md) into the
`botcircuits-workflow-authoring` skill. It generates the `ai_trends` workflow,
which:

1. queries Google Trends for trending AI topics and rising search terms into a
   `trends_findings` slot,
2. summarizes those findings into a concise `trends_report` slot, and
3. writes the report to `find_<today>.txt`
   (e.g. `find_2026-06-22.txt`, today's date in `YYYY-MM-DD`),

then ends the flow.

Build it, then run it (workflow-running skill). Because the flow is linear and
unattended, a single run reads the trends, writes the report, and finishes
without pausing.

# 11. Runtime Providers

[← Index](00-index.md)

---

A **runtime provider** is the source of "agent intelligence" the workflow
engine uses when a step needs an LLM — to perform an `action`, or to resolve a
non-deterministic slot value. The engine itself is deterministic: it owns the
state machine (branching, ordering, slot evaluation) and is run-to-run
predictable. It hands off to a runtime provider only for the genuinely
non-deterministic work, then takes control back.

This is the BotCircuits direction: **stop maintaining our own agent loop / LLM
plumbing and let an existing host agent supply the intelligence.** The project
ships two skills a host agent loads:

- **botcircuits-workflow-authoring** — turn a natural-language description into a
  built workflow JSON (pure generation + validation; no runtime needed).
- **botcircuits-workflow-running** — drive a built workflow through the engine,
  dispatching each action/slot decision to the host runtime.

## The providers

| Provider | How it supplies intelligence |
|---|---|
| `self` (inline) | The host agent that loaded the skill performs each segment **in its current session**, one step at a time, via the `step_workflow` driver. No subprocess, no second model. This is what powers `claude > "run order fulfillment"`. |
| `claude-code` | Shells out to a host `claude` CLI, headlessly, **one process per segment**, reading a JSON object on stdout. For cross-agent / non-self-hosted cases. No SDK binding. |
| `codex`, `openclaw`, … | Same CLI model via config; added behind the same interface. |
| `native` | The in-process BotCircuits agent loop (`agent.core.Agent`). The original path; kept as the default fallback and for CI. |

Every provider satisfies one interface — `run_segment` (perform a segment's
actions, report observed slots / per-item facts / a pause) and `resolve_slots`
(backfill empty branch variables). The engine can't tell them apart.

## Why CLI, not SDK

Some host agents have no SDK. Command-line execution is the lowest common
denominator: build an argv, run it, read JSON back. The contract the host
follows is simply "print this JSON shape as your final output." This stage
targets Linux/POSIX command interfaces; the OS-specific bits live in one place
(`runtime/cli_exec.py`) so other platforms drop in later.

## Isolation and state

Each action segment runs the host CLI in a **fresh temporary working
directory** — an isolated session context, torn down after. Continuity within
a segment is the CLI call; continuity *across* segments is the engine's slot
state, by design (cache-stable, deterministic). When a step needs the user, the
provider returns `{"paused": true, "question": …}`; the runner persists a resume
cursor and continues on the next invocation.

## Inline / self mode (the common case)

When the host agent *is* the runtime — Claude running a Claude-hosted workflow —
there's no point spawning a CLI: the agent is already in an active session. The
**self** provider hands each segment back to that agent one step at a time. The
mechanism reuses the engine's own pause/resume: `run_segment` returns a paused
result carrying the action, the engine yields its resume cursor, and the
`step_workflow` driver surfaces the action to the host. The host performs it,
reports observed values, and the next call seeds them so the engine advances.
This is what the **botcircuits-workflow-running** skill drives; the host never spawns a
second model.

## Selecting the runtime

Resolution order (first hit wins): explicit config (`$BOTCIRCUITS_RUNTIME`, or
`runtime` in `.botcircuits/settings.json`) → env markers the host sets
(`CLAUDECODE`, `CODEX_*`, …) → a `which` probe for a known CLI binary →
`native`. The `self` runtime is selected explicitly (the botcircuits-workflow-running skill
drives it), never auto-probed. A per-provider argv template lives in config, so
adding a CLI that emits the same JSON contract is configuration, not code.

See the Implementation Guide's runtime-providers page for the engine seam and
how to add a provider.

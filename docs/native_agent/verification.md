# Verification (`agent/verification.py`)

Until now the agent answered and the harness trusted the answer.
Verification closes that gap: **no receipt, no acceptance**.

## The enforced-run gate (wired into the loop)

```
 turn produced a terminal reply
        │
        ▼
 changed code this turn? ────────────── no ──► accept the reply
   (write/edit of .py/.ts/…)
        │ yes
        ▼
 AGENTS.md declares "## Testing" cmd? ── no ──► accept the reply
        │ yes
        ▼
 transcript shows shell_exec <cmd>
 paired to exit_code == 0?  ─────────── yes ─► accept the reply
        │ no
        ▼
 attempts < verify_attempts? ────────── no ──► accept last reply
        │ yes                                  (failure stays visible)
        ▼
 inject: "Run `<cmd>` now — it must exit 0"
        │
        └───────► back into the loop (the model runs it for real)
```

The *model* runs the test, with the `shell_exec` tool it already has;
the harness's job is to *enforce*. When a turn changed code and the
project declares a test command, the loop will not accept "done" until
it has OBSERVED, in this turn's tool transcript, a real passing run:

- **Trigger** — a `write_file` / `edit_file` call whose path has a code
  extension (`.py`, `.ts`, `.go`, …). Prose files don't arm the gate.
- **Oracle** — the project's declared test command: the first line of
  the first fenced block under a `## Testing` heading in `AGENTS.md`
  (read from `Agent(agents_dir=...)`, default cwd). No AGENTS.md or no
  heading → no gate.
- **Receipt** — a `shell_exec` call whose argv contains the command,
  paired **by `tool_call_id`** to a result with `exit_code == 0`. A
  failed run, or a narrated "it works", never counts.
- **Feedback** — on a missing/failed run the harness appends
  *"Run `<command>` with the shell_exec tool now — it must exit 0"* and
  loops, capped at `verify_attempts` (default 3). Exhausted attempts
  return the last reply so the turn still ends; the failure stays
  visible in the transcript. `require_run=False` opts out.

The gate is identical on both `chat` and `chat_stream`. A standing
policy in the CLI system prompt ("never claim it works on your word
alone") makes the model *try* to verify; the gate is what makes trying
insufficient.

## The standalone oracle

`run_python(code, check)` runs candidate code plus an independent
assertion in a fresh process with a scrubbed environment and a scoped
temp workdir — model-written code never sees our credentials. Success is
signalled by a per-run random nonce printed only after the check
completes, so an early `exit(0)` or a printed guess can't forge a pass.
`extract_code(text)` pulls the fenced block out of a model reply.

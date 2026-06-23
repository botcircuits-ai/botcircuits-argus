# Runtime Providers

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

The workflow engine (`agent/workflow/engine/runner.py::run_workflow_engine`) is
provider-agnostic. It drives the deterministic state machine and calls back out
for exactly two capabilities, never touching an LLM directly:

1. `run_segment` — perform one branch-delimited segment's action(s) and report
   the branch slots / per-item facts observed (or pause for the user).
2. `resolve_unfilled` — backfill branch variables a segment left empty (Tier-0
   deterministic, then Tier-2 semantic extraction).

An **`AgentRuntimeProvider`** (`runtime/base.py`) packages those two behind one
interface so the engine can't tell providers apart. `run_segment` returns the
engine's existing `SegmentResult`; `resolve_slots` matches the
`resolve_unfilled` hook.

## Package layout

```
src/botcircuits/runtime/
├── __init__.py          # select_runtime(), AgentRuntimeProvider re-export
├── base.py              # AgentRuntimeProvider ABC + RuntimeConfig
├── detect.py            # config-first → env markers → which-probe → native
├── cli_exec.py          # OS-isolated subprocess runner (argv list, temp cwd)
├── result.py            # tolerant JSON-stdout → SegmentResult / slot dict
├── run_workflow.py      # CLI entry: drive a workflow via a CLI provider
├── step_workflow.py     # CLI entry: drive a workflow inline, one step at a time
└── providers/
    ├── native.py        # wraps Agent._run_segment + _make_resolve_unfilled
    ├── inline.py        # self/inline — host agent performs each segment in-session
    └── claude_code.py   # `claude -p … --output-format json` headless one-shot
```

## The native path is behavior-preserving

`NativeRuntime` forwards `run_segment` to the live `Agent._run_segment` and
`resolve_slots` to the closure from `agent.workflow._make_resolve_unfilled` —
the exact methods the engine used to receive directly. Wiring the engine through
the provider produces identical results; this is what keeps the refactor
zero-regression. The workflow tool / `register_workflows` take an optional
`runtime=` (defaulting to None = native in-process), so existing callers are
untouched.

## The CLI path

`ClaudeCodeRuntime.run_segment` reuses the engine's cache-stable
`ENGINE_SYSTEM_PROMPT` + `build_segment_user_message`, swapping the trailing
"call record_slots" instruction for an OUTPUT CONTRACT: print one JSON object —
`{"slots": …, "items": …, "paused": …, "question": …, "text": …}`. It runs the
host CLI once per segment via `cli_exec.run_cli` in a fresh temp dir, then
`result.segment_result_from_stdout` parses stdout (tolerant of CLI envelopes,
fences, and prose).

`resolve_slots` keeps Tier-0 deterministic in-process (`slot_resolver`, zero
tokens, OS-independent) and crosses the CLI boundary only for Tier-2, reusing
`variable_normalizer`'s prompt body + the same hallucination guard.

## Adding a provider

1. If it emits the same JSON contract, add a `_RuntimeSpec` in `detect.py`
   (env markers, binary, argv template) — `select_runtime` already routes it to
   `ClaudeCodeRuntime`. No new code.
2. If its stdout shape differs, add an adapter in `result.py` and a small
   subclass in `providers/`.

## The inline / self path

`InlineRuntime` (`providers/inline.py`) is how the host agent runs a workflow
in its own session. It performs no action itself: `run_segment` returns a
*paused* `SegmentResult` whose `question` is an encoded action marker
(`encode_action`/`decode_action`), reusing the engine's pause machinery so the
resume cursor + slot persistence are the engine's existing, tested code. The
`step_workflow` driver catches the pause, persists state, and prints the action
+ the `report` schema for the host; the next invocation seeds the host's
observed values (`seed_result`) so the engine consumes that segment and advances
to the next hand-off. `resolve_slots` is deterministic Tier-0 only — anything it
can't fill surfaces to the host as the engine's own clarification question. No
subprocess is ever spawned.

## Running

Inline (host agent performs each step in-session — the botcircuits-workflow-running skill):

```bash
python -m botcircuits.runtime.step_workflow --name <wf> --restart [--initial-args '{…}']
python -m botcircuits.runtime.step_workflow --name <wf> --observed '{"slots":{…}}'
python -m botcircuits.runtime.step_workflow --name <wf> --reply "<answer>"
```

Cross-agent / headless (one CLI subprocess per segment):

```bash
python -m botcircuits.runtime.run_workflow --name <wf> \
    [--initial-args '{"k":"v"}'] [--runtime claude-code] [--reply "<answer>"]
```

Both persist a cursor to `.botcircuits/workflows/.runs/<name>.json` (gitignored)
so a question/action and its answer can span separate process invocations. The
native runtime has no standalone runner — run it through `botcircuits` instead.

## Out of scope for CLI providers (native-only)

The gateway (`gateway/`), persistent memory (`agent/memory.py`), and hosted MCP
stay with the native provider. CLI providers rely on the host agent's own tools
/ MCP / memory.

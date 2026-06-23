# Local Tools & Workflows

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 8. Local Tools — Package Layout & Per-Tool Config

[agent/tools/](../../src/botcircuits/agent/tools/). Each built-in tool lives in its own file under `builtins/` and exposes two things:

```python
def shell_exec_tool(*, auto=False, ...) -> LocalTool:
    """Factory — captures policy in a closure so the model can't override it."""

def register(reg: ToolRegistry, **config) -> None:
    """Threaded by `default_registry()` from `tools.<name>` in JSON.
    Validates keys, then calls the factory."""
```

`default_registry(tools_config)` walks a single `_BUILTINS` dispatch table and calls each `register(reg, **overrides)`. Per-tool config in JSON looks like:

```json
{
  "tools": {
    "shell_exec": { "timeout_seconds": 60, "auto": false },
    "now": null
  }
}
```

Three behaviors:
- **dict** → register the tool with those overrides on top of the factory's defaults.
- **`null`** or **`false`** → skip registration entirely (disable the tool).
- **omitted** → register with the factory's defaults.

Validation happens at startup in [agent/tools/__init__.py](../../src/botcircuits/agent/tools/__init__.py) (unknown tool names → reject) and in each tool's own `register()` (unknown override keys → reject). The CLI exits 2 with a clear message before any provider is built.

### 8.1 Why this shape
The earlier "kitchen-sink" `tools.py` couldn't accommodate more than three or four tools without becoming unreadable. One file per tool: trivial to add, trivial to delete, trivial to ship a tool optionally without dragging it into the default surface. The `register(reg, **config)` contract gives every tool a uniform override surface so the JSON schema doesn't grow when you add a tool — it grows by one key.

### 8.2 The `shell_exec` tool
[agent/tools/builtins/shell.py](../../src/botcircuits/agent/tools/builtins/shell.py). Runs a system command via `asyncio.create_subprocess_exec` — **no shell**, so pipes, redirects, globs, and metacharacters are literal. Default config:

| Param | Default |
|---|---|
| `timeout_seconds` | 30 |
| `max_output_bytes` | 10 KB |
| `auto` | `False` |

There is intentionally **no command allow-list**. The gate is a human y/N confirmation prompt that runs before every command. The model proposes argv; the tool prints it on stderr, reads a y/N response from stdin, and either runs the command or returns `{"denied": true, ...}` with a hint to the model not to retry the same argv.

**Auto mode.** `auto=True` skips the prompt. A warning banner still prints on stderr before each command so the user sees what ran. Set via `--auto` on the CLI or `tools.shell_exec.auto: true` in JSON. The CLI flag is fanned out by `cli/app.py:_AUTO_GATED_TOOLS` into the `auto` key of every gated tool (`shell_exec`, `shell_stop`, `write_file`, `edit_file`, `plan_and_confirm`, `build_workflow`), merged *after* `resolve()` so it overrides the JSON value when both are set. `--auto` on a tool disabled with `"<tool>": null` is intentionally a no-op — disabling and automating are contradictory states; we don't resurrect a tool the user explicitly turned off.

**Effective-auto for non-tty.** The constructor computes `effective_auto = auto or not sys.stdin.isatty()`. The FastAPI gateway and piped CLI invocations have no human to answer the prompt, so they automatically engage auto mode regardless of config — otherwise every tool call would deadlock waiting for stdin that never arrives. This is evaluated once at tool construction; tools live for the life of the process, so a one-shot check is correct.

**Confirmation prompt UX.** The prompt writes to **stderr**, not stdout, so it doesn't get mixed into the streamed assistant text on stdout. Color is auto-disabled when stderr isn't a TTY or `NO_COLOR` is set. The reader uses `loop.run_in_executor(None, input, "")` so it doesn't block the event loop — provider streaming continues, MCP heartbeats keep firing, the agent loop stays responsive.

**Why no allow-list.** A read-only allow-list conflates "what's safe by default" with "what's useful by default" — the obvious read-only set blocks `git`, `python`, `pip`, anything that mutates, so any real agentic task needs to override it. The model then hits rejections, retries with a different argv, and burns tokens. A y/N gate is more honest: the human decides per-call, the model sees real denials with explicit reasoning, and there's no policy buried in a constant somewhere. For unattended runs, `auto=True` is one keystroke.

**Why no cwd pinning.** Pinning a working directory looks like sandboxing but isn't — absolute paths in argv bypass it entirely (`['cat', '/etc/passwd']` doesn't care where the process started). The user is already gating every call via y/N, so the cwd guardrail adds friction without adding safety. The subprocess inherits the agent process's cwd; the user controls where the agent is launched.

The tool description string (sent to the model) **announces the confirmation gate and the limits** so the model adapts its behavior — it knows to expect a denied result and what argv it can use.

The tool is in `default_registry()` because the confirmation gate makes the default surface safe by construction.

### 8.3 Adding a new built-in tool
Three steps:
1. Create `agent/tools/builtins/<name>.py` with a factory and a `register(reg, **config)`.
2. Add one entry to `_BUILTINS` in [agent/tools/__init__.py](../../src/botcircuits/agent/tools/__init__.py).
3. (Optional) Document the config keys in the README.

That's it. Users can configure or disable it via `tools.<name>` in JSON without any further plumbing.

If the new tool needs y/N gating, import `from . import _confirm` and call `_confirm.effective_auto(auto)` once at construction, then `_confirm.confirm(title, lines)` / `_confirm.warn(title, lines)` per call. Register `auto` as one of the tool's allowed config keys, then add the tool name to `_AUTO_GATED_TOOLS` in [cli/app.py](../../src/botcircuits/cli/app.py) so `--auto` covers it.

**Lazy-registered builtins.** Some tools are heavy enough that we don't want them on the model's tool list for normal chat — `build_workflow`, today — so [agent/tools/__init__.py](../../src/botcircuits/agent/tools/__init__.py) maintains a `_LAZY_BUILTINS = ("build_workflow",)` set. `default_registry()` skips lazy builtins unless the user explicitly opted in via `tools.<name>` in JSON. The CLI's slash dispatcher loads them on demand via `register_builtin(reg, name, *, provider, config)`, which is a no-op if the tool is already on the registry. The wiring lives in `LAZY_TOOL_TRIGGERS` in [cli/commands.py](../../src/botcircuits/cli/commands.py) — one entry maps `/workflow` to the `build_workflow` tool name and the slash handler calls `register_builtin(...)` before forwarding the rest of the line as a chat message. Adding a new lazy trigger is one line in each map.

**Provider-aware builtins.** Tools that need the agent's `LLMProvider` at register time (e.g. `build_workflow` runs the workflow indexer against the same model the agent chats with) are listed in `_PROVIDER_AWARE_TOOLS`. `default_registry(tools_config, *, provider=None)` injects `provider=` into those tools' `register()` kwargs automatically; CLI and gateway both pass the constructed provider through. Library callers who omit `provider` get a working tool with whatever provider-dependent step is skipped — `build_workflow` writes the raw source but doesn't index.

### 8.4 The code-gen tool surface

`shell_exec` alone is enough to drive a build, but it forces the model to invent argv for things the runtime can do natively (read a file, replace a substring, walk a tree for matches). The other code-gen builtins are dedicated tools for those operations so the model gets structured input/output and the user gets a uniform gating UX.

| Tool | Why dedicated (vs. shell_exec) |
|---|---|
| `read_file` | Returns `{start_line, end_line, total_lines, truncated, content}` — the model knows what it has without parsing `wc -l` output. `offset`/`limit` for large files. |
| `write_file` | Y/N prompt shows path + byte count + content preview before the write. Parent dirs auto-created. |
| `edit_file` | Y/N prompt shows a **unified diff**, not the new content alone. Enforces unique-match contract for `old_string` (or `replace_all=true`). Mirrors Claude Code's Edit semantics. |
| `list_dir` | Returns typed entries (`file`/`dir`/`symlink`) with sizes — easier than parsing `ls -la` output. |
| `glob_search` | Pure-Python `glob.glob(recursive=True)`. Sorts by mtime (newest first), caps results, and skips common ignore dirs (.git, node_modules, __pycache__, .venv, …). |
| `grep_search` | Pure-Python regex walk. Skips binary files and the same ignore dirs. Optional filename `include` glob. Bounded by `max_results` + `max_file_bytes`. |
| `todo_write` | Replace-semantics list (model passes the whole list each call). Module-global `_STORE` so `plan_and_confirm` can seed it. Renders to stderr with colored glyphs. |
| `plan_and_confirm` | Y/N prompt shows the plan + initial TODO list before any work starts. Seeds `_STORE` on approval. The default system prompt instructs the model to call this once per non-trivial software task. |
| `build_workflow` | Y/N prompt shows the workflow summary + ordered step preview (branches inline as `↳ if <NL> → <next>`). Validates against the supported step types, writes the raw source under `.botcircuits/workflows/`, then runs the condition indexer and emits a runnable copy to `.botcircuits/workflows/.build/`. Lazy-registered (loaded on `/workflow add|edit` only). See §8.6.10. |
| `memory` | Mutates persistent agent + user notes that live across sessions. Three actions: `add` (append), `replace` (substring-match swap), `remove` (substring-match drop). Two targets: `memory` (agent's notes; 2200-char cap) and `user` (user profile; 1375-char cap). Content is auto-loaded into the system prompt at session start — no `read` action because the data is already in context. See §8a. |
| `human_feedback` | Asks the user a question (`question` arg) and **pauses the agent loop**. The handler just echoes `{paused: true, question}`; the loop ([agent/core.py](../../src/botcircuits/agent/core.py)) recognizes the call, surfaces the question as the turn's reply, and hands control back to the user. Used by `question`-type workflow steps (forced) and any time the model judges it needs the user's input. Not gated (no side effect to confirm). See §8.6.11. |

**The planning gate is a normal LocalTool, not a hardcoded loop step.** Putting it in the tool registry has three benefits: (1) the gate is opt-in via the JSON config (disable with `"plan_and_confirm": null` if you don't want it), (2) the model chooses *when* to invoke it based on the task — pure-question requests skip it, (3) `--auto` and the y/N prompt reuse the same `_confirm` helper as every other gated tool, so the UX stays consistent.

**Why no command allow-list / path sandbox for these tools.** Same logic as `shell_exec` (§8.2): an allow-list conflates "safe by default" with "useful by default." A `write_file` restricted to `./src/**` blocks the model from creating tests, configs, or sibling-directory artifacts. The y/N gate puts the human in the loop per call; `--auto` is the explicit escape hatch for trusted/unattended runs. Path traversal isn't a meaningful concern when the user sees the path in the prompt before approving.

**Why module-global `_STORE` in `todo_write`.** The store lives in the tool module, not on the Agent, because it's intrinsically tied to the *tool's* lifetime within a single process. Putting it on `Agent` would couple two unrelated concerns and force every consumer (gateway included) to thread a reference. The cost: two processes can't share a TODO list — fine, because each `botcircuits-cli` run is its own session.

**Default system prompt.** [cli/system_prompt.py](../../src/botcircuits/cli/system_prompt.py) holds `DEFAULT_SYSTEM_PROMPT`. The CLI applies it inside `load_cli_config` when neither `--system` nor JSON `system` is set. The gateway stores it on `app.state.default_system` and the routes fall back to it when `req.system` is absent. The prompt teaches the model the planning workflow: ask focused follow-up questions when ambiguous, call `plan_and_confirm` for non-trivial software tasks, keep `todo_write` fresh, verify with tests, and use `background: true` for non-terminating commands. It also documents the **persistent memory** contract (see §8a) — when MEMORY.md / USER.md are loaded they appear under `<agent_memory>` / `<user_profile>` tags, the `memory` tool is how the agent updates them, and edits take effect on the NEXT session (the snapshot is frozen at session start to keep the prompt cache warm). Users override the prompt entirely with `--system "..."` (empty string disables the default).

### 8.5 Background shell processes

`shell_exec(background: true)` exists because foreground-only execution is hostile to anything that doesn't terminate: dev servers, file watchers, `tail -f`, `uvicorn --reload`, `npm run dev`. With a finite `timeout_seconds`, those commands all hit the timeout and get killed — the model sees a useless error.

The model surface is three tools: `shell_exec` (with `background: true`), `shell_status`, `shell_stop`. The state lives in [agent/tools/builtins/_bg.py](../../src/botcircuits/agent/tools/builtins/_bg.py) as a module-global `_REGISTRY` dict keyed by short uuids. Same module-global pattern as `todo_write._STORE`: the registry is intrinsically tied to its tools' lifetime in one process, and putting it on `Agent` would force every consumer to thread a reference.

**Tail buffers as bounded `collections.deque`.** Each background process spawns two reader tasks (`_pump`) that read lines from the pipe into `deque(maxlen=MAX_LINES_PER_STREAM)`. Bounded so a runaway producer can't OOM the agent; per-line truncated at `LINE_BYTES` so a process that never emits newlines can't either. `shell_status` returns `tail(buf, lines)` slices — the model gets the most recent context without ever seeing the full history.

**Why deques, not strings.** Concatenating strings is O(n²) for a chatty producer; deque appends are O(1). The model wants the *recent* tail anyway, not the start.

**Why pipe-readers as background tasks, not on-demand reads.** If we lazily read in `shell_status`, the pipe buffer (~64KB on macOS) fills and the producer blocks. The model would see a "frozen" process that's actually waiting for someone to drain its stdout. Pumping continuously keeps the producer unblocked even if the model never polls.

**Cleanup has two layers.** `Agent.aclose()` awaits `_bg.terminate_all()` — graceful SIGTERM-then-SIGKILL while the event loop is still alive. As a backstop, the first call to `_bg.register` installs an `atexit` hook that falls back to synchronous `os.kill` (the loop is gone by then, so we can't `await` cleanly). This catches the case where someone forgets `async with Agent(...)` and the process exits anyway. Orphaned `npm run dev` after a CLI exits is unfriendly enough to justify the two-layer approach.

**Why `shell_stop` is gated but `shell_status` isn't.** `shell_status` is pure observation — never modifies state, can't fail in a way that needs human approval. `shell_stop` kills a process; that's a real side effect of the same magnitude as starting one, so it goes through `_confirm.confirm` with the same `auto` semantics. `shell_stop` is also in `_AUTO_GATED_TOOLS`, so `--auto` covers it.

**`terminated_exit_code`, not `exit_code`.** The registry's is-error heuristic (§5.x) flags any tool result with non-zero `exit_code` as an error. But a SIGTERM-killed process has `returncode = -15`, and that's the *successful* outcome of `shell_stop` — not a failure observation. Returning the key under a different name keeps the heuristic accurate while preserving the information.

### 8.6 BotCircuits workflows as tools

> **Architecture note (engine-driven execution).** Workflow execution was inverted: once a workflow tool fires, the **engine owns the loop** and calls the LLM per branch-delimited *segment* with a constant-size, cache-stable prompt — instead of the LLM driving and re-calling the workflow tool to advance one step at a time. The new model is documented in **§8.6.13**; it is the current behavior. Sections **§8.6.2–§8.6.12** below describe the original LLM-driven mechanism (the `pendingBranch` re-entry dance, the empty-args auto-recall, the per-step re-call reminders). That mechanism still exists as the **legacy `run_workflow` fallback** (used when the agent loop doesn't supply a `run_segment` callback), and most of its sub-parts — the engine `executor`, the slot resolver, Layers A/B, the `condition_processor` indexer, `build_workflow` — are **reused unchanged** by the engine path. Read §8.6.13 first for how it all fits together today; treat the auto-recall / re-call-reminder passages in §8.6.6, §8.6.8, §8.6.11, §8.6.12 as historical.

[agent/workflow/](../../src/botcircuits/agent/workflow/). Workflows are loaded from a local directory and each one is exposed as a `LocalTool` on the same `ToolRegistry` that holds the built-ins. From the model's perspective a workflow looks identical to any other tool — same name/description/schema surface — so no provider needs to know workflows exist.

Public functions in [agent/workflow/__init__.py](../../src/botcircuits/agent/workflow/__init__.py):

| Function | Purpose |
|---|---|
| `fetch_workflows()` | Return the workflow records discovered on disk. |
| `run_workflow(workflow_name, args, *, session_id, provider, last_assistant_message, last_user_message, normalize_enabled)` | Execute one step of a workflow and return its result. Threads `session_id` through so subsequent calls re-enter the same workflow conversation. `provider` + the message snapshots feed Layer B normalization (see §8.6.4). |
| `workflow_tool(record, *, provider, normalize_enabled)` | Wrap a single workflow record as a `LocalTool` with closure state for multi-turn execution. The handler accepts an optional `context` dict from the agent loop; when that context carries a `run_segment` callback it drives the **engine path** (§8.6.13), otherwise it falls back to the legacy per-step `run_workflow`. |
| `register_workflows(reg, *, provider, normalize_enabled)` | Discover workflows + wrap each as a `LocalTool` and register on `reg`. Returns `(registered_names, skipped_names)`. |
| `active_workflow_names(reg)` | List the names of workflow tools on `reg` that currently hold a live `session_id` (engine-driven: the workflow is **paused** waiting on the user). Read by the agent loop to inject a resume reminder. |

#### 8.6.1 Discovery + loader (raw source vs. `.build/` artifact)

[agent/workflow/local.py](../../src/botcircuits/agent/workflow/local.py). Workflows live in two parallel locations under `$BOTCIRCUITS_WORKFLOWS_DIR` (default `.botcircuits/workflows`):

- **Raw source** — `<workflows-dir>/<name>.json`. The human-editable file the author maintains. Carries `name`, `description`, and natural-language `conditions` on `agentAction` states. `conditions` lives at the step **root** (sibling of `type`/`next`), not nested inside `settings` — it describes control flow, not step-type-specific payload.
- **Build artifact** — `<workflows-dir>/.build/<name>.json`. The built, runnable copy with `expCondition` strings, `choices[]` arrays (also at the step root), and an aggregated `flow.variables` list. Written by the `workflow build` CLI command and the `build_workflow` tool.

`fetch_workflows()` reads only from `.build/`. A missing `.build/` directory or `.build/<name>.json` for an existing raw file is reported with a stderr warning telling the user to run `botcircuits-cli workflow build --name=<name>`; the workflow is then skipped rather than loaded un-built. Rationale: an un-built workflow has natural-language conditions the engine can't evaluate, so silently loading it would surface as cryptic "no choice matched" failures at runtime. Skipping with an actionable error message keeps the failure mode obvious.

`name` is the sole identifier — it doubles as the registered LLM-facing tool name, so it must match `^[a-zA-Z0-9_-]+$` (OpenAI's strictest tool-name regex). The loader validates this and defaults to the filename stem when the field is missing.

`_load_workflow_record(workflow_name)` re-reads the build artifact on every `run_workflow` call so on-disk edits pick up without a restart (the file in `.build/` is the source of truth at runtime; we never cache it across calls).

#### 8.6.2 The embedded STM engine

[agent/workflow/engine/](../../src/botcircuits/agent/workflow/engine/) is deliberately narrow. The whole engine is four small modules:

| File | Role |
|---|---|
| `executor.py` | `run_flow(flow, message, start_step_id, journey_id)` walks step-by-step until a step yields data (the `agentAction` pause) or the graph runs out of steps. On re-entry it also resolves `pendingBranch` (see below). |
| `state.py` | `WorkflowStateContext` tracks `currentStep`, `runningStep`, `pendingBranch`, and per-journey `slots` across re-entries. |
| `handlers/choice.py` | `evaluate_choices(choices, message, default_next)` — pure helper for expression evaluation. No longer a state type; called by the executor when resolving a `pendingBranch`. Operators: `is`/`is not`, `>`/`>=`/`<`/`<=`, `contains`/`not contains`, `starts with`/`ends with`, `is empty`/`is not empty`. Typed values from the indexer (`500` not `"500"`) compare correctly because the helper coerces string `value` against typed `variable` at comparison time. |
| `handlers/action.py` | Builds the payload `{action, conditions, choices, variables, end, nextStep, slots, ...}` the workflow tool surfaces to the LLM. The executor routes by `step.type == "agentAction"`, so this handler doesn't re-check the kind. |
| `handlers/question.py` | Same payload shape as `action.py` but tags it `kind: "question"`. The tool wrapper reads that tag and frames a directive that forces the model to call `human_feedback` (which pauses the loop). Control flow (`next`/`conditions`/`choices`) behaves identically to an agentAction. |
| `utils.py` | `fill_text_with_slots` (case-insensitive `{slot}` interpolation), `get_next_step_for_prompt_action`, `coerce_for_compare`. |

**Step kind: a single `state.type` field, four values.** Kind is set by one top-level field rather than threaded through multiple discriminators:

| `state.type` | What it does |
|---|---|
| `start` | No-op; the executor falls through to `next`. |
| `agentAction` | Calls `handle_action`; emits the action payload and pauses the workflow. If `step.conditions` or `step.choices` is present (both at the step root, sibling of `type`/`next`), the executor records `pendingBranch` on the saved session so the **next** re-entry resolves the branch instead of walking blindly to the static `next`. |
| `question` | Calls `handle_question`; identical to `agentAction` for control flow, but the emitted payload carries `kind: "question"`. The workflow tool turns that into a directive instructing the model to call `human_feedback` with the question, which pauses the loop until the user replies. Branching still works (a question can have `conditions` evaluated on re-entry once the answer fills slots). |
| `systemAction` | **Non-pausing** engine-side bookkeeping — no LLM round-trip. The executor records the slot-interpolated `settings.action` text as an audit note and keeps walking; consecutive systemActions collapse into the surrounding pauses. Branching, if any, is evaluated **immediately** against current slots (they were filled at the previous pausing step's re-entry — nothing to wait for), so no `pendingBranch` is recorded. Accumulated notes are surfaced by `run_workflow` as `system_notes` and prepended to the next directive (`render_system_notes`), keeping the audit trail in the transcript. A workflow that **ends** on a systemAction chain returns `done=True, action=None` — a "quiet finish": the tool wrapper sets `finished_quietly` on `_workflow_state`, and the agent loop (`_quiet_workflow_finish`) ends the turn without another provider call when its own auto-recall produced that result (the model's previous text already was the final answer). Use it for record-keeping steps (`mark_fulfill`, `reject_item`, terminal "done" states) that previously each cost a full provider call with the whole context attached. A `_MAX_SYSTEM_CHAIN` guard (100 steps) turns an accidental systemAction cycle into an error instead of an infinite engine loop. |

Anything else (`choice`, `message`, `prompt`, `aiTask`, `webhook`, `codehook`, `integrationWorkflow`, `journey`, `human_support`, `pause`, `custom_action`, `end_action`, `auth_action`) raises `ValueError(f"... does not support step type {step_type!r} ...")` with a hint pointing authors to the agentAction-with-conditions pattern. Silent no-ops were considered and rejected: they make broken workflows look like working ones.

**Why no separate `choice` state type.** Branching evaluates the *current* state of slot values; for an LLM-driven workflow those values exist only *after* the model has acted on the previous agentAction. The next re-entry (the loop's auto-recall — §5.4) then carries the surrounding transcript into Layer A/B normalization, which extracts the values the branch needs. Putting branching inside `agentAction` makes that timing explicit — the executor knows it needs to wait for the LLM to act before evaluating. A standalone `choice` state would either fire too early (against stale slots) or require synchronous LLM intervention to fill slots first, which is exactly what we don't want — branching stays deterministic.

**The `pendingBranch` mechanism.** When an `agentAction` with conditions pauses, the executor sets `saved_session["pendingBranch"] = {"stepId": <id>, "defaultNext": <static next>}`. On the next call, before walking from `currentStep`, `_resolve_pending_branch` reads the step's `choices`, calls `evaluate_choices(choices, message, defaultNext)`, and uses the result as the entry point — overriding `currentStep`. The marker is then cleared so it doesn't fire again. This keeps the engine purely sequential between turns (no embedded async branching), while still routing on the latest slot values.

When no `choice` matches, the engine falls through to `defaultNext` (or ends the workflow if there isn't one). There is no LLM/RAG fallback inside the engine — recovery, if any, is the agent loop's job, not the workflow's.

#### 8.6.3 Building — `workflow build --name=<wf>`

[agent/workflow/condition_processor.py](../../src/botcircuits/agent/workflow/condition_processor.py). The author writes natural-language `conditions` on an `agentAction`. The builder compiles them into a typed `choices` array and writes `flow.variables` once. Run via the CLI:

```bash
uv run botcircuits-cli workflow build --name=order_status
```

What it does:

1. Walks every `agentAction` state with a non-empty `conditions` array (dedupes per state).
2. Builds a prompt: each condition's NL text + the surrounding state graph (so the LLM picks sensible variable names by looking at neighboring action text).
3. Calls `provider.complete(...)` with a strict-JSON system prompt — **same `LLMProvider` the agent runs on**, no tools, no streaming.
4. Parses each returned expression (`<variable> <operator> <value>`) back into the `{variable, operator, value}` shape the engine reads, and aggregates `variables[]` across all states.
5. Writes the built result to `<workflows-dir>/.build/<name>.json`, leaving the raw source under `<workflows-dir>/<name>.json` untouched. Idempotent: re-running replaces (not appends to) the generated `choices` and the top-level `variables` list. The raw source is the canonical input; if the user edits the raw file and forgets to re-build, `fetch_workflows()` prints a warning and skips that workflow until the next build run.

**Operator allow-list mirrors the engine.** The prompt enumerates the same 12 operators `evaluate_choices` understands; the LLM is told to use *only* those. Anything else is dropped at parse time.

**Why the indexer lives next to the engine.** The engine is the consumer; the indexer's output must match the engine's input contract exactly (same operator names, same `expressionList` shape). Keeping them together means a change to one is one PR.

**Why same provider as the agent.** The user already picked an `LLMProvider` for chat; reusing it for indexing means one model behavior, one set of credentials, one place to swap providers. The alternative — a hardcoded model just for indexing — would mean a separate dependency, a separate API key, and a separate place where output quality could drift.

#### 8.6.4 Variable normalization on re-entry — resolver + A + B

When a workflow tool is re-entered after an `agentAction` with conditions — preferably by the model's own re-call carrying the branch variables as args (see §8.6.12), falling back to the agent loop's empty-args auto-recall (§5.4) — any values not in the call's args live in the surrounding transcript (`order_total="500"`, `order_status="has been delivered"`), and even when present rarely match the choice expressions exactly. [agent/workflow/local.py](../../src/botcircuits/agent/workflow/local.py) runs a normalization pipeline before merging values into slots and handing control to the executor: a deterministic **slot resolver** first, then **Layer B** (LLM extraction) only for what the resolver couldn't satisfy, then **Layer A** (type coercion) over the merged result.

**Gate.** The pipeline runs *only* when `saved_session.pendingBranch` is set (the prior turn paused on a branching agentAction) and the state has variables to resolve; Layer B additionally needs a `provider` wired in. Initial calls and re-entries into non-branching actions skip the whole pipeline — they pay zero extra LLM calls.

**Slot resolver — `slot_resolver.resolve_slots(...)`.** [agent/workflow/slot_resolver.py](../../src/botcircuits/agent/workflow/slot_resolver.py). Deterministic, zero-LLM resolution that runs before Layer B. For each branch variable, the first hit wins:

1. **Raw args** — the model passed the variable explicitly and it coerces to the declared `dataType`.
2. **Choice-value match** — the pending step's `choices[].expressionList` carries the literal values the branch compares against (`is`, `contains`, `starts with`, `ends with`). If exactly one of them appears (case-insensitive, token-boundary) in the last user message or raw args, that authored value is assigned — with its authored casing so the engine's `is` comparison matches. Values with `{slot}` placeholders are skipped.
3. **Typed extraction** — a number-typed variable resolves when the user's reply contains exactly one standalone number (digits inside identifiers like `sys_10001` don't count); a boolean-typed one when the reply (or its first word) is an unambiguous yes/no token.
4. **Question verbatim reply** — when the pending step is a `question` referencing a single string variable with no authored choice literals (the branch only checks emptiness/containment), the user's reply *is* the slot value, verbatim.
5. **Saved slot** — the variable already holds a coercible value from an earlier turn. Deliberately lowest priority: sources 1–4 read the fresh turn, so a new answer beats a stale one when a loop re-visits the same branching step.

The resolver never guesses — ambiguity (two choice literals matched, two numbers in the reply) means "unresolved". Each resolution logs `[workflow] slot resolver: <name>=<value> (<source>)` to stderr. Variables it can't satisfy are handed to Layer B; **when it satisfies all of them, the LLM call is skipped entirely** — which is both the token saving and the determinism guarantee: a value resolved here is the same value on every run.

**Layer B — `variable_normalizer.normalize(...)`.** [agent/workflow/variable_normalizer.py](../../src/botcircuits/agent/workflow/variable_normalizer.py). One `provider.complete(...)` round-trip with `tools=[]`, `hosted_mcp=[]`, `skills=[]`, restricted to the variables the resolver left unresolved (its allow-list means it can never override a deterministically resolved value). Inputs:

- The **filtered** variable schema: `variables_for_step(flow, pending_step_id)` walks the step's `choices[].expressionList[].variable` and returns only the matching entries from `flow.variables`. Listing irrelevant variables wastes tokens and tempts hallucination.
- The raw tool args, the action text, and `last_assistant_message` (provided by the agent loop via the tool's `context`).

The model returns `{normalized: {variableName: value, ...}}`. Three post-processing steps:

1. **Allow-list restriction.** Drop any key the model invented that isn't in the filtered schema.
2. **Hallucination guard.** Each value must appear (case-insensitive substring) somewhere in the source context (args JSON + action text + last assistant message). Booleans always pass (trivially present in any text); numbers match both as-typed (`500`) and stripped (`500.0` → `500`); empty strings pass (so `is empty` checks fire correctly). Values that don't appear get dropped with a stderr warning.
3. **Failure tolerance.** Any provider error, malformed JSON, or schema mismatch returns `{}` and logs a single stderr line. The workflow never aborts because of B; it degrades to "raw args + Layer A."

**Layer A — `_coerce_variables(...)`.** [agent/workflow/local.py](../../src/botcircuits/agent/workflow/local.py) (the scalar coercers it uses live in `slot_resolver.py`, shared with the resolver — one source of truth for type coercion). Deterministic type coercion against `flow.variables[].dataType`. Always runs when the workflow has an indexed schema, regardless of whether B ran. Behavior:

| dataType | Accepts | Produces | Drops |
|---|---|---|---|
| `number` | `int`, `float`, `"500"`, `"500.5"`, `"1e3"` | `int` or `float` | `bool`, words, currency-decorated strings |
| `boolean` | `True`/`False`, `"true"`/`"false"`/`"yes"`/`"no"`/`"on"`/`"off"`/`"1"`/`"0"`/`""` | `True`/`False` | ambiguous strings (`"maybe"`) |
| `string` | anything | `str(value).strip()` | nothing (string is the catch-all) |

Coercion failures emit `[workflow] dropping <name>=<value>: cannot coerce to <type>` and **omit the key from the merged slots**. The default branch in the choice evaluator is the safety net — a missing slot reads as the empty string, so `is empty` fires correctly and other operators no-match.

**Why drop on failure rather than raise.** The workflow's job is to keep the conversation moving. A coercion failure means the LLM didn't give us a usable value; the right answer is to take the default branch (often `apologize` / `ask_clarification` / similar) rather than crash the whole user-facing flow. The stderr warning makes the bug visible to the operator without breaking the run.

**Why no string case-folding.** Tempting (`"Delivered"` → `"delivered"` matches the enum), but it would corrupt case-sensitive identifiers like `"SKU-ABC"` or `"PROD-001"`. The indexer's prompt instead pushes enum values toward lowercase, and Layer B's `description` hint (e.g. `"one of: pending | shipped | delivered"`) lets the model normalize semantically. If a workflow really needs case-insensitive enum matching, an optional `allowedValues` field on the variable schema would be a future addition.

**Settings knob.** `workflow.normalize: true|false` in `settings.json` toggles Layer B. Validated by `cli/config.py::_parse_workflow`; unknown keys inside the `workflow` block are rejected at startup. Layer A always runs.

**The normalized values reach the engine via the same slot path the runtime already uses** — `run_workflow` writes them into `session_context.slots`, the executor hands the `message` to `evaluate_choices`, and the choice handler reads `message["data"]["sessionContext"]["slots"][variable]`. The engine itself doesn't know normalization happened; B and A are pure I/O fixups around the existing slot pipeline.

#### 8.6.5 Session resumption

A workflow pauses on every `agentAction`. The engine returns the action and the saved session (`{currentStep, slots, runningStep, pendingBranch, ...}`); `local.run_workflow` keeps that saved session in a module-global `_SESSIONS: dict[str, dict]` keyed by `session_id`. On re-entry with the same `session_id`:

1. Look up the saved session; if `pendingBranch` is set, normalize args (Layer B + Layer A) and run `_resolve_pending_branch` to override the entry point.
2. Merge (normalized) incoming `args` into per-journey slots (slot values stick across calls).
3. Run the engine from `currentStep` (or the branched-to state if step 1 fired) until the next `agentAction` pause or end.

When the workflow ends (or returns no action), `_SESSIONS.pop(session_id)` drops the entry so a future call with that id starts fresh. `_SESSIONS` is module-global for the same reason as `todo_write._STORE` and `_bg._REGISTRY` — the lifetime is tied to the tool's process, and routing it through `Agent` would force every consumer to thread a reference.

The tool result on a non-terminal step is `{"status": "ok", "workflow_name", "session_id", "action", "done": false, "kind", "running_step", "messages": [<engine frame>], "conditions", "choices", "variables"}`. `kind` is `"question"` when the engine paused on a `question` step (else unset). On a terminal turn (or one with no action), `action` is `None` and `done` is `True`. Note the LLM doesn't see this raw dict — `workflow_tool`'s handler renders it into a `WorkflowStepDirective` (§8.6.6); the dict shape is what `run_workflow` returns and what the eval runners consume.

#### 8.6.6 Multi-turn execution — closure state per workflow tool, context-aware handler

`workflow_tool()` captures a tiny mutable dict in the tool's handler closure so a single workflow can span many LLM turns. The handler signature is widened to accept an optional `context` dict from the agent loop (see §8.6.7):

```python
state: dict[str, str | None] = {"session_id": None}

async def _handler(args: dict, context: dict | None = None) -> str:
    ctx = context or {}
    result = await run_workflow(
        wf_name, args,
        session_id=state["session_id"],
        provider=provider,                                       # B's provider
        last_assistant_message=ctx.get("last_assistant_message", ""),
        last_user_message=ctx.get("last_user_message", ""),
        normalize_enabled=normalize_enabled,
    )
    action, done, kind = result["action"], bool(result["done"]), result.get("kind")
    state["session_id"] = None if done else result.get("session_id")
    if not action:
        return compose_workflow_empty_action(wf_name)
    directive = compose_workflow_step_directive(wf_name, done=done, kind=kind)
    return directive.as_plain_text(action)
```

The first call passes `session_id=None`; `run_workflow` mints a uuid and `_SESSIONS` keys the workflow conversation on it. Subsequent calls re-enter the same `session_id` so the engine advances the same workflow instance instead of starting a new one. The state clears when the workflow ends so the *next* invocation starts fresh.

**The directive asks for a re-call only when a branch is pending.** `compose_workflow_step_directive` ([agent/workflow/cli_commands.py](../../src/botcircuits/agent/workflow/cli_commands.py)) frames a plain step as something to *perform*, full stop — no "call '<name>' again" footer, because the agent loop auto-recalls the workflow tool once the model stops issuing tool calls (§5.4). Two exceptions: a `question`-kind step tells the model to call `human_feedback` (which pauses the loop) rather than answer on the user's behalf; and a step with `branch_variables` (the engine paused on a branching step — §8.6.12) gets a footer asking the model to re-call the tool with the values it observed for those variables once the step is done. The wording lives in `cli_commands` so the in-process CLI wrapper here and the out-of-process Hermes wrapper render identical text; `kind` and `branch_variables` default to `None` so Hermes callers that don't pass them get the plain action framing.

`tool._workflow_state` is exposed (assigned post-construction) so the agent loop can introspect mid-run workflows without touching the closure directly — see `active_workflow_names()` above.

#### 8.6.7 Handler context plumbing — `LocalTool.handler(args, context=None)`

Layer B needs the last assistant message to ground its hallucination guard. Pulling it out of the agent's `Conversation` requires the workflow tool to see something it doesn't naturally have — surrounding loop state. The fix is a single optional second argument on every tool handler.

**Registry-side introspection.** [agent/tools/registry.py](../../src/botcircuits/agent/tools/registry.py) inspects each handler's signature once at call time. A handler accepts `context` if it has ≥2 positional parameters, or a parameter named `context`, or `**kwargs`. Two-arg handlers receive `(args, context)`; one-arg handlers receive `(args)` unchanged. This is *additive* — every existing builtin keeps working without modification.

**Loop-side population.** [agent/core.py](../../src/botcircuits/agent/core.py) builds a `tool_context` dict once per turn before dispatching tool calls, and passes the same snapshot to every concurrent tool call in that turn:

```python
tool_context = {
    "last_assistant_message": _last_assistant_text(convo.messages),
    "last_user_message":     _last_user_text(convo.messages),
    "session_id":            convo.session_id,
}
results = await asyncio.gather(*[
    self.tools.run(tc.name, tc.arguments, tool_context)
    for tc in resp.tool_calls
])
```

`_last_assistant_text` / `_last_user_text` walk `convo.messages` in reverse and return the most recent text block of that role (truncated to 2KB via `_CONTEXT_LAST_ASSISTANT_CHARS`), or `""` if none exists yet. `_last_user_text` deliberately skips `tool_result` blocks (which also live on user-role messages) — Layer B wants the human's actual utterance, not tool output that already landed.

**Why a snapshot, not a live reference.** Tools run concurrently via `asyncio.gather` / `asyncio.as_completed`. Passing a snapshot avoids races where one tool mutates state another tool is reading. The 2KB cap is the only token-budget knob the loop applies to context; Layer B's prompt does the rest of the trimming.

**Why a dict, not a typed object.** Future fields (`recent_tool_results`, `recent_user_message`, etc.) are likely. A dict means adding one is a one-liner in `core.py` and an opt-in read in the tool that wants it — no signature change, no version bump.

#### 8.6.8 System-prompt re-entry reminder

The model advances workflows by *not* acting (the loop auto-recalls — §5.4) — except on a branching step, where the model is asked to re-call the tool itself, carrying the branch variables (§8.6.12). [agent/core.py:_with_workflow_reminder](../../src/botcircuits/agent/core.py) appends a `[Active workflow]` block to the system prompt **for every provider call** while any workflow tool reports `session_id != None`. On a non-branching step it keeps the model from re-calling (which would double-advance):

```
[Active workflow] The workflow tool '<name>' is mid-execution. Perform
ONLY the action of the current step (call a tool, send a reply, or call
'human_feedback' if the step asks the user a question). Do NOT call
'<name>' yourself — the next step is requested for you automatically
once you finish acting.
```

When the active workflow's `_workflow_state` carries non-empty `branch_variables` (read via `workflow_branch_variables(reg, name)`), the block flips to the re-call-with-args form instead:

```
[Active workflow] The workflow tool '<name>' is mid-execution on a
branching step. FIRST perform the action of the current step (...).
Once the step is genuinely complete, call '<name>' passing the values
you observed for these arguments — they decide the next step. Omit any
you don't actually have; never invent values:
- order_status (string): one of: pending | shipped | delivered
```

The reminder is computed per-call (not stored on `convo.system`) because the active set can change between turns — a workflow that just finished should stop nagging the model on the next turn. Computing it inline costs one dict lookup per provider call; cheap, and the alternative (caching) would have to be invalidated on every workflow state change.

At most one workflow runs at a time today (the loop picks `names[0]`); the data structure supports a list because a future "parallel workflows" feature is plausible (and `_auto_recall_calls` already injects one recall per active workflow).

#### 8.6.9 Design choices

**Branching in `agentAction`, not a separate `choice` type.** Branching evaluates the slot values *after* the LLM has had a chance to fill them. A standalone `choice` state would fire too early — slot values from the previous agentAction don't exist until the loop re-enters the workflow tool (auto-recall) and normalization extracts them from the transcript. Folding branching into `agentAction` makes the dependency on LLM action explicit: emit → LLM acts → re-enter → branch. The `pendingBranch` marker is what bridges the gap between "we paused with conditions" and "now we have the values to evaluate them against."

**Built-ins take precedence on name collision.** `register_workflows` walks the records and skips any whose name is already registered. The CLI prints a yellow `[workflow] skipped (name collides with built-in tool): ...` line so the conflict is visible. Rationale: a workflow accidentally named `shell_exec` must never silently shadow the built-in tool — security-relevant tools need stable identities.

**Why a separate subpackage, not another builtin.** A workflow isn't a single tool; it's *N* dynamically-discovered tools whose surface depends on the workflows directory. Putting it in `agent/tools/builtins/` would force one factory per workflow at import time, which doesn't work — discovery happens async at startup and the set can change between runs. The subpackage owns the load / register lifecycle and produces standard `LocalTool` instances the registry already knows how to handle.

**Why no LLM/RAG fallback on unmatched choices.** The engine is meant for *deterministic* agent-action graphs. When no choice matches, the LLM that's already driving the agent loop is the natural fallback — it sees the workflow ended and decides what to do next. Embedding an extra LLM hop inside the engine would just duplicate that.

**Why no interruption handling.** There is no end-user inside the engine — the LLM is the caller, and it controls when to re-enter the workflow via the system-prompt reminder. Interruption is just "the LLM chose to call a different tool"; no plumbing needed.

**A + B, not A or B.** Type coercion alone (A) doesn't fix semantic drift (`"shipped already"` → `"shipped"`). LLM normalization alone (B) is non-deterministic and can produce values that fail the runtime's typed comparisons (`"500"` vs `500`). Stacking them means B handles meaning and A guarantees the type contract before the engine sees the slots. If B fails (provider error, bad JSON), the workflow still has A as a guaranteed minimum, plus the executor's default-branch fallthrough — never aborts.

**Deterministic resolver before B, not instead of B.** Most branch re-entries don't need interpretation at all — the answer is an authored choice literal sitting in the user's reply, a lone number, a yes/no, or a value already collected. Resolving those in code makes the common path free (no extra LLM round-trip) and reproducible (same input, same slots, same branch). B remains the fallback for genuinely semantic answers (`"the bigger one"` → `"large"`), and because it only sees the unresolved subset, its prompt shrinks too. The resolver's no-guessing rule (ambiguity → unresolved) is what keeps this safe: it never trades correctness for determinism — it just refuses to answer and lets B arbitrate.

**CLI wiring.** [cli/app.py](../../src/botcircuits/cli/app.py) calls `register_workflows(registry, provider=provider, normalize_enabled=cfg.workflow["normalize"])` after `default_registry()` runs (so built-ins are present and collisions are detectable). `LocalWorkflowError` (raised when a workflow file is malformed) is caught and reported as `[workflow] ...` with exit code 2 — same pattern as the tools-config error path. The `workflow build` subcommand reuses the same provider construction via `load_cli_config(args)` + `make_provider(...)`, so author-time inference and runtime normalization always run on the same model.

#### 8.6.10 Authoring workflows — the `build_workflow` built-in

[agent/tools/builtins/build_workflow.py](../../src/botcircuits/agent/tools/builtins/build_workflow.py). The agent can create or update workflow JSON files mid-chat without the user hand-editing anything. The tool collapses four otherwise-manual steps into one call:

1. **Validate** the model-supplied `workflow` payload against the supported step set (`start`, `agentAction`, `question`), confirm every `next`/`conditions[].next` pointer resolves to a known step id, require `settings.action` on each `agentAction`/`question`, and assert the `name` is slug-safe (`^[a-zA-Z0-9_-]+$` — OpenAI's strictest tool-name regex, since the name doubles as the LLM-facing tool name). Validation errors return `{error: "..."}` so the model sees the failure rather than writing a half-broken file.
2. **Confirm.** Render a y/N block with the required `summary` string and an ordered step preview computed by walking from `start` along `next`. Branches show inline (`↳ if '<condition>' → <next>`). The block uses the shared [`_confirm`](../../src/botcircuits/agent/tools/builtins/_confirm.py) helper so the UX matches `plan_and_confirm` / `write_file` / `edit_file`. Denying returns `{denied: true, message: "..."}` with explicit anti-retry guidance.
3. **Write the raw source.** Writes the un-indexed workflow under `.botcircuits/workflows/<name>.json` as `{name, description, flow: {start, steps}}` — the source-of-truth file the human can re-open and hand-edit later. The write happens *before* indexing so the editable copy lands on disk even if the LLM-driven step below fails.
4. **Index + emit the build artifact.** Deep-copies the raw record, runs `condition_processor.generate_expressions_and_variables(flow, provider)` on the *same* `LLMProvider` the chat is using, and writes the indexed copy to `.botcircuits/workflows/.build/<name>.json`. **The agent runtime only loads from `.build/`** — see §8.6.1 — so an un-built workflow isn't callable. If indexing raises, the build artifact is intentionally **not** written (so a stale un-built copy never masquerades as runnable); the result carries `index_error` pointing the user at `botcircuits-cli workflow build --name=<name>` to retry manually. If no provider is wired in at all, the result carries `index_note` with the same recovery instruction.

**The `on_built` callback — live tool registration without a restart.** `build_workflow_tool(...)` takes an optional `on_built` callback fired after a runnable build artifact lands on disk. The CLI installs one (see `_make_workflow_refresh_callback` in [cli/commands.py](../../src/botcircuits/cli/commands.py)) that re-runs `register_workflows(agent.tools, provider=...)` so the agent picks up the new/edited workflow as a callable tool on the very next turn. The callback only fires when `built_written` is true — failed indexing produces no runnable artifact and therefore no live registration. Both sync and async callbacks are supported; the tool awaits the return value when it's awaitable.

**Provider plumbing.** Most builtins are pure stdlib; `build_workflow` is the first one that needs the agent's `LLMProvider` at register time. The plumbing is one new optional kwarg on `default_registry(tools_config, *, provider=None)` plus a small `_PROVIDER_AWARE_TOOLS = ("build_workflow",)` allow-list — when present, the provider is threaded into that tool's `register(reg, **config)` call. The CLI passes `provider=provider` from [cli/app.py](../../src/botcircuits/cli/app.py); the gateway does the same in [gateway/app.py](../../src/botcircuits/gateway/app.py). Library callers who omit the kwarg get a working `build_workflow` that writes the file but skips indexing — the same fallback path as an indexer failure.

**Lazy registration.** `build_workflow` is in `_LAZY_BUILTINS`, so `default_registry()` *skips* it by default — only the explicit `/workflow add|edit` slash command (or a `tools.build_workflow: {}` entry in JSON) loads it. Rationale: 99% of chat turns don't author workflows, but the tool's description is large enough to be worth keeping off the model's tool catalog when not in use. The slash handler calls `register_builtin(agent.tools, "build_workflow", provider=..., config={"on_built": _refresh})` once, which is a no-op on subsequent `/workflow` invocations within the same session.

**Why not the `agent/workflow/` subpackage.** `register_workflows()` lives there because it discovers per-record tools at startup. `build_workflow` is the inverse — one fixed tool that *produces* records. It belongs alongside the other gated stdlib-only builtins, not in the workflow loader. The two share `condition_processor` but nothing else, so the import is one targeted line, not a structural coupling.

**Why lazy imports inside the file.** `agent/tools/__init__.py` is reachable from `providers/base.py` via `agent.mcp`, so top-level `from ....providers.base import LLMProvider` would re-trigger the same circular-import the rest of the package already dodges. `LLMProvider` is `TYPE_CHECKING`-only here; `condition_processor` is imported *inside* the handler so the heavyweight provider/types graph stays out of module-load time. The cost is one extra `import` lookup per call — negligible next to a multi-second LLM round-trip.

**Why the input schema mirrors the on-disk file.** The model already knows the workflow file shape (it's documented in the workflow section above and frequently appears in example JSON). Reusing `steps`/`settings` keys plus a step-root `conditions` means the LLM doesn't have to learn a parallel intermediate schema for the tool — what it would write to disk by hand is what it passes as `workflow.steps` here. The `name` field doubles as the filename and as the registered tool name so the model picks a single identifier rather than two.

**Why `conditions` lives at the step root, not inside `settings`.** `settings` holds the *step-type-specific payload* — for `agentAction` that's `settings.action`, the natural-language instruction the LLM has to execute. `conditions` (and its compiled sibling `choices`) describes *where to go next*, which is control flow — the same category as `type` and `next`. Putting all control-flow fields at the step root keeps the mental model clean (`type`/`next`/`conditions` cluster together) and lets a future step type carry an entirely different `settings` schema without touching the branching surface.

**Why guidance lives in the tool description.** The tool's `description` carries the full authoring flow (clarify first, call once, report path / index_error, don't hand-edit with write_file). The system prompt stays generic code-gen behavior — per-tool rules belong with the tool so they appear in the model's tool catalog and vanish when the tool is disabled, matching the convention already used by `plan_and_confirm` / `write_file` / `edit_file`.

#### 8.6.11 Human feedback + loop-driven advancement

Two changes moved workflow *advancement* out of the model's hands and gave question-asking a first-class pause.

**The `human_feedback` builtin.** [agent/tools/builtins/human_feedback.py](../../src/botcircuits/agent/tools/builtins/human_feedback.py). An eagerly-registered (non-lazy), non-gated tool taking one `question` argument. Its handler is a thin echo — it returns `{"paused": true, "question": <text>}`. It does nothing on its own; the *behavior* lives in the agent loop, which recognizes a `human_feedback` call and treats it as a pause (below). The tool name uses an underscore (`human_feedback`) to satisfy every provider's tool-name regex. The constant `HUMAN_FEEDBACK_TOOL` is exported so the loop and the registration can't drift on the name.

**Pause mechanism — terminal turn.** After the loop runs a turn's tool calls, `_human_feedback_pause(tool_calls, results)` ([agent/core.py](../../src/botcircuits/agent/core.py)) scans for a `human_feedback` call; if one ran, it pulls the question (from the JSON result, falling back to the call's `question` arg) and the loop ends the turn, returning the question as the assistant's reply. This is a *terminal-turn* pause: control returns to the caller (`chat()` / REPL / gateway) exactly as a normal end-of-turn would, and the user's next message is their answer. We chose this over a handler that blocks reading stdin mid-loop because the blocking design couples the tool to the CLI and breaks the gateway and streaming `done`-event contract; a terminal turn fits all three callers with no new control flow.

**Auto-recall — advancement without coaxing.** Previously the workflow tool's result string and the `[Active workflow]` reminder both *told the model to re-call the tool* to advance, and the model would sometimes forget. Now: when a turn ends with no model-issued tool calls but `active_workflow_names(reg)` is non-empty, `_auto_recall_calls(reg)` synthesizes one workflow tool call per active workflow (empty args, ids prefixed `wf-autorecall-`), the loop runs them like any other tool call, and the returned next-step directive feeds the next provider call. The model therefore only ever *performs* steps; the loop owns "fetch the next one." This is gated on `enable_workflows`, so the eval framework's no-workflow baseline is unaffected, and on there being an active workflow, so an ordinary empty-tool turn with no workflow still terminates normally. For branching steps auto-recall is the *fallback*: the preferred path is the model's own re-call carrying the branch variables (§8.6.12), which — being a model-issued tool call — suppresses the auto-recall for that turn by construction.

**Interaction.** A `human_feedback` pause deliberately runs *before* any auto-recall would (it's checked after the tool results land, and it `return`s), so a `question` step doesn't advance past an unanswered question. On the user's next turn, the model acts (often just acknowledging the answer), produces no tool calls, and *then* auto-recall fires to pull the step after the question — at which point Layer A/B normalization extracts the answer's values from the recent transcript exactly as for any other re-entry.

**Why the engine `question` step and the free-form tool coexist.** A workflow author marks a step `type: "question"` to *force* the model to ask (the directive instructs a `human_feedback` call); the same tool is also on the model's catalog for moments it judges on its own that it needs the user. Both paths land on the identical pause, so there's one mechanism to reason about regardless of who decided to ask.

#### 8.6.12 Branching steps — the model's re-call carries the slots

Diagrams and full rationale: [docs/design/tool-system-llm-flow.md](../design/tool-system-llm-flow.md).

The empty-args auto-recall left the re-entry pipeline blind to values that never appear in the 2KB last-text snapshot — above all values that surfaced in `tool_result` blocks (the model looked an order up via an API tool; the status lives in the tool's output, not in anyone's prose). The resolver's highest-priority source (raw args) could never fire because auto-recall always passed `{}`, so branching leaned on Layer B guessing over an incomplete snapshot.

The fix inverts the carrier: when the engine pauses on a branching step, the **model's own tool call in the main loop** re-enters the workflow with the slot values as ordinary tool-call arguments. Moving parts:

- **`run_workflow` surfaces `branch_variables`** ([agent/workflow/local.py](../../src/botcircuits/agent/workflow/local.py)): when the paused session carries a `pendingBranch`, the result includes `variables_for_step(flow, pending.stepId)` — the same filtered schema Layer B uses.
- **`workflow_tool` mirrors them onto the tool surface** ([agent/workflow/__init__.py](../../src/botcircuits/agent/workflow/__init__.py)): the handler stores them in `_workflow_state["branch_variables"]` and rewrites the tool's `input_schema` so providers advertise the variables as optional properties (typed via `dataType` → JSON-schema type). Non-branching pauses and terminal turns reset the schema to empty. ReAct mode picks the schema up automatically because the preamble is re-rendered per call.
- **Directive + reminder ask for the re-call** (§8.6.6, §8.6.8): the step directive's footer and the `[Active workflow]` block list the variables (`render_branch_variable_lines`) and instruct: perform the step first, then call `<wf>` with the observed values, omitting anything not actually observed.
- **Auto-recall demoted to fallback** (§8.6.11): unchanged code — it only fires on turns with no model-issued tool calls, so a turn containing the model's re-call never double-advances, and a model that forgets degrades to the old resolver→B→A path, never to a stall.
- **No pipeline change**: model-supplied args flow through the existing `raw_args` path, hit the slot resolver's source #1, and get type-coerced by Layer A. Layer B becomes the rare semantic fallback instead of the routine crutch.

#### 8.6.13 Engine-driven execution (inversion of control)

**This is the current execution model.** §8.6.2–§8.6.12 describe the original LLM-driven mechanism, now retained only as the legacy `run_workflow` fallback. Here the **engine owns the loop**: once a workflow tool fires, the engine walks the graph and invokes the LLM as a subroutine per branch-delimited *segment* with a constant-size, cache-stable prompt. The state machine is the memory, not the conversation history.

**Why.** The old design made every step a full provider round-trip replaying the entire growing history (system prompt + all turns + all tool schemas + a mutating `[Active workflow]` reminder) — roughly quadratic in step count, and it defeated prompt caching because the system prompt mutated per step. Advancement also depended on the model *choosing* to re-call the tool (hence the MANDATORY reminder + auto-recall fallback). Inverting control removes both costs: **cost scales with branch decisions (segments), not steps**, and the model can't skip, reorder, or imitate stale history.

**Control flow.**

```
Agent.chat / chat_stream  (trigger path unchanged: model calls the workflow tool)
   workflow tool handler  ──►  run_workflow_engine(flow, run_segment=Agent._run_segment, ...)
                                  │ per segment:
                                  │   Agent._run_segment(ENGINE_SYSTEM_PROMPT  ← static, cached
                                  │                      + terse segment payload
                                  │                      + segment tools + record_slots)
                                  │   → executes tool calls (Tier 0/1 slot capture)
                                  │   → evaluate_choices() at the branch boundary (unchanged)
                                  │   → advance
                                  ▼
                         [yield] workflow end  → one-line summary into history
                                 OR user pause → pending question (parks resume cursor)
```

**New modules.**

| File | Role |
|---|---|
| `engine/segments.py` | `compute_segments(flow)` — pure, build-time. Partitions the graph into maximal runs of consecutive non-branching steps; a step carrying `choices`/`conditions` (or a `question`) terminates a segment. Each segment is `{id, steps, branchStep}`. Emitted into the `.build/` artifact as `flow["segments"]`; the runner falls back to one-step-per-segment when absent. |
| `engine/runner.py` | `run_workflow_engine(...)` — owns the loop. Walks segments, calls `run_segment` per segment, folds captured slots in, evaluates the terminating branch via the unchanged `evaluate_choices`, persists per-branch **decision records** (`{variable, operator, value, slot_value, slot_source, matched_choice, llm_extracted}`), and **yields** on workflow end or user-interaction pause. `EngineResult` carries `done`/`paused`/`summary`/`question`/`paused_step`/`slots`/`decisions`. |
| `engine/segment_exec.py` | The static `ENGINE_SYSTEM_PROMPT`, the synthetic `record_slots` capture tool (`build_record_slots_tool`), and the terse per-segment payload builder (`build_segment_user_message`). |

**`Agent._run_segment`** ([agent/core.py](../../src/botcircuits/agent/core.py)). One bounded inner loop of provider call + concurrent tool execution, factored out of `chat`/`chat_stream`. It runs against a static system prompt + a single segment-payload user message + the minimal tool set (the agent's real tools **minus** workflow tools, **plus** `record_slots` when the segment branches). It returns a `SegmentResult(text, captured_slots, paused, question)`. Two interceptions: a `record_slots` call writes into the capture sink (and terminates the branch segment — its branch-relevant work is done); a `human_feedback` call pauses the segment. A `event_sink` argument lets the streaming path forward segment `text_delta`/`tool_call`/`tool_result` events to the UI (drained via an `asyncio.Queue` in `chat_stream`), so a workflow stays live on screen.

**Tiered slot resolution (§3.2 of the design).** The branch variables a segment needs are filled in priority order:

- **Tier 0 — no LLM.** The deterministic `slot_resolver.resolve_slots` (authored choice literals, typed extraction, saved slots) runs in the runner's backfill hook before evaluating a branch.
- **Tier 1 — same call.** The model reports branch variables via the synthetic `record_slots` tool *in the segment call already being made* — no extra round-trip. Schema built from `variables_for_step`.
- **Tier 2 — cheap-model fallback.** Only when Tiers 0/1 leave a branch variable unresolved: one extraction call via the existing `variable_normalizer.normalize` (the old "Layer B"). Tagged `tier2_normalization` in usage accounting.

All values still pass Layer-A coercion and the hallucination guard. A branch variable explicitly marked `required: true` that remains unfilled routes to a **clarification pause** (a user-facing question) rather than silently taking the default branch; an *unmarked* (optional) empty falls through to the default as before — so the common "no early-termination value" path isn't over-asked.

**Cache stability.** `ENGINE_SYSTEM_PROMPT` is fixed for the whole run; everything per-segment rides the user message after the cached prefix. The segment tool set is byte-stable across calls. This is what makes each segment call hit the provider prompt cache.

**Summary handoff.** On completion the workflow tool returns a single line — `workflow <name> completed: <outcome>, slots {...}` — into the conversational history, not the step-by-step transcript, keeping post-workflow conversation cheap. On a pause it returns the pending question and parks `{engine_paused_step, engine_slots}` on `_workflow_state`; the next call resumes from that cursor. `session_id != None` now means "paused", which is what `active_workflow_names` reports.

**Token accounting (§7).** `LLMProvider.record_usage` buckets each call into `usage_by_purpose` keyed by a `usage_purpose` tag the callers set: `conversational` (main loop), `segment` (engine segment call), `tier2_normalization` (cheap-model fallback). The fourth tag, `trigger` (the conversational turn that fired a workflow tool), can't be known pre-call — the loop retags it **post-hoc** via `provider.reclassify_call(...)` once a workflow tool is seen in that turn's tool calls (totals unchanged, only the breakdown shifts). The eval runner ([evaluation/runner_agent.py](../../src/botcircuits/agent/workflow/evaluation/runner_agent.py)) captures the per-run delta; the report ([evaluation/report.py](../../src/botcircuits/agent/workflow/evaluation/report.py)) renders it.

**Three-way comparison (§7).** The harness ([evaluation/harness.py](../../src/botcircuits/agent/workflow/evaluation/harness.py)) runs each case three ways and emits all three per case: `workflow_on` (the engine-driven Agent under test), `workflow_off` (the prompt-driven baseline, `enable_workflows=False` + the spec in the system prompt), and `workflow_as_tool_run` (the legacy per-step `run_workflow` driver via `run_case_workflow`, measured standalone — the engine now intercepts advancement, so the pre-inversion path is no longer reachable through a real Agent). The report shows the engine-mode per-purpose token breakdown next to the baseline's, making the projected cost reduction visible per case.

**What's reused unchanged.** `evaluate_choices` (branch evaluation), `slot_resolver` (Tier 0), `variable_normalizer` (Tier 2), `condition_processor` (the indexer), `build_workflow`, the `.build/` loader, and `WorkflowStateContext` / `_SESSIONS`. The inversion is about *who drives the loop*, not a rewrite of the deterministic pieces.

---

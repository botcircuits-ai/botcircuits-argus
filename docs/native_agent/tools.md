# Tools (`agent/tools/`)

```
 model emits tool_call(name, args)
        │
        ▼
 ToolRegistry.run(name, args, context)
        │
        ├─ 1 permissions.evaluate(name, args)     deny → blocked
        │                                         ask  → y/N prompt
        │                                         allow → proceed
        ▼
 ┌─ 2 dispatch to the handler ────────────────────────────────┐
 │   builtin        MCP tool       skill         workflow     │
 │   (shell, fs,    (local MCP     (renders      (enters the  │
 │    web, memory)   session)       SKILL.md)     engine)     │
 └──────────────────────────┬─────────────────────────────────┘
                            ▼
 3 (result_text, is_error) ──► tool_result block ──► back to the model
   error dicts / non-zero exit_code are flagged is_error automatically
```

A tool is a `LocalTool`: name + description + JSON-schema parameters + a
handler. Handlers are sync or async and return a string or any
JSON-serializable value; errors come back as strings the model can read and
recover from. A handler that accepts a second `context` argument receives the
loop's context snapshot (see [context.md](context.md)) — the registry checks
the signature, so 1-arg handlers work unchanged.

## Registry

`ToolRegistry` turns tools into provider tool specs and dispatches calls by
name. Every dispatch first consults the `PermissionSet`
(see [permissions.md](permissions.md)). `default_registry(tools_config=...)`
builds the standard set, threading per-tool overrides from the layered
`settings.json`.

## Builtins (`agent/tools/builtins/`)

| Tool | Purpose |
|---|---|
| `shell_exec` / `shell_status` / `shell_stop` | run commands, incl. background jobs |
| `read_file` / `write_file` / `edit_file` | file access |
| `list_dir` / `glob_search` / `grep_search` | discovery / search |
| `todo_write` | task list the UI renders |
| `plan_and_confirm` | present a plan, gate on user approval |
| `human_feedback` | ask the user a question (pauses the loop) |
| `memory` | edit persistent memory files |
| `search_memory` | keyword recall from past sessions (excludes the current one) |
| `web_search` / `web_extract` | web lookup and page extraction |
| `delegate` / `fan_out` | spawn isolated subagents (registered on `Agent.start()`, see [subagents.md](subagents.md)) |
| `build_workflow` | author a workflow (lazy-registered via `/workflow`) |
| `add` / `now` | arithmetic / current time |

Adding a builtin = one file under `builtins/` + one entry in the `_BUILTINS`
map in `tools/__init__.py`.

Two special registration groups in `tools/__init__.py`:
`_PROVIDER_AWARE_TOOLS` get the caller's `LLMProvider` at register time (LLM
-driven tools like the workflow builder); lazy tools stay off the model's
list until explicitly registered (`register_builtin`).

Workflow tools (one per discovered workflow) are registered by
`agent/workflow` and carry a `_workflow_state` attribute — that marker is how
the loop gates, retags usage for, and (in segments) excludes them.

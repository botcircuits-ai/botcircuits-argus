# Overview, Package Layout & Architecture

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 1. Goals & Non-Goals

### Goals
- One agent loop that works against any major LLM provider.
- First-class **MCP** support, with both hosted (provider-side) and local (in-process) modes.
- First-class **Skills** support — both *hosted* skills (mapping to each provider's code-execution surface) **and** *filesystem* skills (Claude-Code-style `SKILL.md` directories auto-discovered and exposed as tools).
- **Persistent memory** — MEMORY.md + USER.md under `~/.botcircuits/memories/` injected into the system prompt at session start; mutated via a `memory` tool with `add` / `replace` / `remove` actions.
- **Streaming** all the way down: a UI can watch text deltas, tool calls, and tool results as they happen.
- **Declarative configuration** via layered files in `~/.botcircuits/` and project `.botcircuits/`: `settings.json` for provider/model/tool config, `mcp.json` for MCP server entries, plus sibling `*.local.json` files for personal overrides.
- Pure Python, fully async, no framework lock-in (FastAPI is optional sugar).

### Non-Goals
- Persisting conversation history. The store is in-memory by design; persistence is a 30-line subclass if you need it.
- Hiding provider differences perfectly. We expose a few capability flags (`supports_hosted_mcp()`) so callers can adapt rather than pretend the world is uniform.
- Token-usage accounting, retries, or rate limiting. These belong in adapter layers and aren't part of the core loop.
- Loading arbitrary code from the JSON config. Tool *parameters* go in JSON; tool *implementations* live in code, where the security review can find them.

---

## 2. Package Layout

```
src/botcircuits/
├── __init__.py              # public API re-exports + .env loader
├── types.py                 # ToolCall, Message, LLMResponse, StreamEvent, ProviderStreamEvent
│
├── agent/                   # multi-round tool-use loop
│   ├── core.py              #   Agent (chat, chat_stream)
│   ├── store.py             #   ConversationStore + Conversation (memory snapshot injected here)
│   ├── memory.py            #   MEMORY.md / USER.md storage + read/mutate API + threat scrub
│   ├── mcp.py               #   MCPServer + LocalMCPManager
│   ├── skill/               #   skills — hosted spec + filesystem loader
│   │   ├── spec.py          #     SkillSpec (hosted code-exec request)
│   │   └── local.py         #     LocalSkill discovery, SKILL.md parser, render_body
│   └── tools/               #   local tools as a package, one file per tool
│       ├── registry.py      #     ToolRegistry + LocalTool
│       └── builtins/
│           ├── arithmetic.py       # `add`
│           ├── time.py             # `now`
│           ├── shell.py            # `shell_exec`  (y/N gated, foreground + background)
│           ├── shell_status.py     # `shell_status`
│           ├── shell_stop.py       # `shell_stop` (y/N gated)
│           ├── _bg.py              # background process registry + ring buffers
│           ├── read_file.py        # `read_file`
│           ├── write_file.py       # `write_file` (y/N gated)
│           ├── edit_file.py        # `edit_file`  (y/N gated, unified diff)
│           ├── list_dir.py         # `list_dir`
│           ├── glob_search.py      # `glob_search`
│           ├── grep_search.py      # `grep_search`
│           ├── todo_write.py       # `todo_write` (live list, in-memory store)
│           ├── plan_and_confirm.py # `plan_and_confirm` (y/N gated)
│           ├── build_workflow.py   # `build_workflow` (y/N gated, NL → workflow JSON + indexer; lazy)
│           ├── memory.py           # `memory` — add/replace/remove on MEMORY.md / USER.md
│           ├── human_feedback.py   # `human_feedback` — ask the user a question; pauses the loop
│           └── _confirm.py         # shared y/N + auto-mode helpers
│
├── agent/workflow/          # On-disk workflows registered as LocalTools
│   ├── __init__.py          #   fetch_workflows / run_workflow / workflow_tool /
│   │                        #   register_workflows / active_workflow_names; engine handoff
│   ├── local.py             #   discover *.json, legacy per-step driver, A-layer coercion
│   ├── condition_processor.py  # `workflow build` — NL conditions → choices + variables
│   ├── slot_resolver.py     #   deterministic slot resolution (Tier 0) + scalar coercers
│   ├── variable_normalizer.py  # Tier-2 LLM extraction on re-entry (unresolved vars only)
│   └── engine/              #   trimmed port of botcircuits-runtime-handler STM
│       ├── runner.py        #     ENGINE-DRIVEN loop: walks segments, branches, yields
│       ├── segments.py      #     compute_segments(flow) — build-time branch-delimited runs
│       ├── segment_exec.py  #     static ENGINE_SYSTEM_PROMPT + record_slots + payload
│       ├── executor.py      #     LEGACY state-machine loop + pendingBranch resolver
│       ├── state.py         #     WorkflowStateContext (saved session, slots)
│       ├── utils.py         #     interpolation + next-state helpers
│       └── handlers/
│           ├── action.py    #       agentAction handler (action emit + branch setup)
│           ├── question.py  #       question handler (action emit tagged kind:"question")
│           └── choice.py    #       evaluate_choices helper (reused by the engine runner)
│
├── providers/               # LLM backends, one file per provider
│   ├── base.py              #   LLMProvider ABC
│   ├── anthropic.py
│   ├── openai.py
│   └── gemini.py
│
├── cli/                     # interactive command-line client
│   ├── __main__.py          #   `python -m botcircuits.cli`
│   ├── app.py               #   arg parsing, REPL loop
│   ├── commands.py          #   slash-command dispatch (/help, /memory, /skills, /workflow add|edit, …)
│   ├── commands_mcp.py      #   `mcp add/remove/list/test` subcommands
│   ├── commands_workflow.py #   `workflow build --name=...` subcommand (writes to `.build/`)
│   ├── config.py            #   CLIConfig + JSON load/resolve/mutate (incl. workflow.normalize)
│   ├── settings.py          #   layered settings.json + mcp.json loaders + parsers
│   ├── system_prompt.py     #   DEFAULT_SYSTEM_PROMPT used when no user override is set
│   ├── render.py            #   stream / blocking renderers
│   └── ansi.py              #   color helpers
│
└── gateway/                 # FastAPI wrapper (JSON + SSE) + multi-channel message gateway
    ├── __main__.py          #   `python -m botcircuits.gateway`
    ├── app.py               #   FastAPI app + lifespan + provider/registry + MessageGateway
    ├── routes.py            #   /healthz, /chat, /chat/stream, /sessions/{id}/reset, /messaging/status
    ├── schemas.py           #   pydantic request/response
    ├── sse.py               #   Server-Sent Events serializer
    ├── messaging.py         #   MessageGateway — channel registry + inbound→agent→outbound routing
    ├── messaging_config.py  #   env + .botcircuits/messaging.json loader
    └── channels/            #   pluggable platform adapters (one file per channel)
        ├── base.py          #     Channel ABC, InboundMessage, OutboundMessage, ChannelError
        ├── whatsapp.py      #     Meta WhatsApp Cloud API (verify GET + events POST + Graph send)
        ├── slack.py         #     Slack Socket Mode (outbound WebSocket via slack_sdk, chat.postMessage)
        ├── webhook.py       #     Generic webhook (Bearer in, configurable POST out)
        └── cron.py          #     60s-tick scheduler, 5-field UTC cron matcher, optional fan-out
```

**Why this shape.** Each file is one responsibility. Providers are siblings of `agent/` because they're a swappable backend the agent depends on through an ABC, not internals of the loop. The `tools/builtins/` package gives every new tool one file plus one entry in a dispatch table. The CLI is split so config parsing, slash commands, and the chat REPL can each be tested without dragging in the others.

---

## 3. High-Level Architecture

```
   ┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐
   │   CLI (REPL)       │  │  FastAPI Gateway   │  │   Library use      │
   │  botcircuits-cli   │  │  /chat /chat/stream│  │  Agent(...)        │
   └─────────┬──────────┘  └─────────┬──────────┘  └─────────┬──────────┘
             │                       │                       │
             ▼                       ▼                       ▼
   ┌────────────────────────────────────────────────────────────────────┐
   │                              AGENT                                 │
   │                  (agent/core.py — async loop)                      │
   │                                                                    │
   │  chat() / chat_stream(user_input, session_id) → StreamEvent...     │
   │                                                                    │
   │  ┌─────────────────────────┐    ┌────────────────────────────────┐ │
   │  │  ConversationStore      │    │ _with_workflow_reminder()      │ │
   │  │  per-session lock       │    │ append "[Active workflow]" to  │ │
   │  │  + Message history      │    │ system prompt if any workflow  │ │
   │  └─────────────────────────┘    │ tool holds a live session_id   │ │
   │                                 └────────────────────────────────┘ │
   │                                                                    │
   │  for step in range(max_steps):                                     │
   │      response = await provider.complete/stream(...)                │
   │      if no tool_calls and a workflow is active:                    │
   │          inject auto-recall of the workflow tool (advance step)    │
   │      record assistant turn (text + tool_calls)                     │
   │      if no tool_calls (and no workflow active): break              │
   │      run tool_calls concurrently → tool_result blocks              │
   │      if a human_feedback call ran: surface its question, pause     │
   └───┬────────────────────────────────────────────────────────────┬───┘
       │ tools=registry.all()                                       │ hosted_mcp + skills
       ▼                                                            │ (passed through)
   ┌──────────────────────────────────────────────────┐             │
   │              TOOL REGISTRY                       │             │
   │              (one flat namespace)                │             │
   │                                                  │             │
   │ ┌──────────────────┐  ┌─────────────────────┐    │             │
   │ │ Built-in tools   │  │ Local-MCP tools     │    │             │
   │ │ (default_registry│  │ (LocalMCPManager    │    │             │
   │ │  in agent/tools/ │  │  wraps each MCP     │    │             │
   │ │  builtins/)      │  │  tool as a          │    │             │
   │ │                  │  │  LocalTool named    │    │             │
   │ │ add  now         │  │  "<srv>__<tool>")   │    │             │
   │ │ read/write/edit  │  └──────────┬──────────┘    │             │
   │ │   _file          │             │               │             │
   │ │ list_dir         │             ▼ stdio / http  │             │
   │ │ glob/grep_search │      ┌─────────────────┐    │             │
   │ │ shell_exec       │      │ Local MCP       │    │             │
   │ │ shell_status     │      │ servers         │    │             │
   │ │ shell_stop       │      │ (in-process     │    │             │
   │ │ todo_write       │      │  ClientSession) │    │             │
   │ │ plan_and_confirm │      └─────────────────┘    │             │
   │ │   (y/N gated via │                             │             │
   │ │    _confirm.py)  │  ┌─────────────────────┐    │             │
   │ └──────────────────┘  │ Workflow tools      │    │             │
   │                       │ (one LocalTool per  │    │             │
   │                       │  BotCircuits        │    │             │
   │                       │  workflow record;   │    │             │
   │                       │  see GUARDRAIL box) │    │             │
   │                       └──────────┬──────────┘    │             │
   └─────────────────────────────────││───────────────┘             │
                                     ││                             │
              ┌──────────────────────┘└────────────────────┐        │
              │                                            │        │
              ▼                                            ▼        ▼
   ┌──────────────────────────────────┐    ┌─────────────────────────────────┐
   │       PROVIDER (LLMProvider)     │    │     HOSTED CAPABILITIES         │
   │       provider.complete()        │    │     (provider executes them)    │
   │       provider.stream()          │    │                                 │
   │                                  │    │  hosted_mcp = MCP servers the   │
   │  ┌───────────┐ ┌──────────────┐  │    │   provider runs server-side     │
   │  │ Anthropic │ │ OpenAI       │  │    │                                 │
   │  │ Messages  │ │ Responses    │  │    │  skills =                       │
   │  │  API      │ │  API         │  │    │   Anthropic Skills bundles      │
   │  └───────────┘ └──────────────┘  │    │   OpenAI code_interpreter       │
   │  ┌───────────────────────────┐   │    │   Gemini code_execution         │
   │  │ Gemini generate_content   │   │    │                                 │
   │  └───────────────────────────┘   │    │  (Gemini lacks hosted MCP →     │
   │                                  │    │   auto-promoted to local)       │
   │  Normalizes wire format ↔        │    └─────────────────────────────────┘
   │   Message blocks (text /         │
   │   tool_call / tool_result)       │
   └──────────────────────────────────┘
                  │
                  ▼ HTTPS
        ┌─────────────────────┐
        │  LLM cloud APIs     │
        │  (Anthropic /       │
        │   OpenAI / Gemini)  │
        └─────────────────────┘


   ╔═══════════════════════════════════════════════════════════════════╗
   ║       WORKFLOW TOOL  =  GUARDRAIL  (agent/workflow/)              ║
   ║                                                                   ║
   ║  At startup: register_workflows(registry, provider=..., ...)      ║
   ║    1. Glob .botcircuits/workflows/*.json                          ║
   ║       (or $BOTCIRCUITS_WORKFLOWS_DIR)                             ║
   ║    2. Wrap each record as a LocalTool with closure state          ║
   ║         state = {"session_id": None}                              ║
   ║       handler signature widened: (args, context=None)             ║
   ║    3. Built-in tool names always win on collision (skipped, with  ║
   ║       a yellow warning).                                          ║
   ║                                                                   ║
   ║  Author-time: `botcircuits-cli workflow build --name=<wf>` (or   ║
   ║    the `build_workflow` tool via `/workflow add|edit`).           ║
   ║    Compiles NL `conditions` on agentAction states into:           ║
   ║      • per-condition `expCondition` annotation                    ║
   ║      • `choices[]` (operator + expressionList + next)             ║
   ║      • `flow.variables[]` (variableName, dataType, description)   ║
   ║    Uses the SAME LLM provider/model the agent is configured with. ║
   ║    Writes the built result to `<dir>/.build/<name>.json`; the     ║
   ║    raw source under `<dir>/<name>.json` is the author's editable  ║
   ║    file. Runtime loads only from `.build/`.                       ║
   ║                                                                   ║
   ║  At call time (local.run_workflow → engine.run_flow):             ║
   ║    a. Load the workflow file by id                                ║
   ║    b. Resume from saved session (currentStep, pendingBranch,     ║
   ║       slots) if any                                               ║
   ║    c. RE-ENTRY only, when pendingBranch is set: normalize args    ║
   ║         • Layer B — provider.complete(...) extracts values using  ║
   ║           the variable schema + last_assistant_message; drops     ║
   ║           hallucinations via string-presence check                ║
   ║         • Layer A — coerce to dataType; drop on failure           ║
   ║    d. Merge normalized args into slots                            ║
   ║    e. Executor: if pendingBranch was set, evaluate choices to     ║
   ║       pick next state; otherwise walk from currentStep           ║
   ║    f. On an agentAction step, return immediately:                 ║
   ║         {action, done, conditions, choices, variables, ...}       ║
   ║       If that action has choices, record pendingBranch on the     ║
   ║       saved session so the NEXT re-entry triggers normalization.  ║
   ║    g. Persist the paused session in _SESSIONS[session_id] so the  ║
   ║       next call resumes from currentStep.                        ║
   ║                                                                   ║
   ║  Supported step types only (state.type at top level):             ║
   ║    • start        → no-op                                         ║
   ║    • agentAction  → emit action payload, pause workflow           ║
   ║       (branches via `conditions`/`choices` evaluated on RE-ENTRY) ║
   ║    • question     → like agentAction but tagged kind:"question";   ║
   ║       forces a `human_feedback` call (which pauses the loop)       ║
   ║    `choice` as a state type is NOT supported — branching lives    ║
   ║    inside agentAction/question.                                   ║
   ║                                                                   ║
   ║  Multi-turn — the guardrail:                                      ║
   ║    • Each workflow run emits ONE step per call.                   ║
   ║    • The model acts on the step; it does NOT re-call the tool.    ║
   ║      Once it stops acting (no tool calls left), the AGENT LOOP    ║
   ║      auto-recalls '<name>' to fetch the next step (normalization  ║
   ║      runs on that re-entry).                                      ║
   ║    • A `question` step routes through `human_feedback`, which     ║
   ║      PAUSES the loop: the question is surfaced as the reply and   ║
   ║      the user's next message resumes the run (no auto-recall).    ║
   ║    • While state.session_id is set, agent loop appends            ║
   ║      "[Active workflow] perform only this step; do NOT call       ║
   ║      '<name>' yourself" to the system prompt on every call.       ║
   ║    • Workflow ends → state.session_id = None → reminder stops.    ║
   ║                                                                   ║
   ║  Cost: branching states pay ONE extra LLM call (Layer B) on       ║
   ║  re-entry. Non-branching agentActions and initial calls pay zero. ║
   ║  No LLM/RAG fallback inside the engine, no interruption handling  ║
   ║  — pure in-process beyond the optional normalization round-trip.  ║
   ╚═══════════════════════════════════════════════════════════════════╝
```

Three layers, top to bottom:
1. **Agent** — owns the loop, history, system-prompt augmentation (workflow reminder + persistent memory snapshot), and tool dispatch.
2. **Provider** — translates normalized requests into provider-specific API calls.
3. **MCP / Skills / Workflows / Memory** — capability extensions:
   - **Built-in tools** live in-process and are gated per-call via `_confirm.py`.
   - **Local MCP** servers are run by us and exposed as `LocalTool`s the provider never sees as "MCP."
   - **Hosted MCP / Hosted Skills** are passed through to the provider as native parameters.
   - **Filesystem skills** (§8b) are `SKILL.md` directories auto-discovered from `./skills/` and `./.botcircuits/skills/` and wrapped as `LocalTool`s; their rendered bodies (with live `` !`cmd` `` substitutions) ride the same dispatch path as any other tool.
   - **Workflow tools** are `LocalTool`s wrapping on-disk BotCircuits workflows, driven by the embedded STM engine in [agent/workflow/engine/](../../src/botcircuits/agent/workflow/engine/); their multi-turn state machine + system-prompt reminder forms the **guardrail layer** that constrains the LLM to a workflow-defined path. At runtime only the indexed copies under `.build/` are loaded; raw sources are author-time files.
   - **Persistent memory** (§8a) is a flat-file store under `~/.botcircuits/memories/` rendered into the system prompt at session start; the `memory` tool is how the agent writes back.

The Agent never calls a provider's SDK directly; the Provider never sees Anthropic-specific block types leak across the boundary. This isolation is what makes provider swaps trivial.

---

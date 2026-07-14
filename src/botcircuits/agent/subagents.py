"""Subagents — split work into isolated, parallel sub-loops.

A subagent is a fresh `Agent` with its own context window, spawned for
one bounded subtask. It returns only the answer, not its transcript, so
the parent's window stays clean. Independent subtasks fan out in
parallel and come back in order.

Subagents share the parent's provider and (a filtered view of) its tool
registry, but not its conversation: no workflow tools (a subtask must
not silently advance a workflow), no `human_feedback` /
`plan_and_confirm` (a subagent can't talk to the user), and no
`delegate` / `fan_out` (no recursive spawning).

`delegate_tool(agent)` / `fan_out_tool(agent)` are registered on the
parent by `Agent.start()` (opt out with `enable_subagents=False`).
"""

from __future__ import annotations

import asyncio

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

DELEGATE_TOOL = "delegate"
FAN_OUT_TOOL = "fan_out"

DEFAULT_WORKER_SYSTEM = (
    "You are a focused worker. Do exactly the subtask and answer concisely."
)

#: Tools a subagent must not see (besides workflow tools, filtered by
#: their `_workflow_state` marker).
EXCLUDED_TOOLS = frozenset({
    DELEGATE_TOOL, FAN_OUT_TOOL,          # no recursive spawning
    "human_feedback", "plan_and_confirm",  # a subagent can't talk to the user
    "build_workflow",                      # authoring is the parent's call
})

#: Round-trip bound for one subagent — a subtask, not an open-ended session.
MAX_SUBAGENT_STEPS = 25

#: Parallelism cap for fan_out.
MAX_CONCURRENT = 4


def subagent_registry(parent: ToolRegistry) -> ToolRegistry:
    """The parent's tools, filtered for an isolated worker. Carries the
    parent's permission rules so the approval posture doesn't loosen in
    a subagent."""
    reg = ToolRegistry(permissions=parent.permissions)
    for t in parent.all():
        if t.name in EXCLUDED_TOOLS:
            continue
        if getattr(t, "_workflow_state", None) is not None:
            continue
        reg.register(t)
    return reg


async def run_subagent(
    task: str,
    *,
    provider,
    tools: ToolRegistry | None = None,
    system: str | None = None,
    max_tokens: int = 4096,
) -> str:
    """Run one bounded subtask in a fresh, isolated agent; return only its
    final answer. The provider is shared with the caller (never closed
    here); the conversation store is private and dies with the call."""
    from botcircuits.agent.loop import Agent  # lazy: avoids an import cycle

    sub = Agent(
        provider=provider,
        tools=tools or ToolRegistry(),
        local_skills_paths=[],
        max_tokens=max_tokens,
        max_steps=MAX_SUBAGENT_STEPS,
        enable_workflows=False,
        enable_subagents=False,
    )
    await sub.start()
    reply, _sid = await sub.chat(task, system=system or DEFAULT_WORKER_SYSTEM)
    return reply


async def fan_out(
    tasks: list[str],
    *,
    provider,
    tools: ToolRegistry | None = None,
    max_concurrent: int = MAX_CONCURRENT,
) -> list[str]:
    """Run subtasks in parallel, each in its own isolated subagent.
    Order preserved; one subtask's failure becomes its error string
    instead of killing the batch."""
    if not tasks:
        return []
    sem = asyncio.Semaphore(max(1, max_concurrent))

    async def _one(task: str) -> str:
        async with sem:
            try:
                return await run_subagent(task, provider=provider, tools=tools)
            except Exception as e:  # noqa: BLE001 — isolate per-subtask failure
                return f"error: {type(e).__name__}: {e}"

    return list(await asyncio.gather(*[_one(t) for t in tasks]))


def delegate_tool(agent) -> LocalTool:
    """A tool that lets the main agent hand a self-contained subtask to a
    fresh subagent and get its result. Bound to the live parent so the
    subagent sees the parent's (filtered) tools at call time — MCP tools
    and skills registered on start() included."""

    async def _handler(args: dict) -> str:
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            return "error: `task` (non-empty string) is required"
        return await run_subagent(
            task,
            provider=agent.provider,
            tools=subagent_registry(agent.tools),
            max_tokens=agent.max_tokens,
        )

    return LocalTool(
        name=DELEGATE_TOOL,
        description=(
            "Delegate a self-contained subtask to a fresh subagent and get "
            "back only its result. The subagent has its own clean context "
            "— use this to keep large exploratory work (reading many "
            "files, summarizing, research) out of the main conversation."
        ),
        input_schema={
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "required": ["task"],
        },
        handler=_handler,
    )


def fan_out_tool(agent) -> LocalTool:
    """A tool that lets the model split work into independent subtasks and
    run them in parallel, each in its own isolated subagent. Results come
    back labeled and ordered, so the model can read them as one block."""

    async def _handler(args: dict) -> str:
        tasks = args.get("tasks")
        # The model sometimes passes a JSON string instead of a list;
        # iterating that would spawn one subagent per character.
        if (not isinstance(tasks, list) or not tasks
                or not all(isinstance(t, str) for t in tasks)):
            return "error: `tasks` must be a non-empty list of strings"
        results = await fan_out(
            tasks,
            provider=agent.provider,
            tools=subagent_registry(agent.tools),
        )
        return "\n\n".join(
            f"[subtask {i}] {task}\n{result}"
            for i, (task, result) in enumerate(zip(tasks, results), 1)
        )

    return LocalTool(
        name=FAN_OUT_TOOL,
        description=(
            "Run several independent subtasks in parallel, each in its own "
            "fresh subagent, and get back their labeled results. Use for "
            "work that splits cleanly into pieces that don't depend on "
            "each other."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The independent subtasks to run in parallel.",
                },
            },
            "required": ["tasks"],
        },
        handler=_handler,
    )

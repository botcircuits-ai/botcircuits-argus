"""`plan_and_confirm` — present a build plan + initial TODO list and
gate execution behind a single y/N answer.

The model should call this once, BEFORE running shell commands or
writing files for any user request that involves building or modifying
software. The tool renders the plan + todos and asks the user to
approve. `auto=True` (CLI `--auto`) skips the prompt.

Return shape: `{approved: bool, plan: str, todos: [...]}`. On denial,
the model should stop and ask the user what to change instead of
retrying with a slightly different plan.

This tool seeds the same in-memory store used by `todo_write`, so the
agent can keep updating the list without re-stating it.
"""

from __future__ import annotations

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry
from botcircuits.agent.tools.builtins import _confirm
from botcircuits.agent.tools.builtins.todo_write import STATUSES, _STORE, _render


def plan_and_confirm_tool(*, auto: bool = False) -> LocalTool:
    effective_auto = _confirm.effective_auto(auto)

    async def _handler(args: dict, context: dict | None = None) -> dict:
        plan = args.get("plan")
        todos = args.get("todos", [])
        summary = args.get("summary", "")
        workflow_bg = (context or {}).get("_workflow_bg")

        if not isinstance(plan, str) or not plan.strip():
            return {"error": "`plan` must be a non-empty string"}
        if not isinstance(todos, list):
            return {"error": "`todos` must be a list"}
        cleaned: list[dict] = []
        for i, raw in enumerate(todos):
            if not isinstance(raw, dict):
                return {"error": f"todos[{i}] must be an object"}
            content = raw.get("content")
            status = raw.get("status", "pending")
            if not isinstance(content, str) or not content.strip():
                return {"error": f"todos[{i}].content must be a non-empty string"}
            if status not in STATUSES:
                return {"error": (
                    f"todos[{i}].status must be one of "
                    f"{sorted(STATUSES)}; got {status!r}"
                )}
            cleaned.append({"content": content.strip(), "status": status})

        # Seed the shared store so todo_write picks up here.
        _STORE.clear()
        _STORE.extend(cleaned)

        lines = []
        if summary:
            lines.append(f"summary: {summary}")
        lines.append("plan:")
        for ln in plan.strip().splitlines():
            lines.append(f"  {ln}")
        lines.append(f"todos:  ({len(cleaned)} items)")
        for it in cleaned:
            marker = {"completed": "✓", "in_progress": "●", "pending": "○"}.get(it["status"], "?")
            lines.append(f"  {marker} {it['content']}")

        if effective_auto:
            _confirm.warn("plan_and_confirm approved:", lines)
            _render(cleaned)
            return {"approved": True, "plan": plan, "todos": cleaned}

        approved = await _confirm.confirm(
            "plan_and_confirm proposes:", lines,
            prompt="proceed? [y/N]: ",
            workflow_bg=workflow_bg,
        )
        if approved:
            _render(cleaned)
            return {"approved": True, "plan": plan, "todos": cleaned}

        return {
            "approved": False,
            "message": (
                "User did not approve the plan. Stop and ask what to "
                "change — do not retry with a slightly different plan."
            ),
        }

    gate = (
        "Auto mode: the plan is shown as a warning and considered approved. "
        if effective_auto else
        "The user is prompted y/N. On denial, do not proceed with the work — "
        "ask what should change. "
    )
    return LocalTool(
        name="plan_and_confirm",
        description=(
            "Present a build/change plan to the user and gate execution "
            "behind y/N approval. Call this BEFORE running shell commands "
            "or writing files for any non-trivial software task. " + gate +
            "Returns {approved, plan, todos}; the todos seed the live TODO "
            "list that todo_write keeps updating."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "One-sentence summary of what you intend to build/change.",
                },
                "plan": {
                    "type": "string",
                    "description": "Multi-line plan: design decisions, files to touch, order of work.",
                },
                "todos": {
                    "type": "array",
                    "description": "Initial TODO list seeding `todo_write`. Usually all pending.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": sorted(STATUSES)},
                        },
                        "required": ["content"],
                    },
                },
            },
            "required": ["plan", "todos"],
        },
        handler=_handler,
    )


def register(reg: ToolRegistry, **config) -> None:
    allowed = {"auto"}
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(
            f"plan_and_confirm config has unknown keys: {sorted(unknown)}. "
            f"Allowed: {sorted(allowed)}"
        )
    reg.register(plan_and_confirm_tool(**config))

"""Human-feedback builtin — ask the user a question and pause the loop.

A workflow step of type `question` (or any moment the model decides it
needs the human's input) routes through this tool. Unlike every other
builtin, its job isn't to *do* something and report back — it's to
surface a question to the user and hand control back to them.

The handler itself is a thin echo: it returns the question text tagged
so the agent loop can recognize it. The actual "pause" is enforced in
the loop (`Agent`), which treats a `human_feedback` call as a terminal
turn — it surfaces the question as the assistant's reply and stops,
rather than auto-recalling the workflow. The user's next chat message
becomes the answer and resumes the run.

The tool name uses an underscore (`human_feedback`) so it satisfies
every provider's tool-name regex.
"""

from __future__ import annotations

from botcircuits.agent.tools.registry import LocalTool, ToolRegistry

# Tool name surfaced to the model + matched by the agent loop's pause
# check. Kept here so the loop and the registration can't drift.
HUMAN_FEEDBACK_TOOL = "human_feedback"


def human_feedback_tool() -> LocalTool:
    def _human_feedback(args: dict) -> dict:
        question = (args or {}).get("question") or ""
        question = question.strip() if isinstance(question, str) else str(question)
        # The text is echoed back so the loop can render it as the
        # assistant's reply. `paused` is the flag the loop keys off; the
        # tool result itself never re-enters the model (the turn ends).
        return {"paused": True, "question": question}

    return LocalTool(
        name=HUMAN_FEEDBACK_TOOL,
        description=(
            "Ask the human user a question and wait for their reply. Call "
            "this whenever you need information only the user can provide — "
            "e.g. a workflow step instructs you to collect an input, or you "
            "are otherwise blocked on a decision that is the user's to make. "
            "Pass the exact question to show the user in `question`. The "
            "agent pauses after this call; the user's next message is their "
            "answer."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to show the user verbatim.",
                },
            },
            "required": ["question"],
        },
        handler=_human_feedback,
    )


def register(reg: ToolRegistry, **config) -> None:
    if config:
        raise ValueError(
            f"`{HUMAN_FEEDBACK_TOOL}` tool takes no config; got: {sorted(config)}"
        )
    reg.register(human_feedback_tool())

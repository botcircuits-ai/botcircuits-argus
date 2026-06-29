"""Tool registry and LocalTool dataclass.

A `LocalTool` is a Python-side tool the model can call. The handler
receives the parsed JSON arguments and returns either a string or a
JSON-serializable value. Sync or async handlers are both accepted.

Handlers may optionally accept a second positional `context` argument —
a free-form dict the agent loop fills with surrounding state (e.g.,
the last assistant message text). The registry inspects each handler's
signature and only passes `context` to handlers that accept it, so the
existing 1-arg builtins continue to work unchanged.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Union

from botcircuits.agent.permissions import Decision, PermissionSet
from botcircuits.agent.tools.builtins import _confirm

ToolHandler = Callable[..., Union[Any, Awaitable[Any]]]


@dataclass
class LocalTool:
    """A tool we execute in this process. Handler can be sync or async."""
    name: str
    description: str
    input_schema: dict
    handler: ToolHandler


class ToolRegistry:
    def __init__(self, permissions: PermissionSet | None = None) -> None:
        self._tools: dict[str, LocalTool] = {}
        self.permissions = permissions or PermissionSet()

    def register(self, tool: LocalTool) -> None:
        self._tools[tool.name] = tool

    def has(self, name: str) -> bool:
        return name in self._tools

    def all(self) -> list[LocalTool]:
        return list(self._tools.values())

    async def run(
        self,
        name: str,
        args: dict,
        context: dict | None = None,
    ) -> tuple[str, bool]:
        """Returns (result_text, is_error). Awaits async handlers transparently.

        `context` is an optional dict of surrounding agent-loop state. The
        registry calls the handler with `(args, context)` only when its
        signature actually accepts a second positional/keyword argument;
        legacy 1-arg handlers are called with `(args)` as before.

        `is_error` is True when:
          - the tool is unknown
          - the handler raised
          - the handler returned a dict with an "error" key (validation /
            missing file / etc — every builtin signals failure this way)
          - the handler returned a dict with non-zero "exit_code" (the
            shell_exec convention — the model should see the command
            failed without having to parse the JSON itself)

        Tools that return a string or a non-error dict are reported as
        success. The text payload is unchanged either way, so the model
        always sees the full observation; `is_error` is just the flag
        the provider wire formats carry alongside the result.
        """
        if name not in self._tools:
            return f"Unknown tool: {name}", True

        decision = self.permissions.evaluate(name, args or {})
        if decision == Decision.DENY:
            return (
                f"Permission denied: a deny rule blocks this `{name}` call. "
                "Do not retry with different arguments; ask the user to "
                "adjust permissions if this should be allowed.",
                True,
            )
        if decision == Decision.ASK:
            allowed = await _confirm.confirm(
                f"{name} requires approval (permission rule):",
                [f"args: {json.dumps(args or {}, default=str)}"],
                prompt="allow? [y/N]: ",
            )
            if not allowed:
                return (
                    f"User denied the `{name}` call (permission ask rule). "
                    "Do not retry the same arguments; ask the user what to "
                    "change.",
                    True,
                )

        try:
            handler = self._tools[name].handler
            if _handler_accepts_context(handler):
                call_context = dict(context or {})
                if decision in (Decision.ALLOW, Decision.ASK):
                    # A permission rule already decided (and, for ASK, the
                    # user already confirmed above) — skip the tool's own
                    # y/N gate so the user isn't prompted twice.
                    call_context["permission_preapproved"] = True
                result = handler(args or {}, call_context)
            else:
                result = handler(args or {})
            if inspect.isawaitable(result):
                result = await result
            is_error = _is_error_result(result)
            text = (result if isinstance(result, str)
                    else json.dumps(result, default=str))
            return text, is_error
        except Exception as e:
            return f"Tool '{name}' raised {type(e).__name__}: {e}", True


def _handler_accepts_context(handler: ToolHandler) -> bool:
    """Does this handler take a second arg beyond `args`?

    We inspect the callable's signature once per call (cheap). A handler
    accepts context if it has at least 2 positional-ish parameters, or a
    `context` keyword param, or **kwargs. Anything else gets the legacy
    1-arg invocation so existing builtins keep working.
    """
    try:
        sig = inspect.signature(handler)
    except (TypeError, ValueError):
        return False
    positional_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    positional = [p for p in sig.parameters.values() if p.kind in positional_kinds]
    if len(positional) >= 2:
        return True
    if "context" in sig.parameters:
        return True
    if any(p.kind == inspect.Parameter.VAR_KEYWORD
           for p in sig.parameters.values()):
        return True
    return False


def _is_error_result(result: Any) -> bool:
    """Inspect a handler's return value for failure signals.

    A dict with an "error" key, or with a non-zero "exit_code", is
    treated as an error. The "denied" case (user said no at a y/N gate)
    is NOT an error — denial is a normal observation the model is
    expected to react to without retrying.
    """
    if not isinstance(result, dict):
        return False
    if "error" in result and result["error"]:
        return True
    exit_code = result.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return True
    return False

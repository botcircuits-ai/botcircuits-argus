"""Reusable parsing + framing for the `/workflow add|edit|run` slash command.

The BotCircuits CLI ([botcircuits.cli.commands][]) and external embedders
(notably the Hermes adapter in `botcircuits_hermes`) both need to react
to the same `/workflow` slash syntax. The argument parser, the JSON
validation for `--initial-args`, the model-facing instruction text for
`add`/`edit`, and the source-file lookup for edits should live in one
place so the two callers can't drift.

This module is the single home for that logic. It is **pure**: no I/O,
no printing, no `Agent` / `CLIState` types. Callers receive a typed
`WorkflowCommand` result and decide how to surface errors and how to
inject the composed prompt into their own conversation state.

The CLI shim in `botcircuits.cli.commands` adapts results to ANSI output
and the in-process tool registry; the Hermes adapter does the analogous
adaptation onto Hermes' `process_command` contract.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


WORKFLOW_USAGE = (
    'usage: /workflow add "<prompt>" [--name <workflow-name>]\n'
    '       /workflow add --file <path.md> [--name <workflow-name>]\n'
    '       /workflow edit "<prompt>" --name <workflow-name>\n'
    '       /workflow run --name <workflow-name> [--initial-args \'{"k":"v"}\']'
)


_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


WorkflowKind = Literal["add", "edit", "run", "error"]


@dataclass
class WorkflowCommand:
    """Parsed `/workflow` invocation.

    `kind == "error"` carries a user-facing error message in `error`;
    the caller prints it however its host wants (ANSI in the BotCircuits
    CLI; plain text or a Hermes notification elsewhere). For the
    successful kinds the remaining fields describe the operation:

      add  — `prompt` is the natural-language workflow description.
      edit — `prompt` + `target` (workflow name from `--name`).
      run  — `target` + `initial_args` (parsed JSON object; `{}` when
             `--initial-args` was omitted).
    """

    kind: WorkflowKind
    prompt: str = ""
    target: Optional[str] = None
    initial_args: dict = field(default_factory=dict)
    error: Optional[str] = None
    # When kind == "error", `show_usage` tells the caller whether to
    # print the usage block alongside the error. Some errors (bad JSON
    # in --initial-args) don't benefit from re-showing the full usage.
    show_usage: bool = True


def parse_workflow_command(rest: str) -> WorkflowCommand:
    """Parse the text following `/workflow` into a `WorkflowCommand`.

    `rest` is everything after the literal `/workflow` token, e.g.
    `add "greet the user"` or `run --name loan_triage --initial-args '{}'`.

    Returns `kind == "error"` for any malformed input; the caller is
    responsible for surfacing `error` to the user.
    """
    try:
        tokens = shlex.split(rest)
    except ValueError as e:
        return WorkflowCommand(
            kind="error",
            error=f"could not parse arguments: {e}",
        )

    if not tokens:
        return WorkflowCommand(kind="error", error="missing subcommand")

    sub = tokens[0].lower()
    if sub not in ("add", "edit", "run"):
        return WorkflowCommand(
            kind="error",
            error=f"unknown subcommand: {sub!r}",
        )

    prompt, flags = _split_prompt_and_flags(tokens[1:])

    if sub == "add":
        unexpected = set(flags) - {"--name", "--file"}
        if unexpected:
            return WorkflowCommand(
                kind="error",
                error=(
                    f"[workflow add] unexpected flag(s): "
                    f"{', '.join(sorted(unexpected))}"
                ),
            )

        # --file points at a .md file whose contents become the prompt.
        # It's an alternative to the inline "<prompt>" positional, not a
        # supplement — supplying both is ambiguous, so reject it.
        if "--file" in flags:
            if prompt:
                return WorkflowCommand(
                    kind="error",
                    show_usage=False,
                    error=(
                        "[workflow add] pass either a \"<prompt>\" or "
                        "--file <path>, not both"
                    ),
                )
            content, file_err = _read_prompt_file(flags["--file"])
            if file_err is not None:
                return WorkflowCommand(
                    kind="error", show_usage=False, error=file_err,
                )
            prompt = content

        if not prompt:
            return WorkflowCommand(kind="error", error="missing <prompt>")
        target = flags.get("--name")
        if target is not None:
            target = target.strip()
            if not target:
                return WorkflowCommand(
                    kind="error",
                    error="[workflow add] --name requires a value",
                )
            if not _NAME_RE.match(target):
                return WorkflowCommand(
                    kind="error",
                    show_usage=False,
                    error=(
                        f"[workflow add] --name {target!r} must match "
                        f"{_NAME_RE.pattern!r} (letters, digits, "
                        f"underscore, hyphen)"
                    ),
                )
        return WorkflowCommand(kind="add", prompt=prompt, target=target)

    if sub == "edit":
        unexpected = set(flags) - {"--name"}
        if unexpected:
            return WorkflowCommand(
                kind="error",
                error=(
                    f"[workflow edit] unexpected flag(s): "
                    f"{', '.join(sorted(unexpected))}"
                ),
            )
        target = flags.get("--name")
        if not target:
            return WorkflowCommand(
                kind="error",
                error="[workflow edit] missing --name <workflow-name>",
            )
        if not prompt:
            return WorkflowCommand(
                kind="error",
                error='[workflow edit] missing "<prompt>"',
            )
        return WorkflowCommand(kind="edit", prompt=prompt, target=target)

    # sub == "run"
    unexpected = set(flags) - {"--name", "--initial-args"}
    if unexpected:
        return WorkflowCommand(
            kind="error",
            error=(
                f"[workflow run] unexpected flag(s): "
                f"{', '.join(sorted(unexpected))}"
            ),
        )
    if prompt:
        return WorkflowCommand(
            kind="error",
            error=f"[workflow run] unexpected positional input: {prompt!r}",
        )

    target = flags.get("--name")
    if not target:
        return WorkflowCommand(
            kind="error",
            error="[workflow run] missing --name <workflow-name>",
        )

    raw_args = (flags.get("--initial-args") or "").strip()
    if not raw_args:
        return WorkflowCommand(kind="run", target=target, initial_args={})

    try:
        initial_args = json.loads(raw_args)
    except json.JSONDecodeError as e:
        return WorkflowCommand(
            kind="error",
            show_usage=False,
            error=f"[workflow run] --initial-args is not valid JSON: {e}",
        )
    if not isinstance(initial_args, dict):
        return WorkflowCommand(
            kind="error",
            show_usage=False,
            error=(
                "[workflow run] --initial-args must be a JSON object "
                "(e.g. '{\"foo\":\"bar\"}')"
            ),
        )
    return WorkflowCommand(kind="run", target=target, initial_args=initial_args)


def compose_add_prompt(prompt: str, target: Optional[str] = None) -> str:
    """Return the model-facing instruction for `/workflow add "<prompt>"`.

    Captured as a function (not a constant) so future tweaks land in one
    place and so both the CLI and Hermes adapter inject identical text.

    When `target` is provided (via `--name`), the model must use it
    verbatim as the workflow `name` — that value doubles as the on-disk
    filename (`<name>.json`) and the registered tool name.
    """
    if target:
        name_instruction = (
            f"Call `build_workflow` once with `name` set to {target!r} "
            f"exactly (the user supplied it via --name; this value "
            f"becomes both the filename and the tool name). "
        )
    else:
        name_instruction = (
            f"Call `build_workflow` once with a fresh `name` (slug-safe). "
        )
    return (
        f"Create a NEW workflow. User request: {prompt}\n\n"
        f"{name_instruction}"
        f"If scope or branching is ambiguous, ask one focused round of "
        f"clarifying questions before calling the tool."
    )


def compose_edit_prompt(target: str, prompt: str, source_path: Path) -> str:
    """Return the model-facing instruction for `/workflow edit ...`."""
    return (
        f"Edit the existing workflow `{target}` "
        f"(source file: {source_path}). Read that JSON first with "
        f"`read_file`, then call `build_workflow` ONCE with the same "
        f"`name` to overwrite it — the tool replaces the file whole, so "
        f"include the full `steps` map (modified parts + unchanged "
        f"parts) in your payload.\n\n"
        f"User edit request: {prompt}"
    )


def compose_generate_workflow_system_prompt() -> str:
    """Return the LLM **system prompt** for direct workflow JSON generation.

    Embedders that can't host the full agent loop (e.g. the Hermes
    `/workflow` plugin, which dispatches slash commands outside any
    tool-using agent context) call the LLM directly and feed the result
    into `build_workflow_tool().handler({...})`. The schema description
    here is anchored on the same `build_workflow` LocalTool the
    BotCircuits CLI uses, so the on-disk shape, the agent-facing tool
    description, and this stand-alone prompt cannot drift.
    """
    return (
        "You are a workflow designer. Given a user request, generate a "
        "BotCircuits workflow JSON in the format the `build_workflow` "
        "tool accepts.\n\n"
        "Rules:\n"
        "- Only two step types: \"start\" (entry point, no action) and "
        "\"agentAction\" (the LLM performs settings.action).\n"
        "- The \"start\" step has only \"type\" and \"next\".\n"
        "- Each \"agentAction\" step has \"type\", \"settings.action\" "
        "(natural-language instruction), and a control-flow target: "
        "\"next\" (the default next step) and/or a step-root "
        "\"conditions\" list (branches that may override the default).\n"
        "- Branching uses a step-root \"conditions\" field (sibling of "
        "\"type\", \"next\", and \"settings\" — NOT inside \"settings\"). "
        "Each entry is {\"condition\": \"<natural-language test>\", "
        "\"next\": \"<step_id>\"}. List ONLY real conditions; do NOT "
        "include a literal \"otherwise\" entry.\n"
        "- The fallback (\"otherwise\") branch is expressed as the "
        "step's own \"next\" field at the step root. The engine falls "
        "through to \"next\" automatically when none of the conditions "
        "match. A branching step therefore needs BOTH \"conditions\" "
        "(the real branches) AND \"next\" (the default branch).\n"
        "- A step with no \"next\" and no \"conditions\" is terminal.\n"
        "- The workflow \"name\" must be slug-safe (letters, digits, "
        "underscore, hyphen).\n\n"
        "Respond with ONLY a JSON object (no markdown, no explanation) "
        "with this exact shape:\n"
        "{\n"
        "  \"summary\": \"one sentence describing what this workflow does\",\n"
        "  \"workflow\": {\n"
        "    \"name\": \"slug_safe_name\",\n"
        "    \"description\": \"when-to-use description\",\n"
        "    \"start\": \"start\",\n"
        "    \"steps\": {\n"
        "      \"start\": {\"type\": \"start\", \"next\": \"step_1\"},\n"
        "      \"step_1\": {\n"
        "        \"type\": \"agentAction\",\n"
        "        \"settings\": {\"action\": \"...\"},\n"
        "        \"next\": \"step_3\",\n"
        "        \"conditions\": [\n"
        "          {\"condition\": \"user said yes\", \"next\": \"step_2\"}\n"
        "        ]\n"
        "      },\n"
        "      \"step_2\": {\"type\": \"agentAction\", \"next\": \"step_3\", "
        "\"settings\": {\"action\": \"...\"}},\n"
        "      \"step_3\": {\"type\": \"agentAction\", \"settings\": "
        "{\"action\": \"...\"}}\n"
        "    }\n"
        "  }\n"
        "}\n"
    )


def parse_generated_workflow_json(raw: str) -> dict | str:
    """Parse an LLM response produced from `compose_generate_workflow_system_prompt`.

    Returns the parsed `{"summary": ..., "workflow": {...}}` dict on
    success, or a user-facing error string on failure (so the caller can
    surface it without try/except plumbing — same pattern as
    `locate_workflow_for_edit`).
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        return (
            f"[workflow] LLM returned invalid JSON: {e}\n\n"
            f"Raw output:\n{raw[:500]}"
        )

    if not isinstance(result, dict) or "workflow" not in result:
        return (
            f"[workflow] LLM response missing 'workflow' key.\n\n"
            f"Raw output:\n{raw[:500]}"
        )

    return result


def compose_edit_for_direct_generation(
    target: str, prompt: str, existing_json: str,
) -> str:
    """Return the LLM **user-turn prompt** for a direct-generation edit.

    Used by embedders that bypass the agent loop and call the LLM
    directly with `compose_generate_workflow_system_prompt()` as the
    system prompt. The reply must be the COMPLETE updated workflow JSON
    (the engine's `build_workflow` always overwrites whole).

    Contrast with `compose_edit_prompt()`, which targets the agent loop
    — that one tells the agent to call `read_file` then `build_workflow`
    itself, so the JSON never crosses the prompt.
    """
    return (
        f"Edit the existing workflow below. Keep the same `name` "
        f"({target}).\n"
        f"User edit request: {prompt}\n\n"
        f"Existing workflow JSON:\n{existing_json}\n\n"
        f"Respond with the COMPLETE updated workflow JSON (same format "
        f"as creating a new one)."
    )


@dataclass
class WorkflowStepDirective:
    """LLM-facing framing for a single workflow step result.

    The flow engine produces an `action` string per step; the host then
    has to nudge the LLM to *perform* it instead of paraphrasing. Both
    in-process (`agent/workflow/__init__.py`) and out-of-process (Hermes
    `workflow_tool.py`) tool wrappers compose the same three pieces:

      - `header`: short label naming the workflow (CLI surfaces it;
        Hermes' JSON shape doesn't use it).
      - `body`:   the "execute this step…do not paraphrase" instruction.
      - `footer`: a "call me again" or "this is the final step" line
        depending on `done`.

    Returning the parts (not a pre-rendered string) lets each caller
    glue them into its own shape — plain text for the CLI, a JSON
    payload with `step`/`directive` fields for Hermes. The wording lives
    in exactly one place either way.
    """

    header: str
    body: str
    footer: str

    def as_plain_text(self, action: str) -> str:
        """Render the in-process CLI form: header, body, step, footer.

        The footer is optional (empty for non-terminal action steps now
        that the loop auto-advances) — drop it cleanly when blank so the
        text doesn't end in stray whitespace.
        """
        text = f"{self.header}\n{self.body}\n\nStep: {action}"
        if self.footer:
            text += f"\n\n{self.footer}"
        return text


def render_branch_variable_lines(branch_variables: list[dict]) -> str:
    """Render a pending branch's variables as `- name (type): desc` lines
    for the re-call instruction in the step directive / loop reminder."""
    lines: list[str] = []
    for v in branch_variables:
        name = v.get("variableName")
        if not isinstance(name, str) or not name:
            continue
        dtype = v.get("dataType") or "string"
        desc = v.get("description") or ""
        suffix = f": {desc}" if desc else ""
        lines.append(f"- {name} ({dtype}){suffix}")
    return "\n".join(lines)


def compose_workflow_step_directive(
    wf_name: str, *, done: bool, kind: str | None = None,
    branch_variables: list[dict] | None = None,
) -> WorkflowStepDirective:
    """Return the directive framing for one workflow step.

    `done=True` means the engine paused on the terminal step (final
    `action` to perform, no re-entry expected); `done=False` means there
    are more steps.

    Note on advancing the workflow: for a plain step the directive does
    NOT instruct the model to re-call the workflow tool — the agent loop
    auto-recalls it after the model finishes acting (with slot
    normalization). Two exceptions:

      - `kind == "question"`: the model MUST call `human_feedback` to
        collect the user's reply, and the loop pauses on that call
        rather than auto-advancing.
      - `branch_variables` non-empty (the step branches on those
        variables): the directive asks the model to re-call the
        workflow tool with the observed values once the step is done,
        so the slots ride the main loop's tool call instead of being
        re-derived from the transcript. The loop's empty-args
        auto-recall remains the fallback when the model doesn't.
    """
    header = f"WORKFLOW STEP — '{wf_name}'"
    variable_lines = render_branch_variable_lines(branch_variables or [])
    if kind == "question":
        body = (
            "This step needs input from the user. Call the "
            "'human_feedback' tool with the exact question to ask "
            "(pass it as `question`). Do NOT answer on the user's "
            "behalf and do NOT continue the workflow until they reply."
        )
        footer = (
            "The agent pauses after the 'human_feedback' call; the "
            "user's next message is their answer."
        )
        if variable_lines and not done:
            footer += (
                f"\nAfter the user replies, call '{wf_name}' with the "
                f"answer mapped to these arguments (omit any you don't "
                f"actually have):\n{variable_lines}"
            )
        return WorkflowStepDirective(header=header, body=body, footer=footer)

    body = (
        "Execute the following step using whatever capability fits "
        "(tool call, plain reply, skill, etc.). Do NOT describe the "
        "step as done unless you have actually performed it."
    )
    if done:
        footer = "This is the FINAL step of this workflow."
    elif variable_lines:
        footer = (
            f"When you have FINISHED this step, call '{wf_name}' again, "
            f"passing the values you observed for these arguments — they "
            f"decide the next step. Pass only values you actually "
            f"observed; omit anything you don't have:\n{variable_lines}"
        )
    else:
        footer = ""
    return WorkflowStepDirective(header=header, body=body, footer=footer)


def compose_workflow_empty_action(wf_name: str) -> str:
    """Return the message shown when the engine paused with no `action`.

    A well-formed STM shouldn't reach this branch, but both in-process
    and Hermes tool wrappers need a clear fallback string when it
    happens. Centralized so the two sides can't drift.
    """
    return f"Workflow '{wf_name}' finished with no further actions."


def render_system_notes(notes: list[str]) -> str:
    """Render systemAction audit notes as a block to prepend to a step
    directive. These steps were executed engine-side (no model action
    needed); the block keeps the bookkeeping visible in the transcript so
    later steps (e.g. an emit-result action) can rely on it. Wording is
    centralized here so the in-process and Hermes wrappers can't drift."""
    if not notes:
        return ""
    lines = "\n".join(f"- {n}" for n in notes)
    return (
        "Recorded by the workflow engine (already done — no action "
        f"needed):\n{lines}"
    )


def compose_forced_run_kickoff(target: str, initial_args: dict) -> str:
    """Return the synthetic user "kickoff" message for `/workflow run`.

    `/workflow run` seeds the conversation with a fake user turn + a
    synthetic assistant tool_call + the real tool_result, so the LLM's
    next turn sees the forced workflow start as if it had triggered the
    call itself. This is the user-turn text for that triple.
    """
    return (
        f"[user requested forced workflow run] "
        f"Starting workflow '{target}' with initial args: "
        f"{json.dumps(initial_args)}."
    )


def compose_forced_run_follow_up(target: str) -> str:
    """Return the continuation prompt after a forced workflow run.

    Fed to the agent loop *after* the synthetic kickoff turn so the
    model picks up where the workflow paused.
    """
    return (
        f"The workflow '{target}' was just force-started on your behalf. "
        f"Read the tool_result above and perform the workflow step now — "
        f"call any tool, ask the user a question, or send a plain reply "
        f"as the step requires. Then continue per the workflow's "
        f"instructions."
    )


@dataclass
class WorkflowLookup:
    """Result of locating a workflow source file by name.

    On success, `path` + `record` are populated. On failure, `error`
    carries a user-facing message; the caller chooses how to print it.
    """

    found: bool
    path: Optional[Path] = None
    record: Optional[dict] = None
    error: Optional[str] = None


def locate_workflow_for_edit(target: str) -> WorkflowLookup:
    """Find the workflow source file matching `target`.

    Matches first on filename (`<target>.json`), then on the file's
    `name` field. Returns a structured result instead of printing —
    callers adapt to their own output convention.
    """
    # Local import: the workflow.local module pulls provider code via
    # the workflow runtime; keeping this import lazy means callers that
    # only need parsing don't have to load it.
    from botcircuits.agent.workflow.local import (
        DEFAULT_WORKFLOWS_DIR,
        WORKFLOWS_DIR_ENV,
        _resolve_workflows_dir,
    )

    directory = _resolve_workflows_dir()
    if not directory.is_dir():
        return WorkflowLookup(
            found=False,
            error=(
                f"[workflow edit] workflows directory does not exist: "
                f"{directory}. Set ${WORKFLOWS_DIR_ENV} or create "
                f"{DEFAULT_WORKFLOWS_DIR}/."
            ),
        )

    direct = directory / f"{target}.json"
    if direct.exists():
        record_or_err = _safe_load_json(direct)
        if isinstance(record_or_err, str):
            return WorkflowLookup(found=False, error=record_or_err)
        return WorkflowLookup(found=True, path=direct, record=record_or_err)

    for path in sorted(directory.glob("*.json")):
        record_or_err = _safe_load_json(path)
        if isinstance(record_or_err, str):
            continue
        if record_or_err.get("name") == target:
            return WorkflowLookup(found=True, path=path, record=record_or_err)

    available = sorted(p.stem for p in directory.glob("*.json"))
    suffix = (
        f" Available: {', '.join(available)}." if available
        else " No workflows found in that directory."
    )
    return WorkflowLookup(
        found=False,
        error=f"[workflow edit] no workflow named {target!r}.{suffix}",
    )


def _safe_load_json(path: Path):
    """Return the parsed dict, or an error string for the caller to surface.

    Returning a union rather than raising keeps `locate_workflow_for_edit`
    free of try/except plumbing at each call site.
    """
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return f"[workflow edit] failed to load {path}: {e}"
    if not isinstance(data, dict):
        return f"[workflow edit] {path} is not a JSON object"
    return data


def _read_prompt_file(raw_path: str) -> tuple[str, Optional[str]]:
    """Read a `--file` prompt path, returning `(contents, error)`.

    Used by `/workflow add --file <path.md>`: the file's text replaces
    the inline `"<prompt>"` positional. On success returns the stripped
    contents and `None`; on any failure returns `("", <message>)` so the
    caller can surface it without try/except plumbing (same union style
    as `_safe_load_json`). The `.md` extension is expected but not
    enforced — any UTF-8 text file works.
    """
    raw_path = raw_path.strip()
    if not raw_path:
        return "", "[workflow add] --file requires a path"

    path = Path(raw_path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "", f"[workflow add] --file not found: {path}"
    except OSError as e:
        return "", f"[workflow add] could not read --file {path}: {e}"

    text = text.strip()
    if not text:
        return "", f"[workflow add] --file is empty: {path}"
    return text, None


def _split_prompt_and_flags(
    tokens: list[str],
) -> tuple[str, dict[str, str]]:
    """Split a shlex'd token list into (prompt, flags).

    Positionals concatenate (space-joined) into the prompt; `--flag value`
    pairs go into the dict. A trailing bare `--flag` maps to "".
    """
    positionals: list[str] = []
    flags: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--"):
            if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
                flags[tok] = tokens[i + 1]
                i += 2
            else:
                flags[tok] = ""
                i += 1
        else:
            positionals.append(tok)
            i += 1
    return " ".join(positionals).strip(), flags


__all__ = [
    "WORKFLOW_USAGE",
    "WorkflowCommand",
    "WorkflowKind",
    "WorkflowLookup",
    "WorkflowStepDirective",
    "parse_workflow_command",
    "compose_add_prompt",
    "compose_edit_prompt",
    "compose_edit_for_direct_generation",
    "compose_generate_workflow_system_prompt",
    "compose_workflow_empty_action",
    "compose_workflow_step_directive",
    "render_branch_variable_lines",
    "compose_forced_run_kickoff",
    "compose_forced_run_follow_up",
    "parse_generated_workflow_json",
    "locate_workflow_for_edit",
]

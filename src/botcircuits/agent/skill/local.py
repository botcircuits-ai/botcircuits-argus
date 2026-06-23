"""Local (filesystem) skills — Claude Code / Hermes-style.

A local skill is a directory containing a `SKILL.md` file with YAML-ish
frontmatter and a markdown body. We discover skills from a list of root
directories, parse each `SKILL.md`, and expose each one as a `LocalTool`
the model can call. When the model invokes the tool, the handler renders
the skill (running any `` !`cmd` `` substitutions against the live shell)
and returns the rendered body so the model follows fresh instructions.

Supported frontmatter:

    name                       slug; defaults to the directory name
    description                what the skill does (model-facing trigger)
    allowed-tools              space- or comma-separated list of tool names
                               the skill prefers; surfaced in the rendered
                               body as a hint to the model
    disable-model-invocation   "true" to keep the skill out of the model's
                               tool list (user-only via /skill-name)

Dynamic context: `` !`cmd` `` and fenced ` ```! ` blocks are re-run on
every render so the model always sees current state (git diff, ls, etc.).

Layout::

    skills/
      summarize-changes/
        SKILL.md     # ---\n description: ...\n---\n body...

Discovery roots (in order, first match wins on name collision):

    1. ./skills/
    2. ./.botcircuits/skills/
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from botcircuits.agent.tools.registry import LocalTool

DEFAULT_SKILL_ROOTS: tuple[str, ...] = ("skills", ".botcircuits/skills")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# Inline `!`cmd``: a backtick-wrapped command preceded by `!`, only when
# `!` starts a line or follows whitespace. Mirrors Claude Code's rule —
# `KEY=!`cmd`` is intentionally not substituted.
_INLINE_CMD_RE = re.compile(r"(^|(?<=\s))!`([^`\n]+)`")
# Fenced block opened with ` ```! ` (rest of block is one multi-line cmd).
_FENCED_CMD_RE = re.compile(r"^```!\s*\n(.*?)^```\s*$",
                            re.DOTALL | re.MULTILINE)

# Per-command timeout for shell injection. Stops a runaway command from
# hanging the agent loop forever; 10s is enough for git/ls/curl on a
# normal machine.
_CMD_TIMEOUT_SEC = 10


@dataclass
class LocalSkill:
    """A skill loaded from disk. `body` is the raw markdown after the
    frontmatter — call `render_body()` to apply shell substitutions."""
    name: str
    description: str
    body: str
    path: Path
    allowed_tools: list[str] = field(default_factory=list)
    disable_model_invocation: bool = False


def parse_skill_md(text: str, skill_dir: Path) -> LocalSkill:
    """Parse a SKILL.md into a LocalSkill.

    Frontmatter is a `---`-delimited block of `key: value` pairs at the
    top of the file. Unknown keys are ignored so a future SKILL.md with
    richer metadata still loads. If `name` is absent the directory name
    is used.
    """
    meta, body = _split_frontmatter(text)
    name = (meta.get("name") or skill_dir.name).strip()
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Skill name {name!r} in {skill_dir} must be lowercase "
            f"alphanumeric/hyphens, 1-64 chars"
        )
    description = (meta.get("description") or "").strip()
    if not description:
        for para in body.split("\n\n"):
            para = para.strip()
            if para:
                description = para
                break
    return LocalSkill(
        name=name,
        description=description,
        body=body.strip(),
        path=skill_dir,
        allowed_tools=_parse_list(meta.get("allowed-tools", "")),
        disable_model_invocation=_parse_bool(meta.get("disable-model-invocation")),
    )


def discover_skills(roots: list[Path]) -> list[LocalSkill]:
    """Scan each root for `<root>/<name>/SKILL.md` and load them.

    Earlier roots win on name collisions. Unreadable or malformed
    SKILL.md files are skipped with a stderr warning rather than
    failing the whole load.
    """
    seen: dict[str, LocalSkill] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            skill_md = entry / "SKILL.md"
            if not (entry.is_dir() and skill_md.is_file()):
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
                skill = parse_skill_md(text, entry)
            except (OSError, ValueError) as e:
                print(f"[warn] skipping skill at {skill_md}: {e}",
                      file=sys.stderr)
                continue
            if skill.name in seen:
                continue
            seen[skill.name] = skill
    return list(seen.values())


async def render_body(skill: LocalSkill) -> str:
    """Apply dynamic substitutions to the skill body and return the
    final string the model (or CLI) will see.

    Two substitutions are run in order:

      1. Fenced ` ```! ` blocks → command output, fenced as ` ```text `.
      2. Inline `` !`cmd` `` → command output verbatim.

    Commands run via the shell (so pipes work), in the skill's directory,
    with a 10s timeout. Failures don't raise — the placeholder is
    replaced with an `[error: ...]` marker so the model still sees the
    skill body and can decide what to do.

    An `allowed-tools` hint is appended when the skill declares one.
    """
    body = skill.body

    # Fenced blocks first so an inline `!` inside a fenced command body
    # isn't double-expanded.
    async def fenced_sub(m: re.Match) -> str:
        cmd = m.group(1).strip()
        out = await _run_shell(cmd, cwd=skill.path)
        return f"```text\n{out}\n```"

    body = await _async_sub(_FENCED_CMD_RE, fenced_sub, body)

    async def inline_sub(m: re.Match) -> str:
        prefix, cmd = m.group(1), m.group(2).strip()
        out = await _run_shell(cmd, cwd=skill.path)
        return prefix + out

    body = await _async_sub(_INLINE_CMD_RE, inline_sub, body)

    if skill.allowed_tools:
        body += ("\n\n---\nPreferred tools for this skill: "
                 + ", ".join(f"`{t}`" for t in skill.allowed_tools))
    return body


def skill_to_tool(skill: LocalSkill) -> LocalTool:
    """Wrap a LocalSkill as a LocalTool the model can call.

    The handler renders the skill body on every call so commands
    inside `` !`cmd` `` placeholders run fresh.
    """

    async def _handler(args: dict) -> str:
        return await render_body(skill)

    return LocalTool(
        name=skill.name,
        description=skill.description or f"Skill: {skill.name}",
        input_schema={"type": "object", "properties": {},
                      "additionalProperties": False},
        handler=_handler,
    )


# -- internals ---------------------------------------------------------


async def _run_shell(cmd: str, cwd: Path) -> str:
    """Run a shell command and return stdout+stderr as one string.

    Errors and timeouts return an `[error: ...]` marker instead of
    raising — the skill author would rather the model see *something*
    and proceed than have the whole turn explode because `git` wasn't on
    PATH.
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(cwd),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(),
                                            timeout=_CMD_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            return f"[error: command timed out after {_CMD_TIMEOUT_SEC}s: {cmd}]"
        text = out.decode("utf-8", errors="replace").rstrip()
        return text if text else "[empty output]"
    except Exception as e:
        return f"[error running {cmd!r}: {type(e).__name__}: {e}]"


async def _async_sub(pattern: re.Pattern, repl, text: str) -> str:
    """re.sub but the replacement function is async. We scan the string
    once, collect the matches, await each replacement, then splice."""
    out: list[str] = []
    last = 0
    for m in pattern.finditer(text):
        out.append(text[last:m.start()])
        out.append(await repl(m))
        last = m.end()
    out.append(text[last:])
    return "".join(out)


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return ({key: value}, body). No frontmatter → ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
    if not m:
        return {}, text
    raw_meta, body = m.group(1), m.group(2)
    meta: dict[str, str] = {}
    for line in raw_meta.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, body


def _parse_list(value: str) -> list[str]:
    """Split on commas OR whitespace; matches Claude Code's `allowed-tools`
    accepting either form."""
    if not value:
        return []
    parts = re.split(r"[,\s]+", value.strip())
    return [p for p in parts if p]


def _parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in ("true", "yes", "1", "on")

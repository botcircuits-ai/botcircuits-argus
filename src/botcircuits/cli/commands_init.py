"""`botcircuits init` — scaffold `.botcircuits/settings.json` and install the
workflow skills for the chosen host agent runtime in one step.

  init [--dir DIR] [--runtime RUNTIME] [--force] [--link]
        Create `<DIR>/.botcircuits/settings.json`. Defaults to the current
        working directory. With `--runtime`, seeds the file with that
        `runtime` key (see `botcircuits.runtime.detect` for supported host
        agent runtimes); otherwise writes an empty settings object and skips
        skills install. Use `--force` to overwrite a file that already
        exists, and `--link`
        to symlink rather than copy the skill folders.

This is the non-interactive counterpart to `botcircuits setup` — it just
lays down the file/folder so the project has a settings.json to edit (by
hand or via `setup --project`), without prompting for a provider/model/key.

Runtime → skills agent mapping: a runtime only gets its skills installed
automatically if it has a corresponding entry in `_RUNTIME_TO_SKILLS_AGENT`
(see `commands_skills._AGENT_TARGETS` for the agent's skills directory).
Runtimes without a skills target (e.g. `codex`, `openclaw`) just skip that
step — there's nothing to install into yet.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from botcircuits.cli.ansi import C, out
from botcircuits.cli.commands_skills import _AGENT_TARGETS, _cmd_install
from botcircuits.cli.settings import SETTINGS_DIR, SHARED_FILE
from botcircuits.cli.config import _read_raw
from botcircuits.runtime.detect import CLAUDE_CODE, CODEX, HERMES, OPENCLAW

#: Host agent runtimes selectable via `--runtime`. Excludes `native`/`self`,
#: which aren't host-agent CLIs a project would pin itself to.
RUNTIME_CHOICES = (CLAUDE_CODE, HERMES)

#: Which `commands_skills` agent target each runtime installs skills into.
#: Runtimes absent here (codex, openclaw) have no skills directory yet.
_RUNTIME_TO_SKILLS_AGENT = {
    CLAUDE_CODE: "claude",
    HERMES: "hermes",
}

#: Local project-level config folder per agent (sibling of `.botcircuits/`).
#: When present under the init target dir, skills install there instead of
#: the user-level `~/.claude/skills` or `~/.hermes/skills` default.
_AGENT_LOCAL_DIR = {
    "claude": ".claude",
    "hermes": ".hermes",
}


def _local_skills_target(base: Path, agent: str) -> Path | None:
    """`<base>/.claude` or `<base>/.hermes`, if that folder already exists.

    Lets a project that's already set up a local agent config folder get its
    skills installed alongside it, instead of always falling back to the
    user-level skills dir.
    """
    local_dir = _AGENT_LOCAL_DIR.get(agent)
    if local_dir is None:
        return None
    candidate = base / local_dir
    return (candidate / "skills") if candidate.is_dir() else None


def add_init_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "init",
        help="Create .botcircuits/settings.json and install workflow skills "
             "for the selected runtime.",
    )
    p.add_argument(
        "--dir", default=None,
        help="Folder to create .botcircuits/settings.json under "
             "(default: current working directory).",
    )
    p.add_argument(
        "--runtime", choices=RUNTIME_CHOICES, default=None,
        help="Seed settings.json with this host agent runtime, and install "
             "its workflow skills.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Overwrite settings.json if it already exists.",
    )
    p.add_argument(
        "--link", action="store_true",
        help="Symlink the skill folders instead of copying (live edits; dev).",
    )


def run_init_command(args: argparse.Namespace) -> int:
    base = Path(args.dir).expanduser() if args.dir else Path.cwd()
    settings_path = base / SETTINGS_DIR / SHARED_FILE

    runtime = args.runtime
    if settings_path.exists():
        if not args.force:
            out(C.yellow(f"! {settings_path} already exists. Pass --force to overwrite."))
            return 1
        if runtime is None:
            # Re-running with --force and no --runtime: keep whatever runtime
            # was already pinned, so skills install still has something to key off.
            runtime = _read_raw(str(settings_path)).get("runtime")

    settings: dict = {}
    if runtime:
        settings["runtime"] = runtime

    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    out(C.green(f"✓ Created {settings_path}"))
    if runtime:
        out(C.dim(f"  runtime: {runtime}"))

    agent = _RUNTIME_TO_SKILLS_AGENT.get(runtime)
    if agent is None:
        out(C.dim(f"  (no workflow skills target for runtime '{runtime}' yet — skipping)"))
        return 0

    out("")
    local_target = _local_skills_target(base, agent)
    skills_args = argparse.Namespace(
        target=str(local_target) if local_target else None,
        agent=agent, link=args.link,
    )
    return _cmd_install(skills_args)

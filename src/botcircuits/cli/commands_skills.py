"""`botcircuits skills ...` subcommands — install the workflow skills into a
host agent so it can author/run BotCircuits workflows from natural language.

  skills install [--target DIR] [--link] [--agent claude|hermes]
        Copy (or --link) the `botcircuits-workflow-authoring` and
        `botcircuits-workflow-running` skill folders into a host agent's skills
        directory. Default target: ~/.claude/skills (Claude Code, personal).
        --agent hermes is shorthand for --target ~/.hermes/skills.

This replaces the standalone ``scripts/install-skills.sh`` so everything is
reachable from the one ``botcircuits`` CLI right after install — the host agent
then drives the workflow via the skills, shelling out to ``botcircuits workflow
run`` (which is on PATH after the installer's link step).
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

from botcircuits.cli.ansi import C, out

#: The two skills that teach a host agent to author + run workflows.
_SKILLS = ("botcircuits-workflow-authoring", "botcircuits-workflow-running")

#: Per-agent default skills directories.
_AGENT_TARGETS = {
    "claude": Path.home() / ".claude" / "skills",
    "hermes": Path.home() / ".hermes" / "skills",
}

#: Hermes indexes skills two levels deep (`<category>/<skill-name>/SKILL.md`)
#: and falls back to using the skill's own directory name as a bogus
#: "category" for anything installed flat — which both keeps it out of the
#: `general` bucket the model is told to scan and truncates its description
#: in the rendered index. Claude Code has no such nesting requirement, so
#: only Hermes installs get this extra subdirectory.
_AGENT_CATEGORY = {
    "hermes": "botcircuits",
}


def add_skills_subparser(subparsers: argparse._SubParsersAction) -> None:
    sk = subparsers.add_parser(
        "skills",
        help="Install the BotCircuits workflow skills into a host agent.",
    )
    sk_subs = sk.add_subparsers(dest="skills_cmd", required=True)

    inst = sk_subs.add_parser(
        "install",
        help="Copy (or link) the workflow skills into an agent's skills dir.",
    )
    inst.add_argument(
        "--target", default=None,
        help="Skills directory to install into "
             "(default: ~/.claude/skills, or --agent's dir).",
    )
    inst.add_argument(
        "--agent", choices=sorted(_AGENT_TARGETS), default=None,
        help="Shorthand for that agent's skills dir "
             "(claude → ~/.claude/skills, hermes → ~/.hermes/skills).",
    )
    inst.add_argument(
        "--link", action="store_true",
        help="Symlink the skill folders instead of copying (live edits; dev).",
    )


def run_skills_command(args: argparse.Namespace) -> int:
    if args.skills_cmd == "install":
        return _cmd_install(args)
    out(C.red(f"[skills] unknown subcommand: {args.skills_cmd}"))
    return 2


def _skills_source() -> Path | None:
    """Locate the repo's ``skills/`` directory.

    Honors ``$BOTCIRCUITS_SKILLS_DIR``; otherwise resolves it relative to the
    installed package source (``<repo>/skills``, sibling of ``src/``), which is
    how a git/editable install lays it out. Returns ``None`` if not found.
    """
    env = os.environ.get("BOTCIRCUITS_SKILLS_DIR", "").strip()
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    import botcircuits

    # <repo>/src/botcircuits/__init__.py → <repo>/skills
    repo_root = Path(botcircuits.__file__).resolve().parent.parent.parent
    cand = repo_root / "skills"
    return cand if cand.is_dir() else None


def _cmd_install(args: argparse.Namespace) -> int:
    src_dir = _skills_source()
    if src_dir is None:
        out(C.red("[skills] could not locate the skills source directory."))
        out(C.dim("        set $BOTCIRCUITS_SKILLS_DIR to the repo's skills/ "
                  "folder and retry."))
        return 1

    if args.target:
        target = Path(args.target).expanduser()
    elif args.agent:
        target = _AGENT_TARGETS[args.agent]
    else:
        target = _AGENT_TARGETS["claude"]
    category = _AGENT_CATEGORY.get(args.agent)
    install_dir = (target / category) if category else target
    install_dir.mkdir(parents=True, exist_ok=True)

    for skill in _SKILLS:
        src = src_dir / skill
        dst = install_dir / skill
        if not src.is_dir():
            out(C.red(f"[skills] missing skill source: {src}"))
            return 1
        # Replace any prior install (dir or symlink) so re-running is idempotent.
        if dst.is_symlink() or dst.exists():
            if dst.is_dir() and not dst.is_symlink():
                shutil.rmtree(dst)
            else:
                dst.unlink()
        if args.link:
            dst.symlink_to(src, target_is_directory=True)
            out(C.green(f"✓ linked {skill} → {dst}"))
        else:
            shutil.copytree(src, dst)
            out(C.green(f"✓ installed {skill} → {dst}"))

    out("")
    out(f"{C.bold('Skills installed.')} Try in your agent:")
    out(C.dim('  "create an order fulfillment workflow with ..."'))
    out(C.dim('  "run order fulfillment"'))
    return 0

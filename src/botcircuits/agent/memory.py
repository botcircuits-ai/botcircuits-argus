"""Persistent memory — bounded, curated agent/user notes that survive across sessions.

Modeled after Hermes Agent's persistent memory feature
(https://hermes-agent.nousresearch.com/docs/user-guide/features/memory).

Two flat files under `.botcircuits/memories/` — project-local when the
working directory has a `.botcircuits/` folder (each project keeps its own
notes, same resolution as sessions/workflows/settings), else the global
`~/.botcircuits/memories/`:

  - MEMORY.md  agent's notes about environment, conventions, lessons learned.
               Cap: 2200 chars (~800 tokens).
  - USER.md    user profile: preferences, communication style, expectations.
               Cap: 1375 chars (~500 tokens).

Entries are separated by `§` (section sign) delimiters and may span multiple
lines. Both files are read once at session start and injected into the system
prompt as a frozen snapshot — there is no `read` action because the content is
already in context.

The `memory` tool exposes three actions:

    add      append a new entry to the target file
    replace  substring-match an existing entry and swap in new text
    remove   substring-match an existing entry and drop it

Targets: ``memory`` (-> MEMORY.md) and ``user`` (-> USER.md).

Capacity is enforced on writes: when an `add` or `replace` would exceed the
cap, the call fails with a usage report and asks the agent to consolidate
before retrying. At >=80% the response includes a "consider consolidating"
hint, mirroring Hermes' behavior.

Entries pass a lightweight scrub for prompt-injection / exfiltration markers
and invisible Unicode before being accepted.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Paths & limits
# ---------------------------------------------------------------------------

MEMORY_DIR_ENV = "BOTCIRCUITS_MEMORY_DIR"
DEFAULT_MEMORY_DIRNAME = ".botcircuits/memories"

MEMORY_FILE = "MEMORY.md"
USER_FILE = "USER.md"

# Caps (characters) — taken straight from Hermes' published budget so the
# system prompt overhead stays bounded.
MEMORY_CAP_CHARS = 2200
USER_CAP_CHARS = 1375

# At-or-above this fraction of capacity, the tool nudges the agent to
# consolidate before adding more.
CONSOLIDATE_THRESHOLD = 0.80

# Entry separator. Multi-line entries are fine — the delimiter is on its own line.
DELIMITER = "§"

Target = Literal["memory", "user"]


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

def memory_dir() -> Path:
    """Resolve the on-disk directory for memory files.

    Resolution order (mirrors sessions/workflows/settings):
    1. `BOTCIRCUITS_MEMORY_DIR` — tests and non-default deployments.
    2. `<cwd>/.botcircuits/memories/` — when the project has a local
       `.botcircuits/` folder, its memory lives WITH the project, so
       different projects keep separate notes.
    3. `~/.botcircuits/memories/` — global fallback for cwds without a
       project `.botcircuits/`.

    Resolved on every call (not cached) so an agent started in one
    project and another started elsewhere each hit the right store.
    """
    override = os.getenv(MEMORY_DIR_ENV)
    if override:
        return Path(override).expanduser()
    local = Path.cwd() / DEFAULT_MEMORY_DIRNAME
    if local.parent.is_dir():  # project has a .botcircuits/ folder
        return local
    return Path.home() / DEFAULT_MEMORY_DIRNAME


def _file_for(target: Target) -> Path:
    if target == "memory":
        return memory_dir() / MEMORY_FILE
    if target == "user":
        return memory_dir() / USER_FILE
    raise ValueError(f"unknown memory target: {target!r}")


def _cap_for(target: Target) -> int:
    return MEMORY_CAP_CHARS if target == "memory" else USER_CAP_CHARS


# ---------------------------------------------------------------------------
# Validation — reject prompt-injection patterns and invisible Unicode
# ---------------------------------------------------------------------------

# Conservative set of patterns historically used in injection attempts.
# Matching is case-insensitive on prose; the goal is just to make the user
# notice when memory content looks weaponized.
_INJECTION_PATTERNS = [
    re.compile(r"ignore (all )?(previous|prior) instructions", re.I),
    re.compile(r"disregard (the )?(system|previous) (prompt|instructions)", re.I),
    re.compile(r"</?system>", re.I),
    re.compile(r"<\|im_start\|>|<\|im_end\|>"),
    # Exfiltration / data-leak prompts
    re.compile(r"send (me|this) (the )?(api[_ ]?key|secret|token)", re.I),
    re.compile(r"exfiltrat", re.I),
]


def _has_invisible_unicode(text: str) -> bool:
    """Flag zero-width, bidi-override, and other invisible control chars
    that could hide payload from a human reviewer."""
    for ch in text:
        cat = unicodedata.category(ch)
        # Cf = format control (zero-width joiners, bidi overrides)
        # Cc, except newline/tab/CR which are legitimate in memory content
        if cat == "Cf":
            return True
        if cat == "Cc" and ch not in ("\n", "\t", "\r"):
            return True
    return False


def _scan_for_threats(text: str) -> str | None:
    """Returns a human-readable reason if the text should be rejected,
    else None."""
    if _has_invisible_unicode(text):
        return "entry contains invisible Unicode characters (zero-width / control)"
    for pat in _INJECTION_PATTERNS:
        if pat.search(text):
            return f"entry matches prompt-injection pattern: {pat.pattern!r}"
    return None


# ---------------------------------------------------------------------------
# File IO
# ---------------------------------------------------------------------------

def _read_file(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def _write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _split_entries(content: str) -> list[str]:
    """Split a memory file's text into entries.

    Entries are separated by lines containing only the delimiter. Empty
    leading/trailing entries are dropped so we don't accumulate blank slots
    on every round-trip.
    """
    if not content.strip():
        return []
    raw = re.split(rf"(?m)^{re.escape(DELIMITER)}\s*$", content)
    return [e.strip() for e in raw if e.strip()]


def _join_entries(entries: list[str]) -> str:
    """Render entries back to file text. The trailing newline keeps `cat`
    output tidy and makes diff-ing cleaner."""
    if not entries:
        return ""
    body = f"\n{DELIMITER}\n".join(e.strip() for e in entries)
    return body + "\n"


# ---------------------------------------------------------------------------
# Public read API — used by the system-prompt injector
# ---------------------------------------------------------------------------

@dataclass
class MemorySnapshot:
    """A point-in-time read of both memory files. Built once per session
    start; downstream code never re-reads disk."""
    memory: str  # raw text of MEMORY.md (or "" if missing/empty)
    user: str    # raw text of USER.md  (or "" if missing/empty)

    def is_empty(self) -> bool:
        return not (self.memory.strip() or self.user.strip())


def load_snapshot() -> MemorySnapshot:
    """Read both files. Missing files are treated as empty — first-run
    users have no memory yet, which is fine."""
    return MemorySnapshot(
        memory=_read_file(_file_for("memory")),
        user=_read_file(_file_for("user")),
    )


def render_for_system_prompt(snap: MemorySnapshot) -> str:
    """Format a snapshot for inclusion in the system prompt.

    Wrapped in clearly-labeled fenced sections so the model can tell where
    persistent memory ends and the rest of the prompt begins. Returns ""
    when both files are empty, so callers can `(system or "") + render(...)`
    without producing weird trailing whitespace on first run.
    """
    if snap.is_empty():
        return ""
    parts = ["\n\n[Persistent memory — frozen snapshot loaded at session start]"]
    if snap.user.strip():
        parts.append("<user_profile>\n" + snap.user.strip() + "\n</user_profile>")
    if snap.memory.strip():
        parts.append("<agent_memory>\n" + snap.memory.strip() + "\n</agent_memory>")
    parts.append(
        "Use the `memory` tool (add/replace/remove) to update these files "
        "when you learn durable facts. Do not paraphrase them back to the "
        "user unless they ask."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Mutation API — used by the `memory` tool
# ---------------------------------------------------------------------------

class MemoryError(ValueError):
    """Surface-friendly error raised by add/replace/remove. The tool handler
    catches these and returns them as a normal tool-result error so the
    model can react."""


def _capacity_report(target: Target, content: str) -> dict:
    cap = _cap_for(target)
    used = len(content)
    return {
        "used_chars": used,
        "cap_chars": cap,
        "usage_pct": round(used / cap * 100, 1) if cap else 0,
    }


def _check_cap(target: Target, new_content: str) -> None:
    cap = _cap_for(target)
    if len(new_content) > cap:
        report = _capacity_report(target, new_content)
        raise MemoryError(
            f"{target!r} memory would exceed cap "
            f"({report['used_chars']}/{report['cap_chars']} chars). "
            f"Consolidate or remove an entry before adding new content."
        )


def add_entry(target: Target, text: str) -> dict:
    """Append a new entry to the target file. Returns a status dict."""
    text = (text or "").strip()
    if not text:
        raise MemoryError("entry text must be non-empty")
    threat = _scan_for_threats(text)
    if threat:
        raise MemoryError(f"refusing to store entry: {threat}")

    path = _file_for(target)
    entries = _split_entries(_read_file(path))
    if text in entries:
        # Idempotent — no need to error, but tell the caller we didn't grow.
        return {
            "action": "add",
            "target": target,
            "added": False,
            "reason": "entry already present",
            **_capacity_report(target, _read_file(path)),
        }
    entries.append(text)
    new_content = _join_entries(entries)
    _check_cap(target, new_content)
    _write_file(path, new_content)
    report = _capacity_report(target, new_content)
    hint = (
        "Consider consolidating soon — memory is near capacity."
        if report["usage_pct"] >= CONSOLIDATE_THRESHOLD * 100
        else None
    )
    return {
        "action": "add",
        "target": target,
        "added": True,
        "entry_count": len(entries),
        **report,
        **({"hint": hint} if hint else {}),
    }


def replace_entry(target: Target, old_text: str, new_text: str) -> dict:
    """Find an entry containing `old_text` (substring match) and swap it
    out for `new_text`. Errors when zero or multiple entries match — the
    caller is expected to disambiguate with a longer substring."""
    old_text = (old_text or "").strip()
    new_text = (new_text or "").strip()
    if not old_text:
        raise MemoryError("`old_text` must be non-empty")
    if not new_text:
        raise MemoryError("`new_text` must be non-empty; use remove to delete")
    threat = _scan_for_threats(new_text)
    if threat:
        raise MemoryError(f"refusing to store entry: {threat}")

    path = _file_for(target)
    entries = _split_entries(_read_file(path))
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if not matches:
        raise MemoryError(
            f"no entry in {target!r} contains the substring {old_text!r}"
        )
    if len(matches) > 1:
        raise MemoryError(
            f"{len(matches)} entries in {target!r} contain {old_text!r}; "
            f"pass a longer, unique substring"
        )
    idx = matches[0]
    entries[idx] = new_text
    new_content = _join_entries(entries)
    _check_cap(target, new_content)
    _write_file(path, new_content)
    report = _capacity_report(target, new_content)
    return {
        "action": "replace",
        "target": target,
        "replaced": True,
        "entry_count": len(entries),
        **report,
    }


def remove_entry(target: Target, old_text: str) -> dict:
    """Drop the entry containing `old_text`. Same substring-match rules
    as `replace_entry`."""
    old_text = (old_text or "").strip()
    if not old_text:
        raise MemoryError("`old_text` must be non-empty")

    path = _file_for(target)
    entries = _split_entries(_read_file(path))
    matches = [i for i, e in enumerate(entries) if old_text in e]
    if not matches:
        raise MemoryError(
            f"no entry in {target!r} contains the substring {old_text!r}"
        )
    if len(matches) > 1:
        raise MemoryError(
            f"{len(matches)} entries in {target!r} contain {old_text!r}; "
            f"pass a longer, unique substring"
        )
    idx = matches[0]
    removed = entries.pop(idx)
    new_content = _join_entries(entries)
    _write_file(path, new_content)
    report = _capacity_report(target, new_content)
    return {
        "action": "remove",
        "target": target,
        "removed": True,
        "removed_preview": removed[:120],
        "entry_count": len(entries),
        **report,
    }


def list_entries(target: Target) -> list[str]:
    """Read entries from one target. Used by the /memory slash command."""
    return _split_entries(_read_file(_file_for(target)))

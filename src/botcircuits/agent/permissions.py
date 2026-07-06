"""Fine-grained tool permission rules, modeled on Claude Code's
`permissions.allow` / `permissions.ask` / `permissions.deny` system
(https://code.claude.com/docs/en/permissions).

A rule is written `Tool` or `Tool(specifier)`:

    Read                        matches every read_file call
    shell_exec(npm run *)       matches shell_exec calls whose argv,
                                 joined with spaces, starts with "npm run "
    Read(//private/tmp/**)      matches read_file/list_dir/glob_search/
                                 grep_search calls whose `path` arg falls
                                 under the absolute path /private/tmp
    Edit(./src/**)               matches write_file/edit_file calls whose
                                 `path` falls under <cwd>/src

`PermissionSet.evaluate(tool_name, args)` returns a `Decision`. Rules are
checked in deny -> ask -> allow order; the first match in that order wins
regardless of which list is more specific. If nothing matches, a built-in
read-only shell_exec allowlist (`READ_ONLY_COMMANDS`) is checked next, so
commands like `pwd`/`ls`/`cat` never prompt unless an explicit ask/deny
rule names them. Anything still unmatched falls back to
`Decision.UNSPECIFIED`, meaning "this permission system has no opinion —
let the tool's own gate (e.g. shell_exec's y/N prompt) decide."

Tool name groups, mirroring Claude Code's Bash/Read/Edit/WebFetch split:

    Read  -> read_file, list_dir, glob_search, grep_search   (path arg)
    Edit  -> write_file, edit_file                           (path arg)
    Bash  -> shell_exec                                      (argv arg)

A rule can also be written with the concrete tool name (e.g.
`read_file(...)`, `shell_exec(...)`) instead of the group alias; both
forms are accepted and behave identically.

`Read`/`Edit` deny and ask rules also apply to file-reading shell commands
(`cat`, `head`, `tail`, `less`, `more`) recognized inside `shell_exec`
argv, the same way Claude Code's docs describe for its Bash tool: a
`Read(.env)` deny rule blocks `read_file(path=".env")` AND
`shell_exec(argv=["cat", ".env"])`. This only covers the small set of
recognized commands — it is not a sandbox, and a script that opens files
itself (python, node, ...) is not inspected.
"""

from __future__ import annotations

import enum
import fnmatch
import os
from dataclasses import dataclass, field
from pathlib import Path


class Decision(enum.Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"
    UNSPECIFIED = "unspecified"  # no rule matched


# Tool -> group alias. A rule written for the alias matches any tool in
# the group; a rule written for the concrete name matches only that tool.
_GROUPS: dict[str, str] = {
    "read_file": "Read",
    "list_dir": "Read",
    "glob_search": "Read",
    "grep_search": "Read",
    "write_file": "Edit",
    "edit_file": "Edit",
    "shell_exec": "Bash",
}

_PATH_TOOLS = {"read_file", "write_file", "edit_file", "list_dir", "glob_search", "grep_search"}
_BASH_TOOLS = {"shell_exec"}


# Commands recognized as read-only and run without a confirmation prompt
# in every mode, mirroring Claude Code's built-in read-only set
# (https://code.claude.com/docs/en/permissions#read-only-commands). Matched
# against argv[0] only — these commands can't mutate state regardless of
# their flags/arguments. Not configurable; add an explicit `ask` or `deny`
# rule for one of these names to require a prompt again.
#
# File-dumping commands (FILE_READ_COMMANDS below) are deliberately
# excluded here even though they're read-only, because they can be used to
# read a Read-denied path (e.g. `cat .env`) and must go through the
# Read-rule cross-check instead of a blanket auto-allow.
READ_ONLY_COMMANDS = frozenset({
    "pwd", "ls", "echo", "wc", "which", "stat", "du", "whoami", "date",
})

# Commands whose trailing non-flag arguments are file paths to read.
# Checked against Read deny/ask rules so `Read(.env)` also blocks
# `shell_exec(["cat", ".env"])`. Not an exhaustive shell-safety net — see
# the module docstring.
FILE_READ_COMMANDS = frozenset({"cat", "head", "tail", "less", "more"})


def _is_builtin_read_only(tool_name: str, args: dict) -> bool:
    if tool_name not in _BASH_TOOLS:
        return False
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv:
        return False
    return str(argv[0]) in READ_ONLY_COMMANDS


def _file_read_command_paths(args: dict) -> list[str]:
    """If `args["argv"]` invokes a recognized file-dumping command,
    return its non-flag trailing arguments (candidate file paths).
    Otherwise return []."""
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv:
        return []
    if str(argv[0]) not in FILE_READ_COMMANDS:
        return []
    paths = []
    for tok in argv[1:]:
        tok = str(tok)
        if tok.startswith("-"):
            continue
        paths.append(tok)
    return paths


@dataclass(frozen=True)
class PermissionRule:
    """One parsed `Tool` or `Tool(specifier)` rule."""
    raw: str
    tool: str               # e.g. "Read", "Bash", "shell_exec", "add"
    specifier: str | None   # raw text inside the parens, or None for bare rules

    @classmethod
    def parse(cls, raw: str) -> "PermissionRule":
        text = raw.strip()
        if not text:
            raise ValueError("permission rule must not be empty")
        if "(" in text:
            if not text.endswith(")"):
                raise ValueError(f"malformed permission rule: {raw!r}")
            tool, _, rest = text.partition("(")
            specifier = rest[:-1]
            tool = tool.strip()
            if not tool:
                raise ValueError(f"malformed permission rule: {raw!r}")
            return cls(raw=raw, tool=tool, specifier=specifier)
        return cls(raw=raw, tool=text, specifier=None)

    def matches(self, tool_name: str, args: dict) -> bool:
        if not _tool_name_matches(self.tool, tool_name):
            return False
        if self.specifier is None:
            return True
        if tool_name in _BASH_TOOLS or self.tool == "Bash":
            return _bash_specifier_matches(self.specifier, args)
        if tool_name in _PATH_TOOLS or self.tool in ("Read", "Edit"):
            return _path_specifier_matches(self.specifier, args)
        # Tools outside the known groups (add, now, todo_write, ...): a
        # specifier never matches anything narrower than "all calls" since
        # there's no defined argument to match against.
        return False


def _tool_name_matches(rule_tool: str, tool_name: str) -> bool:
    alias = _GROUPS.get(tool_name)
    if rule_tool == tool_name:
        return True
    if alias is not None and rule_tool == alias:
        return True
    return False


# ---------------------------------------------------------------------------
# Bash-style matching (shell_exec argv)
# ---------------------------------------------------------------------------


def _bash_specifier_matches(specifier: str, args: dict) -> bool:
    argv = args.get("argv")
    if not isinstance(argv, list):
        return False
    command = " ".join(str(a) for a in argv)
    pattern = specifier
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    return _wildcard_match(pattern, command)


def _wildcard_match(pattern: str, command: str) -> bool:
    """Claude-Code-style Bash wildcard match. `*` matches any sequence of
    characters (including spaces) and can appear anywhere. A trailing
    `<prefix> *` enforces a word boundary: `ls *` matches `ls -la` but not
    `lsof`."""
    if pattern == "*" or pattern == "":
        return True
    if "*" not in pattern:
        return command == pattern
    if pattern.endswith(" *"):
        prefix = pattern[:-2]
        if command == prefix:
            return True
        if not command.startswith(prefix + " "):
            return False
        return True
    # General case: translate to a regex, treating "*" as ".*".
    import re
    escaped = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(escaped, command) is not None


# ---------------------------------------------------------------------------
# Path-style matching (Read/Edit tools), gitignore-flavored anchors
# ---------------------------------------------------------------------------


def _path_specifier_matches(specifier: str, args: dict, *, cwd: str | None = None) -> bool:
    target = args.get("path")
    if not isinstance(target, str) or not target:
        return False
    return _path_matches(specifier, target, cwd=cwd)


def _path_matches(specifier: str, target: str, *, cwd: str | None = None) -> bool:
    base = Path(cwd) if cwd else Path.cwd()
    abs_target = _resolve(target, base)

    if specifier.startswith("//"):
        anchor = Path("/")
        pattern = specifier[2:]
        return _glob_match(abs_target, anchor, pattern, anchored=True)
    if specifier.startswith("~/"):
        anchor = Path.home()
        pattern = specifier[2:]
        return _glob_match(abs_target, anchor, pattern, anchored=True)
    if specifier.startswith("/"):
        anchor = base
        pattern = specifier[1:]
        return _glob_match(abs_target, anchor, pattern, anchored=True)
    if specifier.startswith("./"):
        anchor = base
        pattern = specifier[2:]
        return _glob_match(abs_target, anchor, pattern, anchored=True)
    # Bare pattern (e.g. "*.env", ".env"): gitignore semantics — matches
    # at any depth under the anchor (cwd).
    return _glob_match(abs_target, base, specifier, anchored=False)


def _resolve(target: str, base: Path) -> Path:
    p = Path(target).expanduser()
    if not p.is_absolute():
        p = base / p
    return Path(os.path.normpath(str(p)))


def _glob_match(abs_target: Path, anchor: Path, pattern: str, *, anchored: bool) -> bool:
    try:
        rel = abs_target.relative_to(anchor)
    except ValueError:
        return False
    rel_str = rel.as_posix()

    if anchored:
        return _gitignore_glob(rel_str, pattern)
    if "/" not in pattern and "**" not in pattern:
        # Bare filename: match the final path segment at any depth.
        return fnmatch.fnmatch(rel.name, pattern)
    return _gitignore_glob(rel_str, pattern)


def _gitignore_glob(rel_path: str, pattern: str) -> bool:
    """Approximate gitignore glob semantics: `**` crosses directory
    boundaries, `*` matches within one segment, everything else is a
    literal (fnmatch-style `?`/`[...]` also supported)."""
    if pattern in ("", "**"):
        return True
    regex = _translate_gitignore(pattern)
    import re
    return re.fullmatch(regex, rel_path) is not None


def _translate_gitignore(pattern: str) -> str:
    import re
    out = []
    i = 0
    n = len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # "**" - match across segments (including the separators)
                j = i + 2
                if j < n and pattern[j] == "/":
                    j += 1
                out.append(".*")
                i = j
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        out.append(re.escape(c))
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


@dataclass
class PermissionSet:
    allow: list[PermissionRule] = field(default_factory=list)
    ask: list[PermissionRule] = field(default_factory=list)
    deny: list[PermissionRule] = field(default_factory=list)

    @classmethod
    def from_config(cls, raw: dict | None) -> "PermissionSet":
        raw = raw or {}
        return cls(
            allow=[PermissionRule.parse(r) for r in raw.get("allow", [])],
            ask=[PermissionRule.parse(r) for r in raw.get("ask", [])],
            deny=[PermissionRule.parse(r) for r in raw.get("deny", [])],
        )

    def evaluate(self, tool_name: str, args: dict) -> Decision:
        """deny -> ask -> allow, first match wins. Before falling back to
        the built-in read-only command allowlist (shell_exec only), a
        shell_exec call to a recognized file-dumping command (cat, head,
        ...) is cross-checked against the Read deny/ask rules for each
        path argument, so `Read(.env)` also blocks `cat .env`. Anything
        still unmatched is UNSPECIFIED."""
        for rule in self.deny:
            if rule.matches(tool_name, args):
                return Decision.DENY
        for path in _file_read_command_paths(args):
            if any(_path_matches(r.specifier, path) for r in self.deny
                   if _tool_name_matches(r.tool, "read_file") and r.specifier is not None):
                return Decision.DENY

        for rule in self.ask:
            if rule.matches(tool_name, args):
                return Decision.ASK
        for path in _file_read_command_paths(args):
            if any(_path_matches(r.specifier, path) for r in self.ask
                   if _tool_name_matches(r.tool, "read_file") and r.specifier is not None):
                return Decision.ASK

        for rule in self.allow:
            if rule.matches(tool_name, args):
                return Decision.ALLOW
        if _is_builtin_read_only(tool_name, args):
            return Decision.ALLOW
        return Decision.UNSPECIFIED

    def is_empty(self) -> bool:
        return not (self.allow or self.ask or self.deny)

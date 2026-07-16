# Permissions (`agent/permissions.py`)

Fine-grained tool permission rules, modeled on Claude Code's
`permissions.allow` / `ask` / `deny`.

```
 evaluate(tool_name, args)
      │
      ▼
 deny rules   ── match? ──► BLOCKED            (checked first — deny
      │ no                                      always wins)
      ▼
 ask rules    ── match? ──► PROMPT (y/N)
      │ no
      ▼
 allow rules  ── match? ──► RUN
      │ no
      ▼
 read-only shell allowlist (pwd, ls, cat, …) ──► RUN without prompting
      │ no
      ▼
 UNSPECIFIED ──► the tool's own gate decides (e.g. shell_exec's y/N)
```

A rule is `Tool` or `Tool(specifier)`:

```
Read                      every read_file call
shell_exec(npm run *)     argv (space-joined) starts with "npm run "
Read(//private/tmp/**)    path arg under absolute /private/tmp
Edit(./src/**)            write_file/edit_file path under <cwd>/src
```

Tool-name groups mirror Claude Code: `Bash` → shell tools, `Read` →
read/list/glob/grep, `Edit` → write/edit, `WebFetch`/`WebSearch` → web tools.

`PermissionSet.evaluate(tool_name, args)` returns a `Decision`, checked in
**deny → ask → allow** order (first match wins, regardless of specificity).
Unmatched calls fall through to a built-in read-only shell allowlist
(`pwd`, `ls`, `cat`, …never prompt), then to `Decision.UNSPECIFIED` — "no
opinion; let the tool's own gate (e.g. shell_exec's y/N confirm) decide."

Rules come from the layered `settings.json` files and are carried on the
`ToolRegistry`; the registry enforces them on every dispatch, so MCP tools
and skills are gated the same way as builtins.

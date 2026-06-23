# Persistent Memory

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 8a. Persistent Memory (MEMORY.md / USER.md)

[agent/memory.py](../../src/botcircuits/agent/memory.py) + [agent/tools/builtins/memory.py](../../src/botcircuits/agent/tools/builtins/memory.py). Modeled after Hermes Agent's persistent memory feature. Two flat markdown files under `~/.botcircuits/memories/` (overridable via `$BOTCIRCUITS_MEMORY_DIR`):

- `MEMORY.md` — agent's notes about the environment, project conventions, and lessons learned. Cap: **2200 chars** (~800 tokens).
- `USER.md` — user profile: preferences, communication style, role, expectations. Cap: **1375 chars** (~500 tokens).

### 8a.1 Read path — frozen snapshot at session start

`ConversationStore.get_or_create(...)` ([agent/store.py](../../src/botcircuits/agent/store.py)) calls `render_for_system_prompt(load_snapshot())` exactly **once**, at session creation, and appends the result to the system prompt for that conversation. The snapshot is then frozen — mutations made via the `memory` tool mid-session are written to disk but do *not* feed back into the live conversation. Rationale: re-reading on every turn would invalidate the Anthropic prompt cache (snapshot delta = new prefix), and within a single session the model can already see what it just wrote in tool-result blocks. Users get the updated snapshot on the next session.

The rendered block is wrapped in `<user_profile>` and `<agent_memory>` tags so the model can tell where persistent memory ends and the rest of the prompt begins. When both files are empty, `render_for_system_prompt` returns `""` so a first-run user doesn't see weird trailing whitespace.

### 8a.2 Write path — the `memory` tool

The `memory` LocalTool exposes three actions, all targeted at one of `{"memory", "user"}`:

| Action | Args | What it does |
|---|---|---|
| `add` | `target`, `text` | Append a new entry. Idempotent: identical text already present returns `{added: false, reason: "entry already present"}`. |
| `replace` | `target`, `old_text`, `new_text` | Substring-match an existing entry and swap text. Errors when zero or >1 entries match — the model is expected to disambiguate with a longer substring. |
| `remove` | `target`, `old_text` | Substring-match and drop an entry. Same uniqueness rule as `replace`. |

There is intentionally **no** `read` action — content is already in the system prompt. Documenting `read` would just invite the model to burn a tool call on something it already has.

### 8a.3 Storage format

Entries are separated by `§` (section sign) delimiters on their own line, so multi-line entries are first-class. `_split_entries` drops empty leading/trailing entries so blank slots don't accumulate across round-trips. `_join_entries` adds a trailing newline so `cat`-style inspection stays tidy and diffs are clean.

### 8a.4 Capacity enforcement

Every `add`/`replace` runs `_check_cap` against the target's character cap and raises `MemoryError` when the new content would exceed it — the tool returns the error as a normal tool-result so the model can react ("consolidate or remove an entry before adding new content"). At ≥80% capacity the success response includes a soft `"hint": "Consider consolidating soon..."` so the model has runway to cleanup before hitting the hard limit. Caps are characters, not tokens, because we render directly into the prompt as text — the token budget is the user's, not the API's.

### 8a.5 Threat scrub

`_scan_for_threats` rejects:

- **Prompt-injection patterns** — case-insensitive matches against a small allow-list (`ignore previous instructions`, `disregard the system prompt`, `</system>`, `<|im_start|>`/`<|im_end|>`, exfiltration phrasing like "send me the API key"). Conservative — false positives are cheap (the model retries with rephrased text); false negatives mean weaponized text lands in the prompt.
- **Invisible Unicode** — any Cf (format-control) or Cc (other-control) character except `\n` / `\t` / `\r`. Zero-width joiners and bidi-overrides have been used to hide payloads from human reviewers; we'd rather refuse than store them.

The scrub runs on both `add` and `replace.new_text`. `remove` doesn't need it (no new content being written).

### 8a.6 Slash command — `/memory`

`/memory` in [cli/commands.py](../../src/botcircuits/cli/commands.py) prints the on-disk directory plus a per-target summary: entry count, total used/cap chars, and a numbered preview of each entry's first line (truncated to 160 chars). Read-only — mutations go through the tool so the model and human share one code path.

### 8a.7 Why not just put memory in JSON config

JSON config is parameters (provider, model, tool flags) — declarative, versioned alongside the project. Memory is *content* — accumulated across sessions, often personal, and the model writes to it. Mixing the two would mean either JSON the model can't safely edit, or memory the user has to maintain by hand. The split keeps each surface honest.

---

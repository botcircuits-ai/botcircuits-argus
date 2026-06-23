# Filesystem & Hosted Skills

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 8b. Filesystem Skills (Claude-Code-style)

[agent/skill/local.py](../../src/botcircuits/agent/skill/local.py). Distinct from the hosted **`SkillSpec`** below (§9): a *filesystem skill* is a directory containing a `SKILL.md` file with YAML-ish frontmatter and a markdown body. We discover skills from a list of root directories, parse each `SKILL.md`, and expose each one as a `LocalTool` the model can call. When the model invokes the tool, the handler re-renders the body — including any `` !`cmd` `` shell substitutions — and returns the rendered string so the model follows fresh instructions every time.

### 8b.1 Discovery roots

`DEFAULT_SKILL_ROOTS = ("skills", ".botcircuits/skills")`. Earlier roots win on name collisions. The `Agent` constructor takes a `local_skills_paths` kwarg to override the default; passing `[]` disables filesystem skills entirely. Discovery runs inside `Agent.start()`, *after* user tools and MCP tools are merged into the registry, so a skill named `shell_exec` can never shadow the built-in shell tool.

### 8b.2 SKILL.md format

```markdown
---
name: my-skill                 # slug, defaults to directory name
description: what this does    # model-facing trigger
allowed-tools: shell_exec, edit_file   # space- or comma-separated hint list
disable-model-invocation: false        # "true" keeps it out of model's tool list
---

Body markdown the model receives when the skill is invoked.
Current branch: !`git branch --show-current`

```!
git diff --stat
```
```

Frontmatter unknown keys are ignored so future SKILL.md versions with richer metadata still load. Names must match `^[a-z0-9][a-z0-9-]{0,63}$` (Claude-Code's rule). If `description` is absent, the first paragraph of the body is used.

### 8b.3 Dynamic substitutions — `` !`cmd` `` and ```` ```! ```` blocks

`render_body()` runs two substitutions in order:

1. **Fenced ` ```! ` blocks** — the block body is one multi-line shell command; the output replaces the block as ` ```text ... ``` `. Done first so an inline `!` inside a fenced command isn't double-expanded.
2. **Inline `` !`cmd` `` placeholders** — backtick-wrapped command preceded by `!`, only when `!` starts a line or follows whitespace (`KEY=!`cmd`` is intentionally not substituted; matches Claude Code's rule).

Commands run via the shell (so pipes work), in the **skill's directory** (not the agent's cwd), with a per-command **10s timeout**. Failures are non-fatal: a timed-out or erroring command becomes a `[error: ...]` marker inline and the rest of the body still renders. Rationale — if the skill author wrote `!`git diff``  in a project without git, the model should still see the body and decide what to do, not have the whole turn explode.

### 8b.4 Model-invokable vs. user-only skills

`disable-model-invocation: true` keeps the skill loaded but **not** registered as a callable tool — it doesn't appear in the model's tool catalog. The user can still invoke it directly via `/<skill-name>` in the CLI REPL ([cli/commands.py](../../src/botcircuits/cli/commands.py)), which calls the same `render_body()` and prints the result. Useful for "preset prompt" skills that should never be model-triggered (e.g. `/security-review`).

### 8b.5 `allowed-tools` hint

When set, `render_body()` appends a markdown footer to the rendered body: `Preferred tools for this skill: \`shell_exec\`, \`edit_file\``. This is a hint to the model, not an enforced restriction — the agent loop doesn't read it. Enforcing would require per-call tool filtering and tight coupling between skills and the registry; today the skill author's intent is communicated through the model's natural attention to the rendered text.

### 8b.6 Slash commands — `/skills` and `/<skill-name>`

- `/skills` lists every loaded filesystem skill, marking user-only ones with `[user-only]`.
- `/<skill-name>` invokes the skill directly — bypassing the model. The handler is the *same* one the model would have called, so user-invoke and model-invoke produce identical output.

---

## 9. Hosted Skills

The `SkillSpec` (from [agent/skill/spec.py](../../src/botcircuits/agent/skill/spec.py)) carries an Anthropic-style skill id (`xlsx`, `pptx`, `docx`, `pdf`). Each provider interprets it differently:

| Provider   | What `SkillSpec` does                                                                |
|------------|--------------------------------------------------------------------------------------|
| Anthropic  | Adds three beta headers, attaches `code_execution_20250825` tool, sets `container.skills` with the named skills |
| OpenAI     | Ignores `skill_id`. Any non-empty list enables `code_interpreter` with auto container |
| Gemini     | Ignores `skill_id`. Any non-empty list enables `Tool(code_execution=...)`             |

The "skill_id is Anthropic-only" leakage is intentional. Anthropic Skills are real, named, versioned bundles; OpenAI/Gemini just have a generic Python sandbox. Pretending they're equivalent would give callers false confidence. So we ship the abstraction that's honest: *"give me hosted code execution; if you're on Anthropic, here's which skill bundle to load."*

---

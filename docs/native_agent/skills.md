# Skills (`agent/skill/`)

Two independent concepts under one roof.

## Hosted skills — `SkillSpec` (`skill/spec.py`)

A request handed to the LLM provider. On Anthropic, `skill_id` selects a
named bundle (`xlsx`, `pdf`, …); on OpenAI/Gemini the presence of any
`SkillSpec` just enables hosted code execution.

## Filesystem skills — `LocalSkill` (`skill/local.py`)

Claude Code / Hermes-style: a directory containing `SKILL.md` with YAML-ish
frontmatter + a markdown body, discovered from `./skills/` (and other default
roots, overridable via `local_skills_paths`).

Frontmatter:

| Key | Meaning |
|---|---|
| `name` | slug; defaults to the directory name |
| `description` | model-facing trigger — when to invoke |
| `allowed-tools` | tool names the skill prefers (hint in rendered body) |
| `disable-model-invocation` | keep off the model's tool list; user-only via `/skill-name` |

Each skill becomes a `LocalTool`: invoking it renders the body and returns it
so the model follows fresh instructions. `` !`cmd` `` substitutions and
fenced ```` ```! ```` blocks re-run on every render, so the model always sees
current state (git diff, ls, …).

Registration order on `Agent.start()`: user tools, then MCP tools, then
skills — so a skill named `shell` never shadows the real shell tool.

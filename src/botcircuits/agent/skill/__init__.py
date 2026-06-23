"""Skill subpackage — two distinct concepts under one roof.

- `SkillSpec` (from .spec): a hosted-skill request handed to the LLM
  provider. On Anthropic it selects a named bundle (xlsx, pdf, …); on
  OpenAI/Gemini it just toggles hosted code execution.
- `LocalSkill` (from .local): a filesystem skill discovered from
  `./skills/` etc., wrapped as a `LocalTool` the model can call.

The two are independent — a project can use either or both.
"""

from botcircuits.agent.skill.local import (
    DEFAULT_SKILL_ROOTS,
    LocalSkill,
    discover_skills,
    parse_skill_md,
    render_body,
    skill_to_tool,
)
from botcircuits.agent.skill.spec import SkillSpec

__all__ = [
    "DEFAULT_SKILL_ROOTS",
    "LocalSkill",
    "SkillSpec",
    "discover_skills",
    "parse_skill_md",
    "render_body",
    "skill_to_tool",
]

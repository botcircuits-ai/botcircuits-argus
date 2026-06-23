"""SkillSpec dataclass — describes a hosted-skill request.

On Anthropic, `skill_id` selects a named skill (e.g. "xlsx", "pdf").
On OpenAI, the presence of any SkillSpec enables `code_interpreter`;
`skill_id` is ignored. Same idea on Gemini → `code_execution`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SkillSpec:
    """skill_id is Anthropic-specific; OpenAI/Gemini just enable hosted code."""
    skill_id: str = ""
    version: str = "latest"

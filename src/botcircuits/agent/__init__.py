"""Agent loop subpackage.

Public surface for ergonomic imports:

    from botcircuits.agent import Agent, default_registry
"""

from botcircuits.agent.core import Agent
from botcircuits.agent.mcp import MCPServer
from botcircuits.agent.skill import LocalSkill, SkillSpec, discover_skills, parse_skill_md
from botcircuits.agent.store import ConversationStore
from botcircuits.agent.tools import LocalTool, ToolRegistry, default_registry
from botcircuits.agent.workflow import (
    fetch_workflows,
    register_workflows,
    run_workflow,
    workflow_tool,
)

__all__ = [
    "Agent",
    "ConversationStore",
    "LocalSkill",
    "LocalTool",
    "MCPServer",
    "SkillSpec",
    "ToolRegistry",
    "default_registry",
    "discover_skills",
    "fetch_workflows",
    "parse_skill_md",
    "register_workflows",
    "run_workflow",
    "workflow_tool",
]

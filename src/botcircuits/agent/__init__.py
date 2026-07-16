"""The native agent package — harness modules around one drive loop.

Modeled on the Model + Harness + UI framing: the model seam lives in
`botcircuits/providers`, the UI in `botcircuits/cli`, and everything in
here is harness — the loop (`loop`), context extraction (`context`),
event mapping (`events`), segment execution (`segments`), sessions
(`sessions`), persistent memory (`memory`), tools + permissions
(`tools`, `permissions`), skills (`skill`), MCP (`mcp`), the ReAct
fallback (`react`), and the workflow engine (`workflow`).

Public surface for ergonomic imports:

    from botcircuits.agent import Agent, default_registry
"""

from botcircuits.agent.loop import Agent
from botcircuits.agent.mcp import MCPServer
from botcircuits.agent.orchestration import Orchestrator, OrchestratorResult
from botcircuits.agent.subagents import fan_out, run_subagent
from botcircuits.agent.verification import extract_code, run_python
from botcircuits.agent.skill import LocalSkill, SkillSpec, discover_skills, parse_skill_md
from botcircuits.agent.sessions import (
    ConversationStore,
    DurableConversationStore,
    list_saved_sessions,
    search_sessions,
)
from botcircuits.agent.tools import LocalTool, ToolRegistry, default_registry
from botcircuits.agent.workflow import (
    collect_agents_config,
    fetch_workflows,
    register_workflows,
    run_workflow,
    workflow_tool,
)

__all__ = [
    "Agent",
    "ConversationStore",
    "DurableConversationStore",
    "LocalSkill",
    "Orchestrator",
    "OrchestratorResult",
    "LocalTool",
    "MCPServer",
    "SkillSpec",
    "ToolRegistry",
    "collect_agents_config",
    "default_registry",
    "discover_skills",
    "extract_code",
    "fan_out",
    "fetch_workflows",
    "list_saved_sessions",
    "parse_skill_md",
    "register_workflows",
    "run_python",
    "run_subagent",
    "run_workflow",
    "search_sessions",
    "workflow_tool",
]

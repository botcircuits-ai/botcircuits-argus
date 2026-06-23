"""Concrete agent-runtime providers.

  - `native`      — wraps the in-process `agent.core.Agent` loop.
  - `claude_code` — shells out to the host `claude` CLI headlessly.

Both satisfy `runtime.base.AgentRuntimeProvider`; the workflow engine treats
them identically.
"""

"""Concrete agent-runtime providers.

  - `native`      — wraps the in-process `agent.core.Agent` loop.
  - `claude_code` — shells out to the host `claude` CLI headlessly; the
                    reference CLI impl (codex/openclaw reuse it via config).
  - `hermes`      — subclasses `claude_code` to drive `hermes -z` (oneshot),
                    overriding only where usage comes from (session store).

All satisfy `runtime.base.AgentRuntimeProvider`; the workflow engine treats
them identically.
"""

"""`_select_provider`: build a plain runtime for a workflow with no `agents`
map (today's behavior, unchanged), or a `MultiplexRuntime` wired from the
per-agent groups when one is declared."""

import botcircuits.runtime.run_workflow as rw
from botcircuits.runtime.providers.multiplex import MultiplexRuntime


class _FakeProvider:
    def __init__(self, name: str, agents_config=None):
        self.name = name
        self.agents_config = agents_config or {}


def _fake_select_runtime(*, settings=None, name=None, agents_config=None):
    return _FakeProvider(name, agents_config)


def test_no_agents_map_returns_plain_select_runtime(monkeypatch):
    monkeypatch.setattr(rw, "select_runtime", _fake_select_runtime)
    provider = rw._select_provider({}, settings=None, resolved_name="claude-code")
    assert isinstance(provider, _FakeProvider)
    assert provider.name == "claude-code"


def test_agents_map_builds_multiplex_with_grouped_configs(monkeypatch):
    monkeypatch.setattr(rw, "select_runtime", _fake_select_runtime)
    flow = {
        "agents": {
            "researcher": {"runtime": "codex", "model": "o3"},
            "writer": {"model": "claude-haiku-4-5"},  # no runtime -> default
        },
    }
    provider = rw._select_provider(flow, settings=None, resolved_name="claude-code")
    assert isinstance(provider, MultiplexRuntime)

    # "writer" has no explicit runtime, so it's grouped under the default
    # ("claude-code") and its config rides on the DEFAULT instance.
    assert provider.default.name == "claude-code"
    assert provider.default.agents_config == {"writer": {"model": "claude-haiku-4-5"}}

    # "researcher" names "codex" explicitly -> its own instance, separate
    # from the default, carrying only its own agent's config.
    assert provider.agent_runtime == {"researcher": "codex", "writer": "claude-code"}
    codex_instance = provider.by_runtime["codex"]
    assert codex_instance.name == "codex"
    assert codex_instance.agents_config == {"researcher": {"runtime": "codex", "model": "o3"}}
    assert provider.by_runtime["claude-code"] is provider.default


def test_agent_naming_the_default_runtime_shares_default_instance(monkeypatch):
    monkeypatch.setattr(rw, "select_runtime", _fake_select_runtime)
    flow = {"agents": {"writer": {"runtime": "claude-code", "model": "x"}}}
    provider = rw._select_provider(flow, settings=None, resolved_name="claude-code")
    assert isinstance(provider, MultiplexRuntime)
    assert provider.by_runtime["claude-code"] is provider.default
    assert provider.default.agents_config == {"writer": {"runtime": "claude-code", "model": "x"}}

"""MultiplexRuntime: route a segment to the runtime instance its `agent`
is bound to, falling back to the run's default for unpinned steps."""

import asyncio

from botcircuits.agent.workflow.engine.runner import SegmentResult
from botcircuits.runtime.providers.multiplex import MultiplexRuntime


class _FakeRuntime:
    """Minimal AgentRuntimeProvider stand-in that just records which agent
    it was called with, so tests can assert routing without a real CLI."""

    def __init__(self, label: str):
        self.label = label
        self.calls: list[str | None] = []
        self.closed = False

    async def run_segment(self, *, agent=None, **_kw) -> SegmentResult:
        self.calls.append(agent)
        return SegmentResult(text=f"{self.label}:{agent}")

    async def resolve_slots(self, **_kw):
        return {"resolved_by": self.label}

    async def aclose(self) -> None:
        self.closed = True


def _multiplex():
    default = _FakeRuntime("default")
    other = _FakeRuntime("other")
    mux = MultiplexRuntime(
        default=default,
        by_runtime={"default-rt": default, "other-rt": other},
        agent_runtime={"writer": "other-rt"},
    )
    return mux, default, other


def test_unpinned_segment_uses_default():
    mux, default, other = _multiplex()
    res = asyncio.run(mux.run_segment(
        actions=[], branch_variables=[], system_notes=[], slots={},
    ))
    assert res.text == "default:None"
    assert default.calls == [None]
    assert other.calls == []


def test_pinned_agent_routes_to_its_runtime():
    mux, default, other = _multiplex()
    res = asyncio.run(mux.run_segment(
        actions=[], branch_variables=[], system_notes=[], slots={},
        agent="writer",
    ))
    assert res.text == "other:writer"
    assert other.calls == ["writer"]
    assert default.calls == []


def test_unknown_agent_falls_back_to_default():
    mux, default, other = _multiplex()
    res = asyncio.run(mux.run_segment(
        actions=[], branch_variables=[], system_notes=[], slots={},
        agent="ghost",
    ))
    assert res.text == "default:ghost"
    assert default.calls == ["ghost"]


def test_resolve_slots_always_uses_default():
    mux, default, other = _multiplex()
    out = asyncio.run(mux.resolve_slots(
        flow={}, step_id="s1", variables=[], slots={},
    ))
    assert out == {"resolved_by": "default"}


def test_aclose_closes_every_distinct_instance_once():
    default = _FakeRuntime("default")
    mux = MultiplexRuntime(
        default=default,
        # "same-rt" repeats the default instance (an agent explicitly names
        # the run's own runtime) — aclose must not double-close it.
        by_runtime={"same-rt": default},
        agent_runtime={"a": "same-rt"},
    )
    asyncio.run(mux.aclose())
    assert default.closed is True

"""Provider-level real-usage accumulation (`LLMProvider.record_usage`).

The counters live on the provider (not the Agent) so EVERY call counts —
agent-loop turns, workflow Layer-B normalization, and the condition
indexer all funnel through the same provider instance. `input_tokens` is
the TOTAL prompt size; the cache counters break out the portion served
from / written to the prompt cache so cost accounting can apply vendor
cache discounts.
"""

from __future__ import annotations

from botcircuits.providers.base import LLMProvider


class _Stub(LLMProvider):
    async def complete(self, *a, **k):  # pragma: no cover - not exercised
        raise NotImplementedError

    async def stream(self, *a, **k):  # pragma: no cover - not exercised
        raise NotImplementedError
        yield


def test_record_usage_accumulates_per_instance():
    p = _Stub()
    p.record_usage(100, 10)
    p.record_usage(200, 20, cache_read_tokens=150, cache_write_tokens=30)

    assert p.usage_llm_calls == 2
    assert p.usage_input_tokens == 300
    assert p.usage_output_tokens == 30
    assert p.usage_cache_read_tokens == 150
    assert p.usage_cache_write_tokens == 30

    # Class attributes stay pristine — a second instance starts at zero.
    q = _Stub()
    assert q.usage_llm_calls == 0
    assert q.usage_input_tokens == 0
    assert q.usage_cache_read_tokens == 0


def test_record_usage_clamps_garbage():
    p = _Stub()
    p.record_usage(-5, None, cache_read_tokens=-1)
    assert p.usage_llm_calls == 1
    assert p.usage_input_tokens == 0
    assert p.usage_output_tokens == 0
    assert p.usage_cache_read_tokens == 0

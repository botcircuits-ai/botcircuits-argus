# Conversation Store

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 14. Conversation Store

[agent/store.py](../../src/botcircuits/agent/store.py). `ConversationStore` is a `dict[str, Conversation]` plus per-conversation `asyncio.Lock`. That's the entire implementation. Sessions live for the life of the process.

To plug in persistence, subclass:

```python
class RedisStore(ConversationStore):
    def get_or_create(self, session_id, system=None):
        # load from redis if present, else create fresh
        ...
```

The Agent only calls `store.get_or_create(session_id, system)` and `store.reset(session_id)`, so the surface to override is small. The per-conversation lock must remain process-local even with a remote store, since it serializes turns within one process.

---

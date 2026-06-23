# 6. Memory

[← Index](00-index.md)

---

Memory is what the agent carries **across** sessions — facts that should outlive
a single conversation.

## Two kinds

- **MEMORY.md** — general facts the agent has learned and should remember.
- **USER.md** — facts about the user (preferences, role, context).

Both live as plain files under the agent's home directory and are loaded into the
system prompt at the start of a session, so the agent starts already knowing
them.

## How it changes

The agent updates memory through a dedicated **memory tool** with simple actions:

- **add** — record a new fact.
- **replace** — update an existing one.
- **remove** — drop one that's no longer true.

## Safety

Incoming content is scrubbed before it's stored, so memory can't be used to slip
hidden instructions into future sessions.

## In short

Memory is a small, file-based, human-readable store: easy to inspect, easy to
edit, and injected up front so the agent is context-aware from the first message.

Next: [Streaming](08-streaming.md).

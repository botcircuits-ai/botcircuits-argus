# 9. Gateway

[← Index](00-index.md)

---

The gateway exposes the agent beyond the terminal — over HTTP and chat channels.

## HTTP gateway

A thin web service wraps the agent so other systems can talk to it over HTTP:
send a message, get a reply (streamed or whole). This is optional sugar — the
core agent doesn't depend on it.

## Message gateway (chat channels)

The message gateway connects the agent to real messaging channels — WhatsApp,
Slack, and similar. It handles the channel's specifics (identifying the
conversation, structured button payloads, message formatting) and routes each
incoming message into the agent, keyed to the right session.

```
   WhatsApp / Slack / HTTP client
              │
              ▼
        ┌───────────┐
        │  Gateway  │   maps the channel to a session, forwards the message
        └───────────┘
              │
              ▼
          Agent Loop
```

## Why it's separate

Keeping the gateway as an optional layer means the agent stays small and
framework-free. You bring it in only when you need to serve the agent over a
network or a chat platform.

---

That's the high-level tour. For depth on any topic, see the
[Implementation Guide](../implementations/01-overview.md). To write a workflow,
see the [Workflow Authoring Guide](06-workflow-authoring-guide.md).

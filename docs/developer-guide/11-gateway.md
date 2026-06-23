# FastAPI Gateway & Message Gateway

[← Implementation Guide index](../../IMPLEMENTATION.md)

---

## 13. FastAPI Gateway

[gateway/](../../src/botcircuits/gateway/). A thin wrapper:

- One `Agent` is built at startup via FastAPI's `lifespan`, reused for every request.
- `POST /chat` calls `agent.chat()`, returns JSON.
- `POST /chat/stream` calls `agent.chat_stream()` and serializes each `StreamEvent` as a Server-Sent Event.
- `POST /sessions/{id}/reset` drops a session.
- `GET /healthz` is a liveness check.

### 13.1 Sharing config with the CLI
The gateway honors `BOTCIRCUITS_CONFIG` (env var pointing at the same JSON the CLI uses). When set, `mcp_servers` and `tools` apply to the gateway too. Env vars (`LLM_PROVIDER`, `ANTHROPIC_MODEL`, etc.) still win over JSON values for backwards compatibility.

This deliberately reuses [cli/config.py](../../src/botcircuits/cli/config.py) — there's exactly one schema and one resolver. If we ever extract a `botcircuits.config` module the gateway will move with it.

### 13.2 Why SSE, not WebSockets?
For one-way server-to-client streaming, SSE is just HTTP — works through any proxy, browser support is universal, no framing protocol to debug. WebSockets only earn their complexity when you need bidirectional streams (interrupting an in-flight response, mid-turn human input). If you need that later, swap the route; the `Agent.chat_stream` API stays the same.

### 13.3 SSE format
Each event is:
```
event: <name>
data: <json-encoded payload>

```
Trailing blank line is part of the spec. We emit a leading `: ready\n\n` comment so any reverse proxy flushes headers before the first real event arrives. The `X-Accel-Buffering: no` header tells nginx not to coalesce events.

### 13.4 Concurrency model
FastAPI handles many requests in parallel on the same agent. Each session has its own lock, so concurrent requests targeting different sessions truly run in parallel; concurrent requests on the same session serialize. The Agent itself is stateless across sessions, so this scales linearly with sessions.

---

## 13a. Message Gateway

[gateway/messaging.py](../../src/botcircuits/gateway/messaging.py) + [gateway/channels/](../../src/botcircuits/gateway/channels/). The Hermes-style "one process drives every platform" layer that sits on top of the same `Agent` the JSON/SSE routes use.

### 13a.1 Roles
- **`Channel` ABC** ([channels/base.py](../../src/botcircuits/gateway/channels/base.py)) — every adapter exposes `name`, `start()`, `stop()`, `routes() -> APIRouter | None`, and `send(OutboundMessage)`. Inbound is platform-specific (HTTP webhook, scheduler tick); outbound is uniform.
- **`InboundMessage` / `OutboundMessage`** — normalized envelopes. `InboundMessage` carries `channel`, `external_chat_id`, `text`, optional `sender_id`, raw payload, and an optional per-message `system` override (used by cron jobs).
- **`MessageGateway`** ([messaging.py](../../src/botcircuits/gateway/messaging.py)) — owns a `{name: Channel}` registry, drives lifecycles, and implements `handle_inbound(msg)` and `dispatch(msg)`.

### 13a.2 Session keys
`session_key(msg) = f"{msg.channel}:{msg.external_chat_id}"`. Channel-namespacing prevents an `external_chat_id` collision across platforms from accidentally merging two unrelated chats — Slack channel `C0123` and a WhatsApp number that happens to render the same don't share state.

### 13a.3 Inbound flow
1. The channel's FastAPI route validates platform signatures/tokens and converts the body into `InboundMessage` objects.
2. The route calls `gateway.dispatch(msg)` (returns an `asyncio.Task`, route returns 200 immediately).
3. `handle_inbound` runs `agent.chat(text, session_id=key, system=...)`.
4. The reply is delivered through the *same* channel via `Channel.send(...)`.

The 2xx-immediately model matters: Slack retries within ~3s, Meta within ~5s, both with exponential backoff. Holding the connection while the agent thinks would trigger duplicate deliveries.

### 13a.4 The cron channel
Not really a channel — a scheduler dressed up as one so it goes through the same code path. `CronChannel._run` ticks every 60s; for each `CronJob` whose 5-field cron expression matches the current UTC minute, it synthesizes an `InboundMessage(channel="cron", external_chat_id=job.name, text=job.prompt)` and calls `gateway.handle_inbound`.

Two guards on the tick loop:
- `_last_fired_minute` per job — even if a tick overruns (e.g. the agent takes 90s), the next minute boundary only fires once.
- `asyncio.wait_for(self._stop.wait(), timeout=60)` rather than `asyncio.sleep` — lets `stop()` interrupt the wait cleanly during shutdown.

The cron expression engine ([`_cron_matches`](../../src/botcircuits/gateway/channels/cron.py)) is intentionally minimal — `*`, literals, `A-B`, `*/S`, comma lists. Day-of-week accepts both `0` and `7` for Sunday. No timezone field, no `@daily` macros, no day-of-month ⊕ day-of-week disjunction — every job we plan to run today fits inside this grammar.

Each job has its own conversation history (the session key is its name), so a daily summary job builds context over time. `deliver_to_channel` + `deliver_to_chat_id` let a cron job route its reply through a different channel — e.g. "every weekday at 9:00 UTC, ask the agent for a summary and post it to Slack channel C0123".

### 13a.5 Platform specifics worth knowing
- **WhatsApp** uses Meta's two-phase webhook: a GET with `hub.mode=subscribe` and `hub.verify_token` (echo back `hub.challenge`), then POST event payloads. Non-text messages (media, reactions, statuses) are silently dropped — the agent only handles text today.
- **Slack** uses **Socket Mode** (matching the Hermes Agent setup at https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack). On `Channel.start()` we call `auth.test` to cache the bot's own `user_id`, then open a WebSocket via `slack_sdk.socket_mode.aiohttp.SocketModeClient` using the app-level token (`xapp-…`, scope `connections:write`). Every inbound `SocketModeRequest` is ACK'd in `_on_request` *before* event processing so Slack never retries on our slow paths; only `events_api` envelopes carrying `event_callback` are unpacked into messages. We subscribe to the four Hermes-recommended bot events — `message.im`, `message.channels`, `message.groups`, `app_mention` — and filter out subtype messages, `bot_id`-bearing echoes, and our own bot's `user_id` to prevent reply loops. Outbound is `chat.postMessage` via `AsyncWebClient(token=bot_token)`. There is no inbound HTTP route, no signing-secret verification, and no public URL requirement.
- **Generic webhook** is a fallback for "anything else." Inbound is `POST {chat_id, text, sender_id?}` with a `Bearer` token (optional but recommended). Outbound to a configured URL is also optional — when absent, the channel becomes inbound-only and the agent's reply is logged but not sent anywhere, which is fine when a caller polls for replies via `/chat`-style routes or just wants fire-and-forget triggering.

### 13a.6 Configuration plumbing
[gateway/messaging_config.py](../../src/botcircuits/gateway/messaging_config.py) merges two sources: env vars (credentials) and `.botcircuits/messaging.json` (richer config like cron-job lists). A channel registers itself when its required credentials are all present; otherwise it's skipped with an `info` log line and the gateway still starts. Bad JSON or a missing required cron field raises at startup so typos surface there, not at the first cron tick.

### 13a.7 Lifespan integration
`lifespan` in [app.py](../../src/botcircuits/gateway/app.py) is the one place the gateway is built. The agent comes up first (so a channel can call it immediately if a webhook races startup), then channels are registered, their routers are mounted via `app.include_router(...)`, then `gateway.start()` opens HTTP clients and starts the cron loop. Shutdown reverses it: `gateway.stop()` cancels the cron task and closes channel clients before the agent itself is torn down.

---

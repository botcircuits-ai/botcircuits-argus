"""Message gateway: the single background process that ties channels to
the agent.

Mirrors the concept described at
https://hermes-agent.nousresearch.com/docs/user-guide/messaging/ —
one process connects to every configured channel (WhatsApp, Slack,
generic webhooks, cron triggers), maintains per-chat sessions, and
forwards user messages to the agent loop. The agent's reply is delivered
back through the same adapter that received the message.

Per-chat session keys are namespaced as `{channel}:{external_chat_id}`
so the same person on two platforms gets two independent histories.
"""

from __future__ import annotations

import asyncio
import logging

from botcircuits.agent import Agent
from botcircuits.gateway.channels.base import Channel, InboundMessage, OutboundMessage

log = logging.getLogger(__name__)


class MessageGateway:
    """Owns the set of registered channels and routes their traffic
    through a shared `Agent` instance.

    Designed to be created once at app startup and reused for the
    lifetime of the process.
    """

    def __init__(self, agent: Agent, *, default_system: str | None = None) -> None:
        self.agent = agent
        self.default_system = default_system
        self._channels: dict[str, Channel] = {}
        self._started = False

    # -- registration -------------------------------------------------------

    def register(self, channel: Channel) -> None:
        """Add a channel. Must be called before `start()`."""
        if not channel.name:
            raise ValueError("Channel.name must be set before registration")
        if channel.name in self._channels:
            raise ValueError(f"channel {channel.name!r} already registered")
        self._channels[channel.name] = channel

    def channels(self) -> list[Channel]:
        return list(self._channels.values())

    def get(self, name: str) -> Channel:
        try:
            return self._channels[name]
        except KeyError as e:
            raise KeyError(f"no channel named {name!r}") from e

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._started:
            return
        for ch in self._channels.values():
            ch_ref = ch  # capture for closure if we ever go parallel
            await ch_ref.start()
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        for ch in self._channels.values():
            try:
                await ch.stop()
            except Exception:
                log.exception("error stopping channel %s", ch.name)
        self._started = False

    # -- message flow -------------------------------------------------------

    def session_key(self, msg: InboundMessage) -> str:
        """Per-chat session key. Namespacing by channel prevents an ID
        collision across platforms from merging two unrelated chats."""
        return f"{msg.channel}:{msg.external_chat_id}"

    async def handle_inbound(self, msg: InboundMessage) -> OutboundMessage | None:
        """Run a single inbound message through the agent and deliver
        the reply through the originating channel.

        Returns the outbound message (already sent) so callers — webhook
        handlers, the cron loop — can log or further process it. Returns
        `None` when the agent produced no text (empty replies are
        suppressed rather than echoed as blank messages).
        """
        channel = self._channels.get(msg.channel)
        if channel is None:
            raise KeyError(f"inbound from unknown channel {msg.channel!r}")

        sid = self.session_key(msg)
        system = msg.system or self.default_system
        try:
            reply, _ = await self.agent.chat(msg.text, session_id=sid, system=system)
        except Exception:
            log.exception("agent error on %s session=%s", msg.channel, sid)
            raise

        if not reply or not reply.strip():
            return None

        out = OutboundMessage(
            channel=msg.channel,
            external_chat_id=msg.external_chat_id,
            text=reply,
            in_reply_to=msg,
        )
        try:
            await channel.send(out)
        except Exception:
            log.exception("delivery error on %s session=%s", msg.channel, sid)
            raise
        return out

    def dispatch(self, msg: InboundMessage) -> asyncio.Task:
        """Fire-and-forget variant for webhook handlers that must return
        2xx quickly so the platform doesn't retry. The agent call runs
        in the background; errors are logged, not propagated."""

        async def _run() -> None:
            try:
                await self.handle_inbound(msg)
            except Exception:
                # Already logged in handle_inbound; swallow so the task
                # doesn't raise into the event loop's default handler.
                pass

        return asyncio.create_task(_run())

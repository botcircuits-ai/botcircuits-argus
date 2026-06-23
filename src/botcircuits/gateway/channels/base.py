"""Common contracts for messaging channels.

A `Channel` is the plug-in surface for a platform adapter. Adapters are
responsible for two flows:

  1. Inbound — they translate a platform-native payload (Slack event,
     WhatsApp webhook, cron tick, generic webhook POST) into an
     `InboundMessage` and hand it to the `MessageGateway`.
  2. Outbound — they accept an `OutboundMessage` (the agent's reply)
     and deliver it back to the originating chat/peer on the platform.

The gateway is unaware of platform specifics; it only knows how to call
`channel.send(...)` on whichever adapter produced the inbound message.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ChannelError(RuntimeError):
    """Raised when an adapter cannot deliver or accept a message."""


@dataclass
class InboundMessage:
    """A normalized representation of a message arriving from a platform.

    `channel` identifies the adapter (e.g. "whatsapp", "slack"). The
    pair `(channel, external_chat_id)` is treated as the session key so
    one user on Slack and the same user on WhatsApp get independent
    conversations.
    """

    channel: str
    external_chat_id: str
    text: str
    sender_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    # Per-message overrides; the gateway falls back to its defaults.
    system: str | None = None


@dataclass
class OutboundMessage:
    """A reply destined for a specific chat on a specific channel.

    Built by the gateway after the agent produces a reply. Adapters
    consume this in `Channel.send(...)`.
    """

    channel: str
    external_chat_id: str
    text: str
    in_reply_to: InboundMessage | None = None


class Channel(ABC):
    """Base class every platform adapter must implement.

    Lifecycle (managed by `MessageGateway`):
      - `start()` — optional async init (open HTTP clients, register
        background tasks, etc.).
      - `stop()` — optional async cleanup.
      - `send(msg)` — required: deliver an outbound reply.

    Adapters that receive inbound traffic via HTTP webhooks expose a
    FastAPI router through `routes()` so the gateway can mount it at the
    right prefix.
    """

    #: Stable identifier matching `InboundMessage.channel`.
    name: str = ""

    async def start(self) -> None:
        """Default no-op. Override to spin up background workers."""
        return None

    async def stop(self) -> None:
        """Default no-op. Override to release resources."""
        return None

    def routes(self):  # -> APIRouter | None
        """Return a FastAPI `APIRouter` to mount, or None if the channel
        does not accept inbound HTTP traffic (e.g. pure outbound or
        polling adapters)."""
        return None

    @abstractmethod
    async def send(self, msg: OutboundMessage) -> None:
        """Deliver `msg` to its platform-specific destination."""
        raise NotImplementedError

"""Slack channel — Socket Mode (WebSocket transport).

Mirrors the Hermes Agent approach
(https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack):
the gateway opens an outbound WebSocket to Slack instead of accepting
inbound HTTPS webhooks, so it can run behind a firewall / on a laptop
with no public URL.

Inbound: a background `SocketModeClient` (from `slack_sdk`) receives
events over the socket. We subscribe to the same bot events Hermes
recommends: `message.im`, `message.channels`, `message.groups`, and
`app_mention`. Each user message is normalized into an `InboundMessage`
and handed to the gateway.

Outbound: replies are POSTed to `chat.postMessage` with the bot token,
exactly as with the Events API — outbound is HTTPS either way.

Required configuration (env vars under "Settings"):
  - SLACK_BOT_TOKEN          xoxb-… token for chat.postMessage
  - SLACK_APP_TOKEN          xapp-… app-level token with
                             `connections:write` scope, used to open
                             the WebSocket
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from botcircuits.gateway.channels.base import Channel, ChannelError, InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from ..messaging import MessageGateway

log = logging.getLogger(__name__)


@dataclass
class SlackSettings:
    """Credentials for Socket Mode.

    `bot_token`   — `xoxb-…`, scopes: chat:write, channels:history,
                    groups:history, app_mentions:read, im:history,
                    im:write, users:read (see README).
    `app_token`   — `xapp-…` with `connections:write`. Used only to open
                    the WebSocket; never sent on outbound API calls.
    """

    bot_token: str
    app_token: str


class SlackChannel(Channel):
    name = "slack"

    def __init__(self, settings: SlackSettings, gateway: "MessageGateway") -> None:
        self.settings = settings
        self.gateway = gateway
        self._web: AsyncWebClient | None = None
        self._socket: SocketModeClient | None = None
        self._bot_user_id: str | None = None

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Open the Socket Mode WebSocket and register our event listener.

        We also cache the bot's own `user_id` via `auth.test` so we can
        ignore messages the bot itself posted (otherwise replying to a
        channel would loop).
        """
        self._web = AsyncWebClient(token=self.settings.bot_token)
        try:
            auth = await self._web.auth_test()
            self._bot_user_id = auth.get("user_id")
        except Exception:
            log.exception("slack auth.test failed; bot self-message filtering disabled")

        self._socket = SocketModeClient(
            app_token=self.settings.app_token,
            web_client=self._web,
        )
        self._socket.socket_mode_request_listeners.append(self._on_request)
        await self._socket.connect()
        log.info(
            "slack socket mode connected (bot_user_id=%s)", self._bot_user_id,
        )

    async def stop(self) -> None:
        if self._socket is not None:
            try:
                await self._socket.disconnect()
                await self._socket.close()
            except Exception:
                log.exception("error closing slack socket")
            self._socket = None
        # AsyncWebClient owns an aiohttp session; close it.
        if self._web is not None:
            try:
                await self._web.close()
            except Exception:
                # Older slack_sdk versions don't expose close(); ignore.
                pass
            self._web = None

    # Socket Mode is inbound-over-WebSocket, not HTTP — no router.
    def routes(self):  # type: ignore[override]
        return None

    # -- inbound ------------------------------------------------------------

    async def _on_request(
        self, client: SocketModeClient, req: SocketModeRequest,
    ) -> None:
        """Slack pushes one of these per event over the socket. We must
        ACK every request promptly; if processing the event raises, we
        still want the ACK sent so Slack doesn't retry."""
        try:
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id)
            )
        except Exception:
            log.exception("failed to ack slack socket request")

        if req.type != "events_api":
            # Slash commands / interactivity payloads not handled yet.
            return

        payload = req.payload or {}
        if payload.get("type") != "event_callback":
            return
        event = payload.get("event", {}) or {}
        inbound = _parse_event(event, self._bot_user_id)
        if inbound is not None:
            self.gateway.dispatch(inbound)

    # -- outbound -----------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        if self._web is None:
            raise ChannelError("slack channel not started")
        resp = await self._web.chat_postMessage(
            channel=msg.external_chat_id,
            text=msg.text,
        )
        if not resp.get("ok", False):
            raise ChannelError(f"slack send failed: {resp.data}")


def _parse_event(event: dict[str, Any], bot_user_id: str | None) -> InboundMessage | None:
    """Convert a Slack `event` into an `InboundMessage` if it's a plain
    user message or `app_mention`. Filters out:
      - non-message / non-mention event types
      - message subtypes (edits, channel joins, file shares, …)
      - bot echoes (including our own bot)
      - empty bodies
    """
    etype = event.get("type")
    if etype not in ("message", "app_mention"):
        return None
    if event.get("subtype"):
        return None
    if event.get("bot_id"):
        return None
    if bot_user_id and event.get("user") == bot_user_id:
        return None

    text = event.get("text") or ""
    channel = event.get("channel") or ""
    user = event.get("user")
    if not text or not channel:
        return None
    return InboundMessage(
        channel=SlackChannel.name,
        external_chat_id=channel,
        text=text,
        sender_id=user,
        raw=event,
    )

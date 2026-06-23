"""WhatsApp channel — Meta WhatsApp Cloud API.

Inbound: Meta POSTs message events to a webhook URL. The same URL must
respond to a GET verification handshake when the webhook is first
configured in the Meta dashboard.

Outbound: replies are POSTed to
`https://graph.facebook.com/v20.0/{phone_number_id}/messages` using a
bearer access token tied to the WhatsApp Business app.

Required configuration (env vars listed under "Settings", below):
  - WHATSAPP_PHONE_NUMBER_ID    target phone-number id
  - WHATSAPP_ACCESS_TOKEN       Graph API token
  - WHATSAPP_VERIFY_TOKEN       arbitrary shared string used during the
                                Meta webhook verification GET
  - WHATSAPP_GRAPH_VERSION      optional; defaults to "v20.0"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from botcircuits.gateway.channels.base import Channel, ChannelError, InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from ..messaging import MessageGateway

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com"


@dataclass
class WhatsAppSettings:
    phone_number_id: str
    access_token: str
    verify_token: str
    graph_version: str = "v20.0"


class WhatsAppChannel(Channel):
    name = "whatsapp"

    def __init__(self, settings: WhatsAppSettings, gateway: "MessageGateway") -> None:
        self.settings = settings
        self.gateway = gateway
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=20.0)

    async def stop(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- inbound ------------------------------------------------------------

    def routes(self) -> APIRouter:
        """Mount point: `/messaging/whatsapp` (GET verify + POST events)."""
        router = APIRouter(prefix="/messaging/whatsapp", tags=["whatsapp"])

        @router.get("")
        async def verify(
            hub_mode: str = Query(alias="hub.mode", default=""),
            hub_challenge: str = Query(alias="hub.challenge", default=""),
            hub_verify_token: str = Query(alias="hub.verify_token", default=""),
        ):
            # Meta's webhook handshake: echo back `hub.challenge` iff the
            # verify token matches the one configured in the dashboard.
            if hub_mode == "subscribe" and hub_verify_token == self.settings.verify_token:
                return int(hub_challenge) if hub_challenge.isdigit() else hub_challenge
            raise HTTPException(status_code=403, detail="verify token mismatch")

        @router.post("")
        async def receive(request: Request) -> dict[str, Any]:
            body = await request.json()
            for inbound in _parse_messages(body):
                # Fire-and-forget so we ACK Meta within their 5s window;
                # the agent reply is delivered when ready.
                self.gateway.dispatch(inbound)
            return {"ok": True}

        return router

    # -- outbound -----------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        if self._client is None:
            raise ChannelError("whatsapp channel not started")
        url = (
            f"{GRAPH_BASE}/{self.settings.graph_version}/"
            f"{self.settings.phone_number_id}/messages"
        )
        payload = {
            "messaging_product": "whatsapp",
            "to": msg.external_chat_id,
            "type": "text",
            "text": {"body": msg.text[:4096]},
        }
        headers = {"Authorization": f"Bearer {self.settings.access_token}"}
        resp = await self._client.post(url, json=payload, headers=headers)
        if resp.status_code >= 300:
            raise ChannelError(
                f"whatsapp send failed: {resp.status_code} {resp.text}"
            )


def _parse_messages(body: dict[str, Any]) -> list[InboundMessage]:
    """Walk the nested Meta webhook envelope and yield one
    `InboundMessage` per text message we can route. Non-text messages
    (media, status updates, reactions) are skipped — the agent only
    handles text today."""
    out: list[InboundMessage] = []
    for entry in body.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value", {}) or {}
            for m in value.get("messages", []) or []:
                if m.get("type") != "text":
                    continue
                text = (m.get("text") or {}).get("body") or ""
                sender = m.get("from")  # E.164, no leading '+'
                if not text or not sender:
                    continue
                out.append(InboundMessage(
                    channel=WhatsAppChannel.name,
                    external_chat_id=sender,
                    text=text,
                    sender_id=sender,
                    raw=m,
                ))
    return out

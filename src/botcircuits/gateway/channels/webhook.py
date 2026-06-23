"""Generic webhook channel.

Inbound: clients POST `{"chat_id": "<id>", "text": "<message>"}` (with
an optional `sender_id`) to `/messaging/webhook`. Authentication is a
shared bearer token — the same value must appear in the `Authorization:
Bearer …` header. The chat_id is opaque to the gateway; it just becomes
the session key, so callers control conversation grouping.

Outbound: each reply is POSTed to the configured `outbound_url` with the
shape `{"chat_id", "text", "in_reply_to"}`. The outbound POST is
authenticated with the same token (mirrored back so the receiver can
verify) when a token is configured.

Required configuration (env vars under "Settings"):
  - WEBHOOK_OUTBOUND_URL     where replies are POSTed
  - WEBHOOK_TOKEN            shared bearer token, optional
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from fastapi import APIRouter, Header, HTTPException, Request

from botcircuits.gateway.channels.base import Channel, ChannelError, InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from ..messaging import MessageGateway

log = logging.getLogger(__name__)


@dataclass
class WebhookSettings:
    outbound_url: str | None = None
    token: str | None = None


class WebhookChannel(Channel):
    name = "webhook"

    def __init__(self, settings: WebhookSettings, gateway: "MessageGateway") -> None:
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
        router = APIRouter(prefix="/messaging/webhook", tags=["webhook"])

        @router.post("")
        async def receive(
            request: Request,
            authorization: str | None = Header(default=None),
        ) -> dict[str, bool]:
            self._check_token(authorization)
            body = await request.json()
            chat_id = (body.get("chat_id") or "").strip()
            text = (body.get("text") or "").strip()
            if not chat_id or not text:
                raise HTTPException(
                    status_code=400,
                    detail="webhook payload must include non-empty `chat_id` and `text`",
                )
            inbound = InboundMessage(
                channel=WebhookChannel.name,
                external_chat_id=chat_id,
                text=text,
                sender_id=body.get("sender_id"),
                raw=body,
            )
            self.gateway.dispatch(inbound)
            return {"ok": True}

        return router

    def _check_token(self, authorization: str | None) -> None:
        if not self.settings.token:
            return  # unauthenticated mode (dev only)
        expected = f"Bearer {self.settings.token}"
        if authorization != expected:
            raise HTTPException(status_code=401, detail="invalid webhook token")

    # -- outbound -----------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        # No outbound URL? Then this channel is inbound-only; log and drop.
        # That's a legitimate setup for callers that read replies via the
        # SSE endpoint or via the OutboundMessage returned by handle_inbound.
        if not self.settings.outbound_url:
            log.info(
                "webhook channel: no outbound_url configured; "
                "dropping reply to chat_id=%s", msg.external_chat_id,
            )
            return
        if self._client is None:
            raise ChannelError("webhook channel not started")

        headers: dict[str, str] = {}
        if self.settings.token:
            headers["Authorization"] = f"Bearer {self.settings.token}"
        resp = await self._client.post(
            self.settings.outbound_url,
            json={
                "chat_id": msg.external_chat_id,
                "text": msg.text,
                "in_reply_to": (msg.in_reply_to.raw if msg.in_reply_to else None),
            },
            headers=headers,
        )
        if resp.status_code >= 300:
            raise ChannelError(
                f"webhook send failed: {resp.status_code} {resp.text}"
            )

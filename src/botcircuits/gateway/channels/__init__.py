"""Messaging gateway channels.

Each channel adapts a third-party platform (WhatsApp, Slack, generic
webhooks, cron-scheduled triggers) into a uniform interface so the
`MessageGateway` can route inbound messages into the agent and the
agent's reply back out to the originating platform.
"""

from botcircuits.gateway.channels.base import (
    Channel,
    ChannelError,
    InboundMessage,
    OutboundMessage,
)
from botcircuits.gateway.channels.cron import CronChannel, CronJob
from botcircuits.gateway.channels.slack import SlackChannel
from botcircuits.gateway.channels.webhook import WebhookChannel
from botcircuits.gateway.channels.whatsapp import WhatsAppChannel

__all__ = [
    "Channel",
    "ChannelError",
    "CronChannel",
    "CronJob",
    "InboundMessage",
    "OutboundMessage",
    "SlackChannel",
    "WebhookChannel",
    "WhatsAppChannel",
]

"""Loader for messaging-gateway configuration.

Two sources, merged in this order (later wins):
  1. Environment variables (set in `.env` or the process env).
  2. `.botcircuits/messaging.json` (project) — for richer config like
     cron jobs that would be awkward in env vars.

The JSON shape:

    {
      "whatsapp": {"enabled": true},
      "slack":    {"enabled": true},
      "webhook":  {"enabled": true, "outbound_url": "https://…"},
      "cron": {
        "enabled": true,
        "jobs": [
          {
            "name": "daily-standup",
            "prompt": "Summarize yesterday's PRs and post a standup.",
            "schedule": "0 9 * * 1-5",
            "deliver_to_channel": "slack",
            "deliver_to_chat_id": "C0123456789"
          }
        ]
      }
    }

Channels with `enabled: false` (or missing credentials) are skipped at
startup with a log line — the gateway still starts, it just doesn't
register that channel.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from botcircuits.gateway.channels import CronJob
from botcircuits.gateway.channels.slack import SlackSettings
from botcircuits.gateway.channels.webhook import WebhookSettings
from botcircuits.gateway.channels.whatsapp import WhatsAppSettings

log = logging.getLogger(__name__)


@dataclass
class MessagingConfig:
    whatsapp: WhatsAppSettings | None = None
    slack: SlackSettings | None = None
    webhook: WebhookSettings | None = None
    cron_jobs: list[CronJob] = field(default_factory=list)


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").lower() in ("1", "true", "yes", "on")


def _read_json() -> dict[str, Any]:
    """Read `.botcircuits/messaging.json` from the current working
    directory. Missing file is fine — return {}. Invalid JSON raises
    so a typo isn't silently ignored at startup."""
    path = Path.cwd() / ".botcircuits" / "messaging.json"
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a JSON object")
    return data


def load() -> MessagingConfig:
    """Build a `MessagingConfig` from env + messaging.json."""
    raw = _read_json()
    cfg = MessagingConfig()

    # --- WhatsApp ---
    wa_block = raw.get("whatsapp", {}) or {}
    wa_enabled = _truthy(wa_block.get("enabled", True))
    if wa_enabled:
        phone = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
        token = os.getenv("WHATSAPP_ACCESS_TOKEN")
        verify = os.getenv("WHATSAPP_VERIFY_TOKEN")
        if phone and token and verify:
            cfg.whatsapp = WhatsAppSettings(
                phone_number_id=phone,
                access_token=token,
                verify_token=verify,
                graph_version=os.getenv("WHATSAPP_GRAPH_VERSION", "v20.0"),
            )
        else:
            log.info(
                "whatsapp channel skipped: WHATSAPP_PHONE_NUMBER_ID / "
                "WHATSAPP_ACCESS_TOKEN / WHATSAPP_VERIFY_TOKEN not all set"
            )

    # --- Slack ---
    slack_block = raw.get("slack", {}) or {}
    if _truthy(slack_block.get("enabled", True)):
        bot = os.getenv("SLACK_BOT_TOKEN")
        app_token = os.getenv("SLACK_APP_TOKEN")
        if bot and app_token:
            cfg.slack = SlackSettings(bot_token=bot, app_token=app_token)
        else:
            log.info(
                "slack channel skipped: SLACK_BOT_TOKEN / "
                "SLACK_APP_TOKEN not set (Socket Mode requires both)"
            )

    # --- Generic webhook ---
    wh_block = raw.get("webhook", {}) or {}
    if _truthy(wh_block.get("enabled", True)):
        cfg.webhook = WebhookSettings(
            outbound_url=(
                wh_block.get("outbound_url")
                or os.getenv("WEBHOOK_OUTBOUND_URL")
            ),
            token=os.getenv("WEBHOOK_TOKEN") or wh_block.get("token"),
        )

    # --- Cron jobs ---
    cron_block = raw.get("cron", {}) or {}
    if _truthy(cron_block.get("enabled", True)):
        jobs_raw = cron_block.get("jobs", []) or []
        if not isinstance(jobs_raw, list):
            raise ValueError("cron.jobs must be a JSON array")
        for i, entry in enumerate(jobs_raw):
            if not isinstance(entry, dict):
                raise ValueError(f"cron.jobs[{i}] must be an object")
            try:
                cfg.cron_jobs.append(CronJob(
                    name=entry["name"],
                    prompt=entry["prompt"],
                    schedule=entry["schedule"],
                    deliver_to_channel=entry.get("deliver_to_channel"),
                    deliver_to_chat_id=entry.get("deliver_to_chat_id"),
                    system=entry.get("system"),
                ))
            except KeyError as e:
                raise ValueError(
                    f"cron.jobs[{i}] missing required key {e.args[0]!r}"
                ) from None

    return cfg

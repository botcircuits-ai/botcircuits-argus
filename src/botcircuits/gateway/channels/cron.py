"""Cron scheduler channel.

A pseudo-channel: nothing arrives from a remote platform. Instead, the
channel runs a background task that ticks every 60 seconds and, for any
job whose schedule is due, synthesizes an `InboundMessage` whose
`external_chat_id` is the job name.

That message flows through `MessageGateway.handle_inbound` exactly like
a Slack or WhatsApp message — the agent treats it as a user prompt and
its reply lands back on the cron channel. By default the reply is just
logged; jobs that need to *deliver* their result somewhere should set
`deliver_to_channel` so the reply is forwarded to a different channel
(e.g. Slack).

Schedules use standard 5-field cron expressions evaluated in UTC:

    * * * * *        — every minute (lower bound: we tick every 60s)
    */15 * * * *     — every 15 minutes
    0 9 * * 1-5      — 9am UTC Monday–Friday
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from botcircuits.gateway.channels.base import Channel, InboundMessage, OutboundMessage

if TYPE_CHECKING:
    from ..messaging import MessageGateway

log = logging.getLogger(__name__)

TICK_SECONDS = 60


@dataclass
class CronJob:
    """A scheduled prompt.

    `name` is used as both the job identifier and the agent's session
    key, so each job keeps its own conversation history across firings.

    `prompt` is the text fed to the agent as if a user typed it.

    `schedule` is a 5-field cron expression (UTC). See module docstring.

    `deliver_to_channel` / `deliver_to_chat_id`: when both are set the
    agent's reply is sent through that channel instead of being silently
    consumed. Useful for "every morning post the daily summary to
    Slack #ops".
    """

    name: str
    prompt: str
    schedule: str
    deliver_to_channel: str | None = None
    deliver_to_chat_id: str | None = None
    system: str | None = None
    _last_fired_minute: datetime | None = field(default=None, init=False, repr=False)


class CronChannel(Channel):
    name = "cron"

    def __init__(self, jobs: list[CronJob], gateway: "MessageGateway") -> None:
        self.jobs = list(jobs)
        self.gateway = gateway
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="cron-scheduler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None

    # The cron channel never sends "outbound" to itself — its outputs
    # are either logged or forwarded to another channel inside `_fire`.
    async def send(self, msg: OutboundMessage) -> None:
        log.info("cron job %s replied: %s", msg.external_chat_id, msg.text[:200])

    # -- scheduler loop -----------------------------------------------------

    async def _run(self) -> None:
        """Tick every 60 seconds, fire any due jobs."""
        log.info("cron scheduler started with %d job(s)", len(self.jobs))
        while not self._stop.is_set():
            now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            for job in self.jobs:
                if job._last_fired_minute == now:
                    continue  # already handled this minute
                if _cron_matches(job.schedule, now):
                    job._last_fired_minute = now
                    asyncio.create_task(self._fire(job, now))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                continue

    async def _fire(self, job: CronJob, fired_at: datetime) -> None:
        log.info("cron firing job=%s at=%s", job.name, fired_at.isoformat())
        inbound = InboundMessage(
            channel=CronChannel.name,
            external_chat_id=job.name,
            text=job.prompt,
            sender_id="cron",
            raw={"job": job.name, "fired_at": fired_at.isoformat(),
                 "schedule": job.schedule},
            system=job.system,
        )
        try:
            out = await self.gateway.handle_inbound(inbound)
        except Exception:
            log.exception("cron job %s failed", job.name)
            return

        # Optional fan-out: deliver the reply to another channel.
        if out is None:
            return
        if job.deliver_to_channel and job.deliver_to_chat_id:
            try:
                target = self.gateway.get(job.deliver_to_channel)
            except KeyError:
                log.warning(
                    "cron job %s wants delivery to unknown channel %r; skipping",
                    job.name, job.deliver_to_channel,
                )
                return
            await target.send(OutboundMessage(
                channel=job.deliver_to_channel,
                external_chat_id=job.deliver_to_chat_id,
                text=out.text,
                in_reply_to=inbound,
            ))


# ---------------------------------------------------------------------------
# Minimal 5-field cron matcher (UTC). Supports:
#   *         any value
#   N         a literal value
#   A-B       inclusive range
#   */S       step from the field's minimum
#   A-B/S     step within a range
#   comma-separated lists of any of the above (e.g. "0,15,30,45")
# Day-of-week: 0 or 7 == Sunday.
# ---------------------------------------------------------------------------


_FIELD_BOUNDS = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (after normalizing 7 -> 0)
]


def _cron_matches(expr: str, when: datetime) -> bool:
    """True iff `when` (UTC, second=0) matches the cron `expr`."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(
            f"cron schedule must have 5 fields, got {len(parts)}: {expr!r}"
        )
    dow = when.weekday()  # Mon=0..Sun=6
    # Convert Python's Mon=0..Sun=6 to cron's Sun=0..Sat=6.
    cron_dow = (dow + 1) % 7
    values = [when.minute, when.hour, when.day, when.month, cron_dow]
    return all(
        _field_matches(field_expr, v, lo, hi)
        for field_expr, v, (lo, hi) in zip(parts, values, _FIELD_BOUNDS)
    )


def _field_matches(field_expr: str, value: int, lo: int, hi: int) -> bool:
    for term in field_expr.split(","):
        if _term_matches(term, value, lo, hi):
            return True
    return False


def _term_matches(term: str, value: int, lo: int, hi: int) -> bool:
    step = 1
    if "/" in term:
        base, step_str = term.split("/", 1)
        step = int(step_str)
        if step <= 0:
            raise ValueError(f"cron step must be > 0: {term!r}")
    else:
        base = term

    if base == "*":
        start, end = lo, hi
    elif "-" in base:
        a, b = base.split("-", 1)
        start, end = int(a), int(b)
    else:
        # Single literal — must also be reachable from `lo` by `step`.
        n = int(base)
        if n == 7 and lo == 0 and hi == 6:
            n = 0  # day-of-week: 7 == Sunday
        return value == n

    if value < start or value > end:
        return False
    return (value - start) % step == 0

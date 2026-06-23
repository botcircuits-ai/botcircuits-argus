#!/usr/bin/env python3
"""Deterministic per-item carrier lookup for the `shipment_tracking` workflow.

The workflow's `listDecision` step runs this once per tracking number (the
engine's `itemFacts` exec path — no LLM). It queries the mock carrier API and
prints a single JSON object of facts on stdout, which the step's `derive` map
turns into the per-item variables its branches test.

Usage:
    track.py [--host <api-host>] [--threshold <days>] <tracking_number>

    --host       carrier API host (default http://localhost:4000/v1)
    --threshold  "delayed" threshold in days (default 7); an in-transit parcel
                 whose ETA is more than this many days away needs attention.

Output JSON fields:
    tracking_number, lookup_failed, not_found, status, last_location,
    estimated_delivery, days_until_delivery, is_delayed, needs_attention, note

This never raises: any connection/HTTP/parse failure becomes
`lookup_failed: true` so the batch keeps going (that item → outcome "error").
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

DEFAULT_HOST = "http://localhost:4000/v1"
DEFAULT_THRESHOLD_DAYS = 7
HTTP_TIMEOUT_S = 10

ESCALATE_STATUSES = {"exception", "returned", "lost"}


def _days_until(eta: str) -> int | None:
    """Whole days from today until the ISO date `eta` (None if unparseable)."""
    if not eta:
        return None
    try:
        y, m, d = (int(part) for part in eta.split("-")[:3])
        return (date(y, m, d) - date.today()).days
    except (ValueError, TypeError):
        return None


def _note(status: str, last_location: str, eta: str) -> str:
    where = f" — last seen {last_location}" if last_location else ""
    when = f"; ETA {eta}" if eta else ""
    return f"{status}{where}{when}".strip()


def lookup(host: str, number: str, threshold: int) -> dict:
    facts: dict = {
        "tracking_number": number,
        "lookup_failed": False,
        "not_found": False,
        "status": "",
        "last_location": "",
        "estimated_delivery": "",
        "days_until_delivery": 0,
        "is_delayed": False,
        "needs_attention": False,
        "note": "",
    }

    url = f"{host.rstrip('/')}/track?number={urllib.parse.quote(number)}"
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_S) as resp:
            code = resp.getcode()
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        code = exc.code
        try:
            body = exc.read().decode("utf-8")
        except Exception:
            body = ""
    except Exception:
        facts["lookup_failed"] = True
        facts["note"] = "Carrier lookup failed (connection/timeout)."
        return facts

    # Carrier explicitly says the number is unknown.
    if code == 404:
        facts["not_found"] = True
        facts["note"] = "Tracking number not recognized by carrier."
        return facts

    # Any other non-200, or an empty body, is an upstream failure.
    if code != 200 or not body.strip():
        facts["lookup_failed"] = True
        facts["note"] = f"Carrier lookup failed (HTTP {code})."
        return facts

    try:
        data = json.loads(body)
    except Exception:
        facts["lookup_failed"] = True
        facts["note"] = "Carrier returned an unreadable response."
        return facts

    status = str(data.get("status") or "").strip()
    last_location = str(data.get("last_location") or "")
    eta = str(data.get("estimated_delivery") or "")
    days = _days_until(eta)

    facts["status"] = status
    facts["last_location"] = last_location
    facts["estimated_delivery"] = eta
    facts["days_until_delivery"] = days if days is not None else 0

    low = status.lower()
    is_delayed = low == "in transit" and days is not None and days > threshold
    is_escalate = low in ESCALATE_STATUSES
    facts["is_delayed"] = bool(is_delayed)
    facts["needs_attention"] = bool(is_delayed or is_escalate)
    facts["note"] = _note(status, last_location, eta)
    return facts


def main(argv: list[str]) -> int:
    host = DEFAULT_HOST
    threshold = DEFAULT_THRESHOLD_DAYS
    number: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif arg == "--threshold" and i + 1 < len(argv):
            try:
                threshold = int(argv[i + 1])
            except ValueError:
                threshold = DEFAULT_THRESHOLD_DAYS
            i += 2
        else:
            number = arg
            i += 1

    number = (number or "").strip()
    if not number:
        # No tracking number: emit a failed-lookup record so the batch is robust.
        print(json.dumps({
            "tracking_number": "",
            "lookup_failed": True,
            "not_found": False,
            "status": "",
            "last_location": "",
            "estimated_delivery": "",
            "days_until_delivery": 0,
            "is_delayed": False,
            "needs_attention": False,
            "note": "No tracking number provided.",
        }))
        return 0

    print(json.dumps(lookup(host, number, threshold)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

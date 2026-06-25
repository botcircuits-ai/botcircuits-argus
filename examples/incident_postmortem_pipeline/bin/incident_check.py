#!/usr/bin/env python3
"""Deterministic per-incident postmortem-triage check for `incident_postmortem_pipeline`.

The workflow's `listDecision` step runs this once per incident id (the engine's
`itemFacts` exec path — no LLM). It queries the mock incident-management API for
the incident's severity, resolution status, and context, applies the SLA /
recurrence / security / assignment rules, and prints a single flat JSON object
of facts on stdout. The step's `derive` map turns those into the per-item
variables its branches test.

Usage:
    incident_check.py [--host <api-host>]
                       [--sla-sev1 <minutes>] [--sla-sev2 <minutes>] [--sla-sev3 <minutes>]
                       <incident_id>

    --host        incident-management API host (default http://localhost:4600/v1)
    --sla-sev1    max minutes to acknowledge a Sev1 before it breaches SLA (default 15)
    --sla-sev2    max minutes to acknowledge a Sev2 before it breaches SLA (default 30)
    --sla-sev3    max minutes to acknowledge a Sev3 before it breaches SLA (default 120)

Output JSON fields (all flat; conditions test these):
    incident_id, lookup_failed, not_found,
    severity, status, customer_impact, duration_minutes,
    is_recurring, is_security_related, assigned_engineer,
    time_to_acknowledge_minutes, sla_minutes, sla_breached,
    no_engineer_assigned, auto_resolved, requires_postmortem,
    needs_attention, note

This never raises: any connection/HTTP/parse failure becomes
`lookup_failed: true` so the batch keeps going (that item -> outcome "error").
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://localhost:4600/v1"
DEFAULT_SLA_SEV1_MIN = 15
DEFAULT_SLA_SEV2_MIN = 30
DEFAULT_SLA_SEV3_MIN = 120
HTTP_TIMEOUT_S = 10


def _get_json(url: str) -> tuple[int, dict | None]:
    """GET a URL; return (status_code, parsed_json_or_None). Never raises."""
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
        return 0, None  # connection/timeout

    if not body.strip():
        return code, None
    try:
        return code, json.loads(body)
    except Exception:
        return code, None


def _note(facts: dict) -> str:
    if facts["lookup_failed"]:
        return "Incident lookup failed (connection / non-200 / unreadable)."
    if facts["not_found"]:
        return "Incident not on file in the incident-management system."
    if facts["is_security_related"]:
        return "Security/compliance incident — routing to legal/compliance review."
    if facts["requires_postmortem"]:
        return f"{facts['severity'].upper()} / customer-impacting — mandatory blameless postmortem."
    if facts["is_recurring"]:
        return "Matches a known recurring pattern — escalating root-cause to engineering leadership."
    if facts["no_engineer_assigned"]:
        return "No engineer was ever assigned — flagging the on-call process."
    if facts["sla_breached"]:
        return f"Acknowledged in {facts['time_to_acknowledge_minutes']}m, over the {facts['sla_minutes']}m {facts['severity'].upper()} SLA."
    if facts["auto_resolved"] and not facts["customer_impact"]:
        return "Auto-resolved with no customer impact — no action needed."
    return f"{facts['severity'].upper()}, non-recurring, within SLA — lightweight writeup only."


# Classification priority (mirrored by the workflow's `listDecision` branches,
# evaluated in this order — first match wins):
#   1. lookup_failed          -> error
#   2. not_found              -> not_found
#   3. is_security_related    -> security_review
#   4. requires_postmortem    -> mandatory_postmortem   (sev1/sev2 or customer-facing)
#   5. is_recurring            -> recurring_escalation
#   6. auto_resolved AND not customer_impact -> no_action
#   7. no_engineer_assigned   -> process_gap
#   8. sla_breached           -> sla_breach
#   9. otherwise              -> quick_writeup
# `no_action` is checked ahead of `process_gap` so an auto-resolved incident
# that legitimately never needed a human responder isn't mistaken for an
# on-call staffing gap.


def check(host: str, incident_id: str, sla_minutes_by_severity: dict[str, int]) -> dict:
    facts: dict = {
        "incident_id": incident_id,
        "lookup_failed": False,
        "not_found": False,
        "severity": "",
        "status": "",
        "customer_impact": False,
        "duration_minutes": 0,
        "is_recurring": False,
        "is_security_related": False,
        "assigned_engineer": "",
        "time_to_acknowledge_minutes": 0,
        "sla_minutes": 0,
        "sla_breached": False,
        "no_engineer_assigned": False,
        "auto_resolved": False,
        "requires_postmortem": False,
        "needs_attention": False,
        "note": "",
    }

    url = f"{host.rstrip('/')}/incident?id={urllib.parse.quote(incident_id)}"
    code, data = _get_json(url)

    # Connection/timeout/unreadable.
    if code == 0 or (code == 200 and data is None):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # Incident unknown to the IMS.
    if code == 404:
        facts["not_found"] = True
        facts["note"] = _note(facts)
        return facts

    # Any other non-200.
    if code != 200 or not isinstance(data, dict):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    severity = str(data.get("severity") or "").lower()
    status = str(data.get("status") or "").lower()
    engineer = str(data.get("assigned_engineer") or "").strip()
    ack_minutes = int(data.get("time_to_acknowledge_minutes") or 0)
    sla_minutes = int(sla_minutes_by_severity.get(severity, DEFAULT_SLA_SEV3_MIN))

    facts["severity"] = severity
    facts["status"] = status
    facts["customer_impact"] = bool(data.get("customer_impact"))
    facts["duration_minutes"] = int(data.get("duration_minutes") or 0)
    facts["is_recurring"] = bool(data.get("is_recurring"))
    facts["is_security_related"] = bool(data.get("is_security_related"))
    facts["assigned_engineer"] = engineer
    facts["time_to_acknowledge_minutes"] = ack_minutes
    facts["sla_minutes"] = sla_minutes
    facts["sla_breached"] = ack_minutes > sla_minutes
    facts["no_engineer_assigned"] = engineer == ""
    facts["auto_resolved"] = status == "auto-resolved"
    facts["requires_postmortem"] = bool(
        severity in ("sev1", "sev2") or facts["customer_impact"]
    )

    facts["needs_attention"] = bool(
        facts["is_security_related"]
        or facts["requires_postmortem"]
        or facts["is_recurring"]
        or facts["no_engineer_assigned"]
        or facts["sla_breached"]
    )
    facts["note"] = _note(facts)
    return facts


def main(argv: list[str]) -> int:
    host = DEFAULT_HOST
    sla_sev1 = DEFAULT_SLA_SEV1_MIN
    sla_sev2 = DEFAULT_SLA_SEV2_MIN
    sla_sev3 = DEFAULT_SLA_SEV3_MIN
    incident_id: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif arg == "--sla-sev1" and i + 1 < len(argv):
            try:
                sla_sev1 = int(argv[i + 1])
            except ValueError:
                sla_sev1 = DEFAULT_SLA_SEV1_MIN
            i += 2
        elif arg == "--sla-sev2" and i + 1 < len(argv):
            try:
                sla_sev2 = int(argv[i + 1])
            except ValueError:
                sla_sev2 = DEFAULT_SLA_SEV2_MIN
            i += 2
        elif arg == "--sla-sev3" and i + 1 < len(argv):
            try:
                sla_sev3 = int(argv[i + 1])
            except ValueError:
                sla_sev3 = DEFAULT_SLA_SEV3_MIN
            i += 2
        else:
            incident_id = arg
            i += 1

    incident_id = (incident_id or "").strip()
    sla_minutes_by_severity = {"sev1": sla_sev1, "sev2": sla_sev2, "sev3": sla_sev3}

    if not incident_id:
        print(json.dumps({
            "incident_id": "",
            "lookup_failed": True,
            "not_found": False,
            "severity": "",
            "status": "",
            "customer_impact": False,
            "duration_minutes": 0,
            "is_recurring": False,
            "is_security_related": False,
            "assigned_engineer": "",
            "time_to_acknowledge_minutes": 0,
            "sla_minutes": 0,
            "sla_breached": False,
            "no_engineer_assigned": False,
            "auto_resolved": False,
            "requires_postmortem": False,
            "needs_attention": False,
            "note": "No incident id provided.",
        }))
        return 0

    print(json.dumps(check(host, incident_id, sla_minutes_by_severity)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

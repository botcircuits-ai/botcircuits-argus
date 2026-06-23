#!/usr/bin/env python3
"""Deterministic per-service release-gate check for `deployment_release_gate`.

The workflow's `listDecision` step runs this once per service name (the
engine's `itemFacts` exec path — no LLM). It queries the mock DevOps API for the
service's deployment/health metrics plus the global change-freeze status, applies
the numeric gate thresholds, and prints a single flat JSON object of facts on
stdout. The step's `derive` map turns those into the per-item variables its
branches test.

Usage:
    gate_check.py [--host <api-host>]
                  [--error-budget <pct>] [--latency-budget <ms>]
                  <service_name>

    --host            DevOps API host (default http://localhost:4100/v1)
    --error-budget    max acceptable error rate, percent (default 1.0)
    --latency-budget  max acceptable p95 latency, ms      (default 500)

Output JSON fields (all flat; conditions test these):
    service, lookup_failed, not_found, freeze_active,
    ci_failed, has_incident, no_baseline,
    over_error_budget, over_latency_budget,
    error_rate, p95_latency_ms, open_sev_incidents, needs_attention, note

This never raises: any connection/HTTP/parse failure becomes
`lookup_failed: true` so the batch keeps going (that item -> outcome "error").
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://localhost:4100/v1"
DEFAULT_ERROR_BUDGET_PCT = 1.0
DEFAULT_LATENCY_BUDGET_MS = 500
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


def _freeze_active(host: str) -> bool:
    """Whether a global production change freeze is currently in effect."""
    code, data = _get_json(f"{host.rstrip('/')}/freeze")
    if code == 200 and isinstance(data, dict):
        return bool(data.get("freeze_active"))
    return False  # fail open on the freeze check; the gate still runs


def _note(facts: dict) -> str:
    if facts["lookup_failed"]:
        return "Metrics lookup failed (connection / non-200 / unreadable)."
    if facts["not_found"]:
        return "Service not registered in the deploy catalog."
    if facts["freeze_active"]:
        return "Blocked by an active production change freeze."
    if facts["ci_failed"]:
        return "Latest CI pipeline failed."
    if facts["has_incident"]:
        return f"{facts['open_sev_incidents']} open SEV incident(s) — paging on-call."
    if facts["over_error_budget"]:
        return f"Error rate {facts['error_rate']}% exceeds budget."
    if facts["over_latency_budget"]:
        return f"p95 latency {facts['p95_latency_ms']}ms exceeds budget."
    if facts["no_baseline"]:
        return "No prior deploy baseline — canary recommended."
    return "All gates green — clear to promote."


def check(host: str, service: str, error_budget: float, latency_budget: int) -> dict:
    facts: dict = {
        "service": service,
        "lookup_failed": False,
        "not_found": False,
        "freeze_active": False,
        "ci_failed": False,
        "has_incident": False,
        "no_baseline": False,
        "over_error_budget": False,
        "over_latency_budget": False,
        "error_rate": 0.0,
        "p95_latency_ms": 0,
        "open_sev_incidents": 0,
        "needs_attention": False,
        "note": "",
    }

    url = f"{host.rstrip('/')}/health?service={urllib.parse.quote(service)}"
    code, data = _get_json(url)

    # Connection/timeout/unreadable.
    if code == 0 or (code == 200 and data is None):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # Service unknown to the catalog.
    if code == 404:
        facts["not_found"] = True
        facts["note"] = _note(facts)
        return facts

    # Any other non-200.
    if code != 200 or not isinstance(data, dict):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # The freeze window is a global gate; check it only when metrics are good.
    facts["freeze_active"] = _freeze_active(host)

    error_rate = float(data.get("error_rate") or 0.0)
    p95 = int(data.get("p95_latency_ms") or 0)
    incidents = int(data.get("open_sev_incidents") or 0)

    facts["error_rate"] = error_rate
    facts["p95_latency_ms"] = p95
    facts["open_sev_incidents"] = incidents
    facts["ci_failed"] = str(data.get("ci_status") or "").lower() != "passed"
    facts["has_incident"] = incidents > 0
    facts["no_baseline"] = not bool(data.get("has_baseline"))
    facts["over_error_budget"] = error_rate > error_budget
    facts["over_latency_budget"] = p95 > latency_budget

    facts["needs_attention"] = bool(
        facts["ci_failed"]
        or facts["has_incident"]
        or facts["over_error_budget"]
        or facts["over_latency_budget"]
        or facts["freeze_active"]
    )
    facts["note"] = _note(facts)
    return facts


def main(argv: list[str]) -> int:
    host = DEFAULT_HOST
    error_budget = DEFAULT_ERROR_BUDGET_PCT
    latency_budget = DEFAULT_LATENCY_BUDGET_MS
    service: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif arg == "--error-budget" and i + 1 < len(argv):
            try:
                error_budget = float(argv[i + 1])
            except ValueError:
                error_budget = DEFAULT_ERROR_BUDGET_PCT
            i += 2
        elif arg == "--latency-budget" and i + 1 < len(argv):
            try:
                latency_budget = int(argv[i + 1])
            except ValueError:
                latency_budget = DEFAULT_LATENCY_BUDGET_MS
            i += 2
        else:
            service = arg
            i += 1

    service = (service or "").strip()
    if not service:
        print(json.dumps({
            "service": "",
            "lookup_failed": True,
            "not_found": False,
            "freeze_active": False,
            "ci_failed": False,
            "has_incident": False,
            "no_baseline": False,
            "over_error_budget": False,
            "over_latency_budget": False,
            "error_rate": 0.0,
            "p95_latency_ms": 0,
            "open_sev_incidents": 0,
            "needs_attention": False,
            "note": "No service name provided.",
        }))
        return 0

    print(json.dumps(check(host, service, error_budget, latency_budget)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

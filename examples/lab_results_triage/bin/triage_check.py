#!/usr/bin/env python3
"""Deterministic per-order lab-results triage check for `lab_results_triage`.

The workflow's `listDecision` step runs this once per lab order id (the engine's
`itemFacts` exec path — no LLM). It queries the mock clinical lab / EHR API for
the order's resulted panel plus patient context, applies the triage rules, and
prints a single flat JSON object of facts on stdout. The step's `derive` map
turns those into the per-item variables its branches test.

Usage:
    triage_check.py [--host <api-host>] [--abnormal-threshold <n>] <order_id>

    --host                clinical lab API host (default http://localhost:4200/v1)
    --abnormal-threshold  min abnormal flags to warrant provider review (default 1)

Output JSON fields (all flat; conditions test these):
    order, lookup_failed, not_found,
    not_resulted, critical_value, drug_interaction, no_episode,
    abnormal, interaction_review, abnormal_flags, worst_flag, panel,
    needs_attention, note

`interaction_review` is the threshold-aware combined fact the workflow's
`interaction` branch tests: an abnormal panel (flags at/above the threshold)
whose ordered follow-up ALSO carries a drug/allergy interaction. The rule
engine evaluates one variable per branch, so the AND is computed here (where
the threshold already lives) rather than in the workflow.

This never raises: any connection/HTTP/parse failure becomes
`lookup_failed: true` so the batch keeps going (that item -> outcome "error").
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://localhost:4200/v1"
DEFAULT_ABNORMAL_THRESHOLD = 1
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
        return "Lab/EHR lookup failed (connection / non-200 / unreadable)."
    if facts["not_found"]:
        return "Lab order not on file."
    if facts["not_resulted"]:
        return f"{facts['panel']} ordered but not yet resulted — deferring."
    if facts["critical_value"]:
        return f"CRITICAL: {facts['worst_flag']} — paging ordering provider."
    if facts["no_episode"]:
        return "Result returned but patient has no active care episode — holding."
    if facts["drug_interaction"]:
        return f"Abnormal ({facts['worst_flag']}) with a drug/allergy interaction on follow-up."
    if facts["abnormal"]:
        return f"Abnormal result for review: {facts['worst_flag']}."
    return f"{facts['panel']} within reference range — routine."


def check(host: str, order: str, abnormal_threshold: int) -> dict:
    facts: dict = {
        "order": order,
        "lookup_failed": False,
        "not_found": False,
        "not_resulted": False,
        "critical_value": False,
        "drug_interaction": False,
        "no_episode": False,
        "abnormal": False,
        "interaction_review": False,
        "abnormal_flags": 0,
        "worst_flag": "",
        "panel": "",
        "needs_attention": False,
        "note": "",
    }

    url = f"{host.rstrip('/')}/labresult?order={urllib.parse.quote(order)}"
    code, data = _get_json(url)

    # Connection/timeout/unreadable.
    if code == 0 or (code == 200 and data is None):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # Order unknown to the lab system.
    if code == 404:
        facts["not_found"] = True
        facts["note"] = _note(facts)
        return facts

    # Any other non-200.
    if code != 200 or not isinstance(data, dict):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    abnormal_flags = int(data.get("abnormal_flags") or 0)

    facts["panel"] = str(data.get("panel") or "")
    facts["abnormal_flags"] = abnormal_flags
    facts["worst_flag"] = str(data.get("worst_flag") or "")
    facts["not_resulted"] = not bool(data.get("resulted"))
    facts["critical_value"] = bool(data.get("critical_value"))
    facts["drug_interaction"] = bool(data.get("drug_interaction"))
    facts["no_episode"] = not bool(data.get("active_episode"))
    facts["abnormal"] = abnormal_flags >= abnormal_threshold
    # Threshold-aware AND for the `interaction` outcome (the rule engine tests
    # one variable per branch, so the conjunction is resolved here).
    facts["interaction_review"] = bool(facts["abnormal"] and facts["drug_interaction"])

    facts["needs_attention"] = bool(
        facts["critical_value"]
        or facts["drug_interaction"]
        or facts["abnormal"]
    )
    facts["note"] = _note(facts)
    return facts


def main(argv: list[str]) -> int:
    host = DEFAULT_HOST
    abnormal_threshold = DEFAULT_ABNORMAL_THRESHOLD
    order: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif arg == "--abnormal-threshold" and i + 1 < len(argv):
            try:
                abnormal_threshold = int(argv[i + 1])
            except ValueError:
                abnormal_threshold = DEFAULT_ABNORMAL_THRESHOLD
            i += 2
        else:
            order = arg
            i += 1

    order = (order or "").strip()
    if not order:
        print(json.dumps({
            "order": "",
            "lookup_failed": True,
            "not_found": False,
            "not_resulted": False,
            "critical_value": False,
            "drug_interaction": False,
            "no_episode": False,
            "abnormal": False,
            "interaction_review": False,
            "abnormal_flags": 0,
            "worst_flag": "",
            "panel": "",
            "needs_attention": False,
            "note": "No lab order id provided.",
        }))
        return 0

    print(json.dumps(check(host, order, abnormal_threshold)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

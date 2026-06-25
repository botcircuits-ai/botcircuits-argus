#!/usr/bin/env python3
"""Deterministic per-test flakiness check for `test_suite_flakiness_triage`.

The workflow's `listDecision` step runs this once per test id (the engine's
`itemFacts` exec path — no LLM). It queries the mock test-analytics API for the
test's historical pass/fail signal, applies the flakiness-rate threshold, and
prints a single flat JSON object of facts on stdout. The step's `derive` map
turns those into the per-item variables its branches test.

Usage:
    testhealth_check.py [--host <api-host>] [--threshold <pct>] <test_id>

    --host        test-analytics API host (default http://localhost:4700/v1)
    --threshold   max acceptable flakiness rate, percent (default 8.0)

Output JSON fields (all flat; conditions test these):
    test, lookup_failed, not_found,
    has_history, currently_quarantined,
    is_consistent_failure, is_flaky, is_timing_issue, is_environment_issue,
    over_threshold, total_runs, pass_count, fail_count, flakiness_rate,
    failure_pattern, runner_specific_failures, needs_attention, note

This never raises: any connection/HTTP/parse failure becomes
`lookup_failed: true` so the batch keeps going (that item -> outcome "error").
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://localhost:4700/v1"
DEFAULT_THRESHOLD_PCT = 8.0
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
        return "Test-analytics lookup failed (connection / non-200 / unreadable)."
    if facts["not_found"]:
        return "Test is unknown to the test-analytics service."
    if facts["is_consistent_failure"]:
        return f"Consistently failing ({facts['fail_count']}/{facts['total_runs']} runs) — real bug, not flaky."
    if facts["currently_quarantined"] and not facts["over_threshold"]:
        return f"Currently quarantined but now stable ({facts['flakiness_rate']}%) — eligible for release."
    if facts["over_threshold"] and not facts["currently_quarantined"]:
        return f"Flakiness rate {facts['flakiness_rate']}% exceeds the {facts['threshold']}% threshold — quarantine."
    if facts["is_environment_issue"]:
        return "Fails only on specific runners — environment/infra issue, not a code bug."
    if facts["is_timing_issue"]:
        return "Race-condition / timing-dependent failure pattern — needs a test-code fix."
    if not facts["has_history"]:
        return "No run history yet — needs a baseline before triage can judge it."
    return f"Flakiness rate {facts['flakiness_rate']}% is within threshold — stable."


def check(host: str, test_id: str, threshold: float) -> dict:
    facts: dict = {
        "test": test_id,
        "lookup_failed": False,
        "not_found": False,
        "has_history": False,
        "currently_quarantined": False,
        "is_consistent_failure": False,
        "is_flaky": False,
        "is_timing_issue": False,
        "is_environment_issue": False,
        "over_threshold": False,
        "total_runs": 0,
        "pass_count": 0,
        "fail_count": 0,
        "flakiness_rate": 0.0,
        "failure_pattern": "none",
        "runner_specific_failures": False,
        "threshold": threshold,
        "needs_attention": False,
        "note": "",
    }

    url = f"{host.rstrip('/')}/testhealth?test={urllib.parse.quote(test_id)}"
    code, data = _get_json(url)

    # Connection/timeout/unreadable.
    if code == 0 or (code == 200 and data is None):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # Test unknown to the analytics service.
    if code == 404:
        facts["not_found"] = True
        facts["note"] = _note(facts)
        return facts

    # Any other non-200.
    if code != 200 or not isinstance(data, dict):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    total_runs = int(data.get("total_runs") or 0)
    pass_count = int(data.get("pass_count") or 0)
    fail_count = int(data.get("fail_count") or 0)
    flakiness_rate = float(data.get("flakiness_rate") or 0.0)
    failure_pattern = str(data.get("failure_pattern") or "none").lower()

    facts["has_history"] = bool(data.get("has_history"))
    facts["currently_quarantined"] = bool(data.get("currently_quarantined"))
    facts["total_runs"] = total_runs
    facts["pass_count"] = pass_count
    facts["fail_count"] = fail_count
    facts["flakiness_rate"] = flakiness_rate
    facts["failure_pattern"] = failure_pattern
    facts["runner_specific_failures"] = bool(data.get("runner_specific_failures"))

    # Consistently failing (every run fails, pattern says "consistent") is a
    # real bug, not flakiness — never quarantine these.
    facts["is_consistent_failure"] = bool(
        failure_pattern == "consistent" and total_runs > 0 and pass_count == 0
    )
    facts["is_timing_issue"] = failure_pattern == "timing"
    facts["is_environment_issue"] = bool(
        failure_pattern == "environment" or facts["runner_specific_failures"]
    )
    facts["over_threshold"] = flakiness_rate > threshold
    facts["is_flaky"] = bool(
        facts["over_threshold"]
        and not facts["is_consistent_failure"]
        and not facts["is_environment_issue"]
        and not facts["is_timing_issue"]
    )

    facts["needs_attention"] = bool(
        facts["is_consistent_failure"]
        or facts["is_flaky"]
        or facts["is_timing_issue"]
        or facts["is_environment_issue"]
    )
    facts["note"] = _note(facts)
    return facts


def main(argv: list[str]) -> int:
    host = DEFAULT_HOST
    threshold = DEFAULT_THRESHOLD_PCT
    test_id: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif arg == "--threshold" and i + 1 < len(argv):
            try:
                threshold = float(argv[i + 1])
            except ValueError:
                threshold = DEFAULT_THRESHOLD_PCT
            i += 2
        else:
            test_id = arg
            i += 1

    test_id = (test_id or "").strip()
    if not test_id:
        print(json.dumps({
            "test": "",
            "lookup_failed": True,
            "not_found": False,
            "has_history": False,
            "currently_quarantined": False,
            "is_consistent_failure": False,
            "is_flaky": False,
            "is_timing_issue": False,
            "is_environment_issue": False,
            "over_threshold": False,
            "total_runs": 0,
            "pass_count": 0,
            "fail_count": 0,
            "flakiness_rate": 0.0,
            "failure_pattern": "none",
            "runner_specific_failures": False,
            "threshold": threshold,
            "needs_attention": False,
            "note": "No test id provided.",
        }))
        return 0

    print(json.dumps(check(host, test_id, threshold)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

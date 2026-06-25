#!/usr/bin/env python3
"""Deterministic per-job CI triage check for `ci_pipeline_triage`.

The workflow's `listDecision` step runs this once per failing job/build id (the
engine's `itemFacts` exec path — no LLM). It queries the mock CI metrics API for
the job's failure signals plus the global CI-provider status, applies the
flakiness/queue thresholds, and prints a single flat JSON object of facts on
stdout. The step's `derive` map turns those into the per-item variables its
branches test.

Usage:
    job_check.py [--host <api-host>]
                 [--flaky-threshold <rate>] [--retry-budget <n>]
                 <job_id>

    --host             CI metrics API host (default http://localhost:4400/v1)
    --flaky-threshold  historical failure rate above which a job is "flaky"
                        (default 0.3)
    --retry-budget     max auto-retries considered before quarantine
                        (default 3)

Output JSON fields (all flat; conditions test these):
    job_id, lookup_failed, not_found, provider_down,
    is_infra_error, memory_exceeded, is_timeout, is_lint_failure,
    is_compile_error, has_baseline, over_flaky_threshold,
    failure_reason, exit_code, duration_seconds, retry_count,
    historical_failure_rate, needs_attention, note

This never raises: any connection/HTTP/parse failure becomes
`lookup_failed: true` so the batch keeps going (that item -> outcome "error").
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://localhost:4400/v1"
DEFAULT_FLAKY_THRESHOLD = 0.3
DEFAULT_RETRY_BUDGET = 3
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


def _provider_down(host: str) -> bool:
    """Whether the global CI provider backend is reporting an outage."""
    code, data = _get_json(f"{host.rstrip('/')}/status")
    if code == 200 and isinstance(data, dict):
        return bool(data.get("provider_down"))
    return False  # fail open on the status check; triage still runs


def _note(facts: dict) -> str:
    if facts["lookup_failed"]:
        return "Job metrics lookup failed (connection / non-200 / unreadable)."
    if facts["not_found"]:
        return "Job id not known to the CI metrics backend."
    if facts["provider_down"]:
        return "CI provider backend is reporting an outage — data untrustworthy."
    if facts["is_infra_error"]:
        return "Failure attributed to CI runner/infra fault, not the code change."
    if facts["over_flaky_threshold"]:
        return f"Historical failure rate {facts['historical_failure_rate']} exceeds flaky threshold — retry/quarantine candidate."
    if facts["memory_exceeded"]:
        return "Job exceeded its memory limit (OOM)."
    if facts["is_timeout"]:
        return "Job exceeded its time limit."
    if facts["is_compile_error"]:
        return "Deterministic compile/build failure — blocks merge."
    if facts["is_lint_failure"]:
        return "Lint/style failure — auto-fixable."
    if not facts["has_baseline"]:
        return "New pipeline with no failure-history baseline yet."
    return "Job failed without a recognized triage signal."


def check(host: str, job_id: str, flaky_threshold: float, retry_budget: int) -> dict:
    facts: dict = {
        "job_id": job_id,
        "lookup_failed": False,
        "not_found": False,
        "provider_down": False,
        "is_infra_error": False,
        "memory_exceeded": False,
        "is_timeout": False,
        "is_lint_failure": False,
        "is_compile_error": False,
        "has_baseline": True,
        "over_flaky_threshold": False,
        "failure_reason": "",
        "exit_code": 0,
        "duration_seconds": 0,
        "retry_count": 0,
        "historical_failure_rate": 0.0,
        "needs_attention": False,
        "note": "",
    }

    url = f"{host.rstrip('/')}/job?id={urllib.parse.quote(job_id)}"
    code, data = _get_json(url)

    # Connection/timeout/unreadable.
    if code == 0 or (code == 200 and data is None):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # Job unknown to the CI metrics backend.
    if code == 404:
        facts["not_found"] = True
        facts["note"] = _note(facts)
        return facts

    # Any other non-200.
    if code != 200 or not isinstance(data, dict):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # The provider-outage status is a global gate; check it only when the job
    # record itself resolved cleanly.
    facts["provider_down"] = _provider_down(host)

    failure_reason = str(data.get("failure_reason") or "")
    exit_code = int(data.get("exit_code") or 0)
    duration_seconds = int(data.get("duration_seconds") or 0)
    retry_count = int(data.get("retry_count") or 0)
    historical_failure_rate = float(data.get("historical_failure_rate") or 0.0)

    facts["failure_reason"] = failure_reason
    facts["exit_code"] = exit_code
    facts["duration_seconds"] = duration_seconds
    facts["retry_count"] = retry_count
    facts["historical_failure_rate"] = historical_failure_rate
    facts["is_infra_error"] = bool(data.get("is_infra_error"))
    facts["memory_exceeded"] = bool(data.get("memory_exceeded"))
    facts["is_timeout"] = failure_reason.lower() == "timeout"
    facts["is_lint_failure"] = failure_reason.lower() in ("lint", "lint_failure")
    facts["is_compile_error"] = failure_reason.lower() in (
        "compile_error",
        "build_failure",
    )
    facts["has_baseline"] = bool(data.get("has_baseline", True))
    facts["over_flaky_threshold"] = (
        historical_failure_rate > flaky_threshold and not facts["is_compile_error"]
    )

    facts["needs_attention"] = bool(
        facts["is_infra_error"]
        or facts["over_flaky_threshold"]
        or facts["memory_exceeded"]
        or facts["is_timeout"]
        or facts["is_compile_error"]
        or facts["provider_down"]
    )
    facts["note"] = _note(facts)
    return facts


def main(argv: list[str]) -> int:
    host = DEFAULT_HOST
    flaky_threshold = DEFAULT_FLAKY_THRESHOLD
    retry_budget = DEFAULT_RETRY_BUDGET
    job_id: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif arg == "--flaky-threshold" and i + 1 < len(argv):
            try:
                flaky_threshold = float(argv[i + 1])
            except ValueError:
                flaky_threshold = DEFAULT_FLAKY_THRESHOLD
            i += 2
        elif arg == "--retry-budget" and i + 1 < len(argv):
            try:
                retry_budget = int(argv[i + 1])
            except ValueError:
                retry_budget = DEFAULT_RETRY_BUDGET
            i += 2
        else:
            job_id = arg
            i += 1

    job_id = (job_id or "").strip()
    if not job_id:
        print(json.dumps({
            "job_id": "",
            "lookup_failed": True,
            "not_found": False,
            "provider_down": False,
            "is_infra_error": False,
            "memory_exceeded": False,
            "is_timeout": False,
            "is_lint_failure": False,
            "is_compile_error": False,
            "has_baseline": True,
            "over_flaky_threshold": False,
            "failure_reason": "",
            "exit_code": 0,
            "duration_seconds": 0,
            "retry_count": 0,
            "historical_failure_rate": 0.0,
            "needs_attention": False,
            "note": "No job id provided.",
        }))
        return 0

    print(json.dumps(check(host, job_id, flaky_threshold, retry_budget)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

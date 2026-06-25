#!/usr/bin/env python3
"""Deterministic per-file code-review scan check for `code_review_gate`.

The workflow's `listDecision` step runs this once per changed file (the
engine's `itemFacts` exec path — no LLM). It queries the mock code-quality API
for the file's static-analysis/lint/security scan results, applies the
numeric review thresholds, and prints a single flat JSON object of facts on
stdout. The step's `derive` map turns those into the per-item variables its
branches test.

Usage:
    scan_check.py [--host <api-host>]
                  [--complexity-budget <n>] [--lint-warning-budget <n>]
                  [--large-diff-lines <n>]
                  <file_path>

    --host                 code-quality API host (default http://localhost:4300/v1)
    --complexity-budget    max acceptable cyclomatic complexity score (default 15)
    --lint-warning-budget  max acceptable lint-warning count        (default 5)
    --large-diff-lines     lines-changed threshold for manual review (default 400)

Output JSON fields (all flat; conditions test these):
    file, lookup_failed, not_found,
    lint_errors, lint_warnings, security_findings, worst_security_severity,
    is_duplicate, duplicate_of, lines_changed, complexity_score,
    test_coverage_delta, has_baseline,
    has_critical_security, has_high_security,
    over_complexity_budget, over_lint_warning_budget, over_large_diff,
    coverage_dropped, needs_review_flags, note

`needs_review_flags` is the threshold-aware combined fact the workflow's
`review` branch tests: lint warnings at/above the budget AND a test-coverage
drop on the same file. The rule engine evaluates one variable per branch, so
the AND is computed here (where the threshold already lives) rather than in
the workflow.

This never raises: any connection/HTTP/parse failure becomes
`lookup_failed: true` so the batch keeps going (that item -> outcome "error").
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_HOST = "http://localhost:4300/v1"
DEFAULT_COMPLEXITY_BUDGET = 15
DEFAULT_LINT_WARNING_BUDGET = 5
DEFAULT_LARGE_DIFF_LINES = 400
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
        return "Scan lookup failed (connection / non-200 / unreadable)."
    if facts["not_found"]:
        return "File not present in the scan catalog."
    if facts["has_critical_security"]:
        return f"CRITICAL security finding ({facts['worst_security_severity']}) — blocking merge."
    if facts["lint_errors"] > 0:
        return f"{facts['lint_errors']} lint error(s) — blocking merge."
    if facts["has_high_security"]:
        return f"High-severity security finding ({facts['worst_security_severity']}) — needs fix."
    if facts["over_complexity_budget"]:
        return f"Complexity score {facts['complexity_score']} exceeds budget — needs fix."
    if facts["is_duplicate"]:
        return f"Duplicate of {facts['duplicate_of']} — needs fix (de-duplicate)."
    if facts["needs_review_flags"]:
        return f"{facts['lint_warnings']} lint warning(s) and a coverage drop — queue for review."
    if facts["over_large_diff"]:
        return f"{facts['lines_changed']} lines changed — large diff, needs manual review."
    return "Within budget — clear to merge."


def check(
    host: str,
    file_path: str,
    complexity_budget: int,
    lint_warning_budget: int,
    large_diff_lines: int,
) -> dict:
    facts: dict = {
        "file": file_path,
        "lookup_failed": False,
        "not_found": False,
        "lint_errors": 0,
        "lint_warnings": 0,
        "security_findings": 0,
        "worst_security_severity": "",
        "is_duplicate": False,
        "duplicate_of": "",
        "lines_changed": 0,
        "complexity_score": 0,
        "test_coverage_delta": 0.0,
        "has_baseline": False,
        "has_critical_security": False,
        "has_high_security": False,
        "over_complexity_budget": False,
        "over_lint_warning_budget": False,
        "over_large_diff": False,
        "coverage_dropped": False,
        "needs_review_flags": False,
        "note": "",
    }

    url = f"{host.rstrip('/')}/scan?file={urllib.parse.quote(file_path)}"
    code, data = _get_json(url)

    # Connection/timeout/unreadable.
    if code == 0 or (code == 200 and data is None):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    # File unknown to the scan catalog.
    if code == 404:
        facts["not_found"] = True
        facts["note"] = _note(facts)
        return facts

    # Any other non-200.
    if code != 200 or not isinstance(data, dict):
        facts["lookup_failed"] = True
        facts["note"] = _note(facts)
        return facts

    lint_warnings = int(data.get("lint_warnings") or 0)
    coverage_delta = float(data.get("test_coverage_delta") or 0.0)
    severity = str(data.get("worst_security_severity") or "").lower()

    facts["lint_errors"] = int(data.get("lint_errors") or 0)
    facts["lint_warnings"] = lint_warnings
    facts["security_findings"] = int(data.get("security_findings") or 0)
    facts["worst_security_severity"] = str(data.get("worst_security_severity") or "")
    facts["is_duplicate"] = bool(data.get("is_duplicate_of"))
    facts["duplicate_of"] = str(data.get("is_duplicate_of") or "")
    facts["lines_changed"] = int(data.get("lines_changed") or 0)
    facts["complexity_score"] = int(data.get("complexity_score") or 0)
    facts["test_coverage_delta"] = coverage_delta
    facts["has_baseline"] = bool(data.get("has_baseline"))

    facts["has_critical_security"] = severity == "critical"
    facts["has_high_security"] = severity == "high"
    facts["over_complexity_budget"] = facts["complexity_score"] > complexity_budget
    facts["over_lint_warning_budget"] = lint_warnings > lint_warning_budget
    facts["over_large_diff"] = facts["lines_changed"] > large_diff_lines
    facts["coverage_dropped"] = coverage_delta < 0
    # Threshold-aware AND for the `review` outcome (the rule engine tests one
    # variable per branch, so the conjunction is resolved here).
    facts["needs_review_flags"] = bool(
        facts["over_lint_warning_budget"] and facts["coverage_dropped"]
    )

    facts["note"] = _note(facts)
    return facts


def main(argv: list[str]) -> int:
    host = DEFAULT_HOST
    complexity_budget = DEFAULT_COMPLEXITY_BUDGET
    lint_warning_budget = DEFAULT_LINT_WARNING_BUDGET
    large_diff_lines = DEFAULT_LARGE_DIFF_LINES
    file_path: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif arg == "--complexity-budget" and i + 1 < len(argv):
            try:
                complexity_budget = int(argv[i + 1])
            except ValueError:
                complexity_budget = DEFAULT_COMPLEXITY_BUDGET
            i += 2
        elif arg == "--lint-warning-budget" and i + 1 < len(argv):
            try:
                lint_warning_budget = int(argv[i + 1])
            except ValueError:
                lint_warning_budget = DEFAULT_LINT_WARNING_BUDGET
            i += 2
        elif arg == "--large-diff-lines" and i + 1 < len(argv):
            try:
                large_diff_lines = int(argv[i + 1])
            except ValueError:
                large_diff_lines = DEFAULT_LARGE_DIFF_LINES
            i += 2
        else:
            file_path = arg
            i += 1

    file_path = (file_path or "").strip()
    if not file_path:
        print(json.dumps({
            "file": "",
            "lookup_failed": True,
            "not_found": False,
            "lint_errors": 0,
            "lint_warnings": 0,
            "security_findings": 0,
            "worst_security_severity": "",
            "is_duplicate": False,
            "duplicate_of": "",
            "lines_changed": 0,
            "complexity_score": 0,
            "test_coverage_delta": 0.0,
            "has_baseline": False,
            "has_critical_security": False,
            "has_high_security": False,
            "over_complexity_budget": False,
            "over_lint_warning_budget": False,
            "over_large_diff": False,
            "coverage_dropped": False,
            "needs_review_flags": False,
            "note": "No file path provided.",
        }))
        return 0

    print(json.dumps(
        check(host, file_path, complexity_budget, lint_warning_budget, large_diff_lines)
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

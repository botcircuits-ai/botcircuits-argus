"""Data contracts shared by the verification-gate executor and its two
check runners (`script_check.py`, `judge_check.py`). Split into its own
module so both runners can import the dataclasses without importing
`executor.py` (which imports both of them)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CheckResult:
    """The outcome of running one check (`type: "script"` or
    `"llm_judge"`) against a completed workflow run.

    `error` is set when the check itself failed to execute (script
    crash/timeout, judge call/parse failure) — distinct from a normal
    `passed=False`. The executor always treats an `error` as blocking
    regardless of `severity`, since a broken check must never silently
    pass the gate."""

    check_id: str
    passed: bool
    severity: str  # "blocking" | "advisory"
    detail: str = ""
    error: str | None = None

    @property
    def is_blocking_failure(self) -> bool:
        """True if this result should fail the gate: either an
        execution error (always blocking), or a normal fail on a
        blocking-severity check."""
        if self.error is not None:
            return True
        return not self.passed and self.severity == "blocking"


@dataclass
class GateResult:
    """The outcome of running an entire gate (`gate.json`) against one
    completed workflow run."""

    workflow_name: str
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)

    def blocking_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.is_blocking_failure]

    def to_dict(self) -> dict:
        return {
            "workflow_name": self.workflow_name,
            "passed": self.passed,
            "checks": [
                {
                    "id": c.check_id,
                    "passed": c.passed,
                    "severity": c.severity,
                    "detail": c.detail,
                    "error": c.error,
                }
                for c in self.checks
            ],
        }


class VerificationError(RuntimeError):
    """Raised when the gate itself is mis-configured in a way that
    can't be attributed to any one check — e.g. an `llm_judge` check is
    present but no `provider` was supplied. Distinct from a `CheckResult`
    error, which is scoped to a single check."""

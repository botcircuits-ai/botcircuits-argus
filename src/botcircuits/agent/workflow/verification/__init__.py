"""Verification gate: a generic executor (`run_gate`) that runs a
workflow-specific set of checks — deterministic scripts and/or LLM
judges — against a completed run's outcome, plus the build-time
generator that produces those checks (`generate_gate`).

See `executor.py` for the framework/data-vs-behavior split this package
is built around: `run_gate` never changes; only the `gate_spec` it's
given (generated per-workflow) does.
"""

from __future__ import annotations

from .executor import run_gate
from .generator import generate_gate
from .types import CheckResult, GateResult, VerificationError

__all__ = [
    "run_gate",
    "generate_gate",
    "CheckResult",
    "GateResult",
    "VerificationError",
]

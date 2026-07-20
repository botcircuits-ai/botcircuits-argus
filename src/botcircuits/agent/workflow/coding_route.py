"""Deterministic detection of coding requests → the coding pipeline.

When the user asks for a coding task, the native agent must NOT freewheel
with the LLM: it routes the request through the static coding pipeline
workflow (`CODING_PIPELINE_WORKFLOW`), which derives requirements, plans,
GENERATES a per-task coding workflow, runs it, validates with the project's
tests, and loops through a gate until the solution is accepted (or a max
loop count is reached).

Routing must be deterministic — decided by the loop BEFORE any provider
call — for the same reason `match_workflow_trigger` exists: a smaller model
asked to "add a dark-mode toggle" answers with clarifying questions or
starts editing files itself instead of entering the pipeline. This module
is the gate that catches a coding request and hands it to the pipeline.

It is intentionally conservative:
  - a leading imperative CODING verb (add / implement / fix / refactor …)
    OR a "can you / please" wrapper around one, and
  - not a bare question ABOUT code ("how does X work?", "what is a
    closure?") — those stay on the normal conversational path.

False negatives are cheap (the request just runs the normal loop); a false
positive drags a non-coding chat into a heavyweight pipeline, so the bar is
deliberately set at an explicit build/modify instruction.
"""

from __future__ import annotations

import re
import string

#: The static pipeline workflow a detected coding request is routed to.
#: Must match the `name` of the built workflow on disk.
CODING_PIPELINE_WORKFLOW = "safe_agentic_workflow"

#: Imperative verbs that make a message a request to CHANGE code. Matched as
#: whole leading words (like `match_workflow_trigger`'s trigger verbs) so a
#: verb buried mid-sentence in a question doesn't count.
_CODING_VERBS = frozenset({
    "add", "implement", "build", "create", "write", "code", "develop",
    "fix", "refactor", "rewrite", "rename", "migrate", "port",
    "optimize", "optimise", "debug", "patch", "update", "change",
    "modify", "remove", "delete", "extract", "generate", "scaffold",
    "integrate", "wire", "extend",
})

#: Nouns that anchor a verb to SOFTWARE work. A verb alone is too broad
#: ("add two numbers", "create a reminder"); it must land near one of these
#: to route. Substring match — "endpoint", "endpoints", "APIs" all count.
_CODING_NOUNS = (
    "function", "method", "class", "module", "endpoint", "api", "route",
    "handler", "component", "test", "unit test", "bug", "feature",
    "code", "script", "file", "parser", "schema", "migration", "model",
    "query", "database", "table", "cli", "command", "flag", "option",
    "config", "type", "interface", "import", "dependency", "package",
    "service", "controller", "middleware", "hook", "validation",
    "regex", "algorithm", "refactor", "typo", "lint", "build", "compile",
)

#: Interrogative openers — a message that opens this way is asking ABOUT
#: code, not asking for a change. Mirrors `workflow.match_workflow_trigger`.
_QUESTION_OPENERS = (
    "how", "what", "why", "when", "where", "who", "which", "explain",
    "describe", "is ", "are ", "does", "do ", "did", "should i", "could you tell",
)

#: Polite wrappers stripped from the front so "can you add a test" is seen
#: as "add a test". Order matters — longest first.
_POLITE_PREFIXES = (
    "can you please", "could you please", "would you please",
    "can you", "could you", "would you", "please", "i want you to",
    "i need you to", "i'd like you to", "help me", "let's", "lets",
)


def _strip_polite_prefix(text: str) -> str:
    """Drop a leading politeness wrapper so the coding verb, if any, becomes
    the first meaningful word."""
    t = text
    changed = True
    while changed:
        changed = False
        low = t.lstrip().lower()
        for pref in _POLITE_PREFIXES:
            if low.startswith(pref):
                # Remove the matched span from the original-cased text.
                t = t.lstrip()[len(pref):]
                changed = True
                break
    return t.strip()


def _leading_coding_verb(tokens: list[str]) -> bool:
    """True when one of the first few tokens is a coding verb — the message
    opens with a build/modify imperative."""
    for tok in tokens[:2]:
        w = tok.strip(string.punctuation).lower()
        if w in _CODING_VERBS:
            return True
    return False


def is_coding_request(text: str) -> bool:
    """True when `text` is an explicit request to write or change code.

    Deterministic, conservative: routes only messages that open with a
    coding imperative (after stripping a polite wrapper) AND mention a
    software noun, and are not phrased as a question about code. See the
    module docstring for the rationale.
    """
    if not text or not text.strip():
        return False
    raw = text.strip()
    lowered = raw.lower()

    # A bare question about code stays conversational, even if it contains a
    # coding verb ("how do I add a route?").
    if lowered.endswith("?") and lowered.startswith(_QUESTION_OPENERS):
        return False

    core = _strip_polite_prefix(raw)
    if not core:
        return False
    tokens = core.split()
    if not _leading_coding_verb(tokens):
        return False

    core_low = core.lower()
    if any(noun in core_low for noun in _CODING_NOUNS):
        return True
    return False


__all__ = ["CODING_PIPELINE_WORKFLOW", "is_coding_request"]

"""Shared plumbing for option-style questions ("pick one of these").

Any pause point that has a predefined answer set (workflow reuse offers,
question steps with authored choices, y/N tool gates) describes it as a
plain `list[str]` of canonical answers. The UI layer renders those as a
selector (arrow keys / numbers in the prompt-toolkit TUI, a numbered list
in piped mode); picking an entry returns its text verbatim, so downstream
interpreters see exactly the canonical answer they published. Anything the
user types instead flows through unchanged and is resolved semantically by
the caller (deterministic interpreters first, LLM classification last).

Two pieces live here because every channel needs them:

- `map_option_reply` — normalize a raw reply against the offered options
  (digit shortcuts, case-insensitive label match). Pure function.
- a process-wide pending-options store — the workflow engine's paused
  question travels to the UI *through the model* (tool result text ->
  `human_feedback` call), which cannot be trusted to carry a structured
  list. The engine parks the options here keyed by question text;
  `human_feedback` picks them up when the question comes back around.
"""

from __future__ import annotations

from dataclasses import dataclass, field


#: The synthetic last entry every selector shows. Picking it never becomes
#: an answer — it switches the prompt to plain typing, whose text then flows
#: through as free-form input. UI-layer only: canonical option lists
#: (EngineResult.options, human_feedback args, the pending store) never
#: contain it.
OTHER_LABEL = "Other (type your answer)"


def is_other_reply(reply: str, options: list[str] | None) -> bool:
    """True when a raw reply picks the synthetic "Other" entry — its number
    (len(options)+1) or the word "other" — rather than answering."""
    if not options:
        return False
    text = (reply or "").strip().lower()
    if text.isdigit() and int(text) == len(options) + 1:
        return True
    return text in ("other", OTHER_LABEL.lower())


def map_option_reply(reply: str, options: list[str] | None) -> str:
    """Normalize a raw reply typed at an option prompt.

    - "2" (a digit within range)      -> that option's text
    - "YES" (case-insensitive match)  -> the option's canonical casing
    - anything else                   -> returned unchanged (free-form;
      the caller resolves it semantically)
    """
    if not options:
        return reply
    text = (reply or "").strip()
    if text.isdigit():
        n = int(text)
        if 1 <= n <= len(options):
            return options[n - 1]
    lowered = text.lower()
    for opt in options:
        if opt.strip().lower() == lowered:
            return opt
    return reply


@dataclass
class OptionOffer:
    """One pending option set, keyed by the question text it belongs to."""

    question: str
    options: list[str] = field(default_factory=list)
    #: Index of the option pre-highlighted in the selector (the safe/likely
    #: answer — e.g. "no" for destructive confirms).
    default_index: int = 0


#: Pending offers, newest last. Kept tiny: entries are consumed on match and
#: the list is capped so an unanswered question can't leak entries forever.
_PENDING: list[OptionOffer] = []
_MAX_PENDING = 8


def offer_options(
    question: str, options: list[str], default_index: int = 0
) -> None:
    """Park an option set for a question about to travel to the UI through
    an untyped channel (model round-trip, plain-text tool result)."""
    if not question or not options:
        return
    _PENDING[:] = [o for o in _PENDING if o.question != question]
    _PENDING.append(OptionOffer(question, list(options), default_index))
    del _PENDING[:-_MAX_PENDING]


def take_options(question: str) -> OptionOffer | None:
    """Consume the pending offer for *question*, if one matches.

    The model relays question text mostly-verbatim but may trim or wrap it,
    so containment in either direction counts as a match. Newest first —
    the question being asked NOW is the one parked last.
    """
    q = (question or "").strip()
    if not q:
        return None
    for i in range(len(_PENDING) - 1, -1, -1):
        stored = _PENDING[i].question.strip()
        if stored == q or stored in q or q in stored:
            return _PENDING.pop(i)
    return None


def clear_options() -> None:
    """Drop all pending offers (tests / session reset)."""
    _PENDING.clear()


async def classify_option_reply(
    provider,
    *,
    question: str,
    options: list[str],
    reply: str,
    max_tokens: int = 128,
) -> str | None:
    """LLM fallback for a typed answer to an option question: does *reply*
    actually pick one of *options*, just phrased differently?

    Returns the matched option verbatim, or None when the reply is
    genuinely free-form (new values, an unrelated message) — the caller
    then resolves it through its normal extraction path. Never raises;
    any provider/parse failure means None.
    """
    import json
    import re
    import sys

    from botcircuits.types import Message

    if provider is None or not options or not (reply or "").strip():
        return None

    prompt = (
        "The user was asked:\n"
        f"{question}\n\n"
        "The predefined answer options are:\n"
        + "\n".join(f"- {o}" for o in options)
        + "\n\nThe user replied:\n"
        f"{reply}\n\n"
        "If the reply is choosing one of the options (any phrasing — "
        "\"yes do same\" chooses \"yes\"), return that option string "
        "EXACTLY as listed. If the reply instead supplies new information "
        "or does not pick an option, return null.\n"
        'Respond with strict JSON: {"choice": "<option>"} or {"choice": null}'
    )
    try:
        response = await provider.complete(
            system=(
                "You produce strict JSON matching the requested shape. "
                "No commentary, no markdown fences, no extra keys."
            ),
            messages=[Message(role="user",
                              blocks=[{"type": "text", "text": prompt}])],
            tools=[],
            hosted_mcp=[],
            skills=[],
            max_tokens=max_tokens,
        )
        m = re.search(r"\{.*\}", response.text or "", re.DOTALL)
        choice = json.loads(m.group(0)).get("choice") if m else None
    except Exception as e:  # provider error / bad JSON — fall back silently
        print(f"[options] reply classification skipped: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return None
    if isinstance(choice, str):
        lowered = choice.strip().lower()
        for opt in options:
            if opt.strip().lower() == lowered:
                return opt
    return None

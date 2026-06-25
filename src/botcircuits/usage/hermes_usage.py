"""Harvest a Hermes oneshot run's REAL token usage from its session store.

Unlike claude-code (which prints a `usage` block on `--output-format json`
stdout), hermes oneshot (`-z`) routes the entire internal agent run to devnull
and prints ONLY the final reply text — so ``usage_from_stdout`` finds nothing.
Naive accounting would record "0 tokens, 0 calls" for a run that actually made
several API calls over tens of thousands of tokens.

Hermes does persist every run to its SQLite session store
(``$HERMES_HOME/state.db``, default ``~/.hermes/state.db``):

  * ``sessions`` — real ``input_tokens`` / ``output_tokens`` (EXCLUSIVE of the
    cached portion), ``cache_read_tokens`` / ``cache_write_tokens``,
    ``api_call_count`` (the true LLM-call count), ``model``, timestamps.

After a segment's subprocess returns we locate the session it created — a
``source='cli'`` session started at/after our launch time whose first user
message is our prompt — and read the ground-truth counters into an
``ActionUsage``. Best-effort: a missing store, an older hermes without the
store, or no matching session simply yields ``None`` (the run shows no usage
for that segment rather than crashing), mirroring how the rest of the usage
path shrugs at unparsable output.

This is the runtime-side counterpart of the eval harness's HermesRunner
harvest; the read shape (column names, the input-tokens-exclusive-of-cache
convention) is kept identical so both read hermes the same way.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from botcircuits.usage.run_usage import ActionUsage


def hermes_state_db() -> Path:
    """Path to hermes' SQLite session store, honoring ``HERMES_HOME``."""
    home = os.environ.get("HERMES_HOME", "").strip()
    base = Path(home).expanduser() if home else Path.home() / ".hermes"
    return base / "state.db"


def _match_session(
    db_path: Path, prompt: str, launched_at: float, claimed: set[str],
) -> str | None:
    """Id of the unclaimed ``source='cli'`` session started at/after
    ``launched_at`` whose first user message is ``prompt``. Read-only/WAL-safe.
    """
    # Hermes stamps started_at when the agent boots, which can land marginally
    # before our subprocess-spawn timestamp on a loaded box — allow some slack.
    cutoff = launched_at - 10
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        rows = con.execute(
            "SELECT id FROM sessions WHERE source='cli' AND started_at >= ? "
            "ORDER BY started_at",
            (cutoff,),
        ).fetchall()
        want = prompt.strip()[:400]
        for (sid,) in rows:
            if sid in claimed:
                continue
            first = con.execute(
                "SELECT content FROM messages WHERE session_id=? AND "
                "role='user' ORDER BY rowid LIMIT 1",
                (sid,),
            ).fetchone()
            got = (first[0] or "").strip()[:400] if first else ""
            if got == want:
                return sid
        return None
    finally:
        con.close()


def _read_usage(db_path: Path, sid: str) -> dict | None:
    """Read the real usage counters for session ``sid`` (read-only)."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        row = con.execute(
            "SELECT input_tokens, output_tokens, api_call_count, "
            "cache_read_tokens, cache_write_tokens FROM sessions WHERE id=?",
            (sid,),
        ).fetchone()
        if not row:
            return None
        return {
            "input_tokens": int(row[0] or 0),
            "output_tokens": int(row[1] or 0),
            "api_call_count": int(row[2] or 0),
            "cache_read_tokens": int(row[3] or 0),
            "cache_write_tokens": int(row[4] or 0),
        }
    finally:
        con.close()


def harvest_usage(
    prompt: str,
    launched_at: float,
    *,
    step: str = "",
    claimed: set[str] | None = None,
) -> ActionUsage | None:
    """Read THIS segment's real usage from hermes' session store.

    ``prompt`` is the exact text handed to ``hermes -z``; ``launched_at`` is the
    ``time.time()`` taken just before spawning. ``claimed`` is an optional set
    of session ids already attributed to earlier segments — concurrent segments
    with identical prompts would otherwise harvest the same row; pass the
    provider's running set and the matched id is added to it.

    Returns an ``ActionUsage`` with ``input_tokens`` as the TOTAL prompt size
    (hermes stores it exclusive of cache, so the cache counters are added back
    to match the rest of the usage path's convention), or ``None`` when the
    store is absent / no session matches.
    """
    db_path = hermes_state_db()
    if not db_path.exists():
        return None
    try:
        sid = _match_session(
            db_path, prompt, launched_at, claimed or set(),
        )
        if sid is None:
            return None
        if claimed is not None:
            claimed.add(sid)
        usage = _read_usage(db_path, sid)
    except Exception:  # noqa: BLE001 — instrumentation must never fail a run
        return None

    if not usage:
        return None
    if not (usage["input_tokens"] or usage["output_tokens"]
            or usage["api_call_count"]):
        return None

    cache_read = usage["cache_read_tokens"]
    cache_write = usage["cache_write_tokens"]
    return ActionUsage(
        step=step,
        runtime="hermes",
        input_tokens=usage["input_tokens"] + cache_read + cache_write,
        output_tokens=usage["output_tokens"],
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        calls=usage["api_call_count"] or 1,
    )


__all__ = ["hermes_state_db", "harvest_usage"]

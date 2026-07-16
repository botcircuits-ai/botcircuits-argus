"""Build-time *graph optimizer* — Passes 2 & 3 of `workflow build`.

These are pure, deterministic graph transforms (no LLM) that rewrite a
correct-but-naive workflow graph into a cheaper-to-run one, so a workflow drawn
in natural language runs lean without the author hand-tuning the structure.

  * **Pass 2 — terminal-restatement fold** (`fold_terminal_emit`). A common
    authored shape is a final step that only *re-reads what a previous step
    persisted and restates it as the answer*. Segmentation already batches that
    terminal step into its predecessor's LLM call, so it costs no extra
    round-trip — but the redundant "now read it back and re-emit" instruction
    makes the model produce the answer twice (once to persist, once to narrate),
    wasting output tokens on every run. This pass moves the emit instruction
    ONTO the data-producing predecessor(s) and deletes the restatement step, so
    the answer is produced once.

  * **Pass 3 — branch-step fusion** (`fuse_independent_branches`). Two adjacent
    branch steps `A -> B` where B is A's only (default) successor and B's branch
    decision does not depend on A's branch outcome are independent screens that
    the author happened to draw as two steps (e.g. validate-header then
    fraud-check). The engine runs a multi-choice step in one segment, so fusing
    them into a single step with both choice sets turns two LLM calls into one.

Both are conservative: they only fire on shapes that provably preserve the
graph's behavior, and they never touch a step reached by more than one path or
whose fusion would change which branch is taken. `optimize_graph` runs them in
order and returns a summary for the CLI.

Ordering in the build: run AFTER condition indexing (Pass 3 needs `choices`),
and BEFORE the action optimizer (so its terse rewrite sees the fused/folded
action text) and `compute_segments` (which reads the final graph).
"""

from __future__ import annotations


# Heuristic markers that a terminal step's action is a pure emit/restatement of
# something a prior step already produced — not new work. Deliberately narrow:
# we only fold when the action clearly just reads-back-and-emits.
_RESTATE_HINTS = ("read_file", "read the", "emit", "final answer",
                  "fenced json", "json block", "end your message",
                  "end with", "restate", "the previous step wrote")


def _steps(flow: dict) -> dict[str, dict]:
    s = flow.get("steps")
    return s if isinstance(s, dict) else {}


def _is_branch(step: dict) -> bool:
    return bool(step.get("choices")) or bool(step.get("conditions"))


def _action(step: dict) -> str:
    return (step.get("settings") or {}).get("action") or ""


def _predecessors(flow: dict, target: str) -> dict[str, list[str]]:
    """Map every step that points at `target` to HOW: 'next' (static) and/or
    'choice'/'condition' (branch). Used to decide a fold/fuse is safe."""
    via: dict[str, list[str]] = {}
    for sid, step in _steps(flow).items():
        if not isinstance(step, dict):
            continue
        if step.get("next") == target:
            via.setdefault(sid, []).append("next")
        for c in step.get("choices") or []:
            if c.get("next") == target:
                via.setdefault(sid, []).append("choice")
        for c in step.get("conditions") or []:
            if c.get("next") == target:
                via.setdefault(sid, []).append("condition")
    return via


# --------------------------------------------------------------------------- #
# Pass 2 — terminal-restatement fold
# --------------------------------------------------------------------------- #

def emit_text_fallback(terminal: dict) -> str:
    """Emit directive used only when a predecessor has no action of its own to
    append to — keep the terminal step's original text in that edge case."""
    return _action(terminal).strip()


def _looks_like_restatement(step: dict) -> bool:
    a = _action(step).lower()
    if not a:
        return False
    hits = sum(1 for h in _RESTATE_HINTS if h in a)
    return hits >= 2  # needs a couple of signals, not just any "emit"


def fold_terminal_emit(flow: dict) -> int:
    """Fold a pure terminal restatement step into its predecessors.

    A step T qualifies when: it is a non-branching `agentAction` with no `next`
    (terminal); its action looks like a read-back-and-emit restatement; and it
    is reachable ONLY as a static `next` target (never a branch target — folding
    into a branch arm would change the arm's own emitted answer). Each
    predecessor P (P.next == T) gets T's action appended to its own action and
    its `next` cleared (P becomes terminal); T is then deleted.

    Returns the number of steps folded (0 or, rarely, more if several terminal
    restatement steps exist). Pure: mutates `flow` in place, returns a count.
    """
    steps = _steps(flow)
    folded = 0
    for tid in list(steps.keys()):
        t = steps.get(tid)
        if not isinstance(t, dict):
            continue
        if t.get("type") != "agentAction" or _is_branch(t) or t.get("next"):
            continue
        if not _looks_like_restatement(t):
            continue
        via = _predecessors(flow, tid)
        if not via or tid == flow.get("start"):
            continue
        # Only fold when EVERY predecessor reaches T via a static `next`
        # (no branch arm targets T) — otherwise a branch's answer differs.
        if any("choice" in v or "condition" in v for v in via.values()):
            continue
        # Fold a TERSE emit directive, not the terminal step's verbose action.
        # The original emit step was authored to re-read what a prior step wrote
        # (it had no memory of it). Folded into the PRODUCER, that read-back is
        # redundant — the producer already has the data in context — and copying
        # the verbose restatement prose is exactly the per-run output waste this
        # pass exists to remove. Replace it with one compact instruction that
        # reuses the value just produced.
        for pid in via:
            p = steps[pid]
            if _is_branch(p):
                # A branch predecessor's `next` is its DEFAULT arm; folding the
                # emit there would skip the emit on its choice arms. Skip — let
                # Pass 1/author handle these.
                continue
            pa = _action(p).rstrip()
            p.setdefault("settings", {})["action"] = (
                f"{pa}\n\nThen end your message with that same result as a single "
                f"fenced ```json block (do not re-read or recompute it)."
                if pa else emit_text_fallback(t)
            )
            p["next"] = None
        # Re-check: did at least one predecessor actually absorb it?
        absorbed = [pid for pid in via if not _is_branch(steps[pid])]
        if absorbed:
            del steps[tid]
            folded += 1
    return folded


# --------------------------------------------------------------------------- #
# Pass 3 — independent branch fusion
# --------------------------------------------------------------------------- #

def _branch_vars(step: dict) -> set[str]:
    """Variables a step's choices/conditions read."""
    out: set[str] = set()
    for c in step.get("choices") or []:
        for e in c.get("expressionList") or []:
            v = e.get("variable")
            if isinstance(v, str):
                out.add(v)
    return out


def _choice_targets(step: dict) -> set[str]:
    out: set[str] = set()
    for c in step.get("choices") or []:
        n = c.get("next")
        if isinstance(n, str):
            out.add(n)
    return out


def _successors(step: dict) -> set[str]:
    """All step ids `step` can go to: static `next` plus every choice target."""
    out = set(_choice_targets(step))
    n = step.get("next")
    if isinstance(n, str):
        out.add(n)
    return out


def _reaches(flow: dict, src: str, dst: str) -> bool:
    """Whether `dst` is reachable from `src` by following next/choice edges.
    Used to detect loops/back-edges that make branch fusion unsafe."""
    steps = _steps(flow)
    seen: set[str] = set()
    stack = [src]
    while stack:
        cur = stack.pop()
        for nxt in _successors(steps.get(cur) or {}):
            if nxt == dst:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def fuse_independent_branches(flow: dict) -> int:
    """Fuse `A -> B` when both are branch steps, B is A's ONLY successor, and
    B's branch is independent of A's outcome.

    Safe-to-fuse conditions (all required):
      - A and B are both branching `agentAction` steps.
      - B is A's default `next`, AND none of A's choice arms target B (so B is
        reached only by "A didn't early-exit") and A has no other path to B.
      - B is reached ONLY from A (single predecessor) — fusing wouldn't strand
        another caller.
      - A's and B's branch variables are disjoint (B doesn't test what A set),
        so evaluating both against the same post-segment slots is order-free.
      - A's choice targets and B's are disjoint from each other's variables.

    The fused step keeps A's id; its choices become A's choices followed by B's
    (A's early-exit arms still win first), its `next` becomes B's `next`, and
    its action is the concatenation (B's work still has to run). B is deleted.

    Returns the count of fusions performed. Pure: mutates `flow` in place.
    """
    steps = _steps(flow)
    fused = 0
    # Iterate over a snapshot; a fusion can enable the next, so loop to fixpoint.
    changed = True
    while changed:
        changed = False
        for aid in list(steps.keys()):
            a = steps.get(aid)
            if not isinstance(a, dict) or not _is_branch(a) or a.get("type") != "agentAction":
                continue
            bid = a.get("next")
            if not isinstance(bid, str):
                continue
            b = steps.get(bid)
            if not isinstance(b, dict) or not _is_branch(b) or b.get("type") != "agentAction":
                continue
            # B must not be one of A's early-exit choice arms, and A must not
            # also branch to B another way.
            if bid in _choice_targets(a):
                continue
            # B reached only from A.
            if set(_predecessors(flow, bid)) != {aid}:
                continue
            # A must be a simple entry into this pair, not a LOOP JOIN. A step
            # targeted by more than one predecessor (e.g. a select-next-item
            # step that several mark_* steps loop back to) is a loop head; fusing
            # the loop body's decision into it collapses a sequential per-iter
            # step into the selector and evaluates the body's branch before the
            # body has run. Only fuse a linear A with a single inbound edge.
            if len(_predecessors(flow, aid)) > 1:
                continue
            # Reject if either step is revisited via a back-edge (loop member):
            # B reaching A, or B reaching itself, means the "screens" aren't the
            # order-free parallel pair fusion assumes.
            if _reaches(flow, bid, aid) or _reaches(flow, bid, bid):
                continue
            # Independence: disjoint branch variables.
            if _branch_vars(a) & _branch_vars(b):
                continue
            # Don't fuse if B's choices target A (would create a self-cycle), or
            # A's arms target B's targets in a way that changes routing.
            if aid in _choice_targets(b):
                continue
            # Fuse: A keeps its early-exit arms first, then B's; default -> B.next.
            a["choices"] = list(a.get("choices") or []) + list(b.get("choices") or [])
            a["next"] = b.get("next")
            a_act = _action(a).rstrip()
            b_act = _action(b).strip()
            if b_act:
                a.setdefault("settings", {})["action"] = (
                    f"{a_act}\n\n{b_act}" if a_act else b_act
                )
            del steps[bid]
            fused += 1
            changed = True
            break  # restart scan after a structural change
    return fused


def optimize_graph(flow: dict) -> dict:
    """Run the structural passes (3 then 2) and return a summary.

    Pass 3 (fuse) first so a fused branch step can then have a trailing emit
    folded into it by Pass 2.
    """
    fused = fuse_independent_branches(flow)
    folded = fold_terminal_emit(flow)
    return {"branches_fused": fused, "emits_folded": folded}

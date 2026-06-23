"""Report rendering — JSON + human-readable text.

Both formats are derived from the same `DatasetReport` produced by the
harness. The text format is intentionally compact: it's read in a
terminal, so we lean on tables over prose.

Column semantics (post-Agent-driven refactor):
  - `workflow_on`  — Agent with the workflow tool registered + enabled.
  - `workflow_off` — Agent with the workflow tool hidden + the
                     dataset's `workflow_spec` injected as system
                     instructions.
"""

from __future__ import annotations

import json
from pathlib import Path

from botcircuits.agent.workflow.evaluation.metrics import DatasetReport


# Max chars to print from an agent's final assistant text in the
# per-case detail block. Keep it tight — terminals scroll.
_FINAL_TEXT_PREVIEW = 240


def _truncate(s: str, n: int = _FINAL_TEXT_PREVIEW) -> str:
    s = (s or "").strip().replace("\n", " ⏎ ")
    if len(s) <= n:
        return s
    return s[:n] + "…"


def _fmt_usage(usage: dict) -> str:
    """One-line token summary with the per-purpose breakdown (§7)."""
    calls = usage.get("llm_calls", 0)
    inp = usage.get("input_tokens", 0)
    outp = usage.get("output_tokens", 0)
    cache = usage.get("cache_read_tokens", 0)
    head = f"{calls} calls, in={inp} out={outp} cache_read={cache}"
    by_purpose = usage.get("by_purpose") or {}
    if by_purpose:
        parts = [
            f"{p}:{b.get('calls', 0)}c/{b.get('input', 0)}in"
            for p, b in sorted(by_purpose.items())
        ]
        head += "  [" + ", ".join(parts) + "]"
    return head


def render_text(report: DatasetReport) -> str:
    """Compact two-column comparison + per-case detail."""
    wf = report.workflow_summary
    pr = report.prompt_summary

    def _row(label: str, w: object, p: object) -> str:
        return f"  {label:<22} {w!s:<14} {p!s:<14}"

    lines: list[str] = []
    lines.append(
        "Evaluation report — Agent with workflow vs Agent without workflow "
        "(spec as system prompt)"
    )
    lines.append(f"  cases: {wf.cases}")
    lines.append("")
    lines.append(f"  {'metric':<22} {'workflow_on':<14} {'workflow_off':<14}")
    lines.append(f"  {'-'*22} {'-'*14} {'-'*14}")
    lines.append(_row("accuracy (mean)",
                      f"{wf.accuracy_mean:.3f}",
                      f"{pr.accuracy_mean:.3f}"))
    lines.append(_row("contains match",
                      f"{wf.contains_match_rate:.3f}",
                      f"{pr.contains_match_rate:.3f}"))
    lines.append(_row("final-state match",
                      f"{wf.final_match_rate:.3f}",
                      f"{pr.final_match_rate:.3f}"))
    lines.append(_row("consistency (mean)",
                      f"{wf.consistency_mean:.3f}",
                      f"{pr.consistency_mean:.3f}"))
    lines.append("")

    lines.append("Per-case results:")
    for c in report.per_case:
        cid = c["case_id"]
        wf_run = c.get("workflow_run") or {}
        pr_run = c.get("prompt_run")
        wf_a = wf_run.get("accuracy") or {}
        pr_a = pr_run.get("accuracy") if pr_run else None
        wf_score = wf_a.get("score")
        pr_score = pr_a.get("score") if pr_a else None
        wf_score_s = f"{wf_score:.2f}" if isinstance(wf_score, float) else "n/a"
        pr_score_s = (
            f"{pr_score:.2f}" if isinstance(pr_score, float) else "n/a"
        )
        mode = c.get("mode", "referenced")
        runtime_name = c.get("workflow", "")
        lines.append(
            f"  - {cid:<40} workflow_on={wf_score_s}  workflow_off={pr_score_s}  "
            f"({mode}{', ' + runtime_name if mode == 'inline' else ''})"
        )
        if mode == "inline":
            inline = c.get("inline") or {}
            be = inline.get("build_error")
            if be:
                lines.append(f"      BUILD FAILED: {be}")
            cleaned = inline.get("cleaned_paths") or []
            if cleaned:
                lines.append(f"      cleaned: {len(cleaned)} file(s)")
            elif inline.get("kept"):
                lines.append(
                    f"      kept: workflow {inline.get('built_as')!r} left "
                    f"on disk (pass --cleanup-inline-workflow to remove)"
                )
        wf_tools = wf_run.get("tool_calls") or []
        wf_invocations = wf_run.get("workflow_invocations", 0)
        lines.append(
            f"      workflow_on  reply: {_truncate(wf_run.get('final_text', ''))}"
        )
        lines.append(
            f"      workflow_on  tools: {wf_tools} "
            f"(workflow invocations: {wf_invocations})"
        )
        wf_usage = wf_run.get("usage") or {}
        if wf_usage.get("llm_calls"):
            lines.append(f"      workflow_on  tokens: {_fmt_usage(wf_usage)}")
        if wf_run.get("error"):
            lines.append(f"      workflow_on  error: {wf_run['error']}")
        if pr_run:
            lines.append(
                f"      workflow_off reply: {_truncate(pr_run.get('final_text', ''))}"
            )
            pr_tools = pr_run.get("tool_calls") or []
            lines.append(f"      workflow_off tools: {pr_tools}")
            pr_usage = pr_run.get("usage") or {}
            if pr_usage.get("llm_calls"):
                lines.append(f"      workflow_off tokens: {_fmt_usage(pr_usage)}")
            if pr_run.get("error"):
                lines.append(f"      workflow_off error: {pr_run['error']}")

        # Third column (§7): the legacy per-step workflow-as-tool path.
        legacy = c.get("workflow_as_tool_run")
        if legacy:
            lg_score = (legacy.get("score") or {}).get("score")
            lg_score_s = f"{lg_score:.2f}" if isinstance(lg_score, float) else "n/a"
            lines.append(
                f"      legacy(wf-as-tool) score={lg_score_s}  "
                f"reply: {_truncate(legacy.get('final_action', ''))}"
            )
            if legacy.get("error"):
                lines.append(f"      legacy(wf-as-tool) error: {legacy['error']}")

    return "\n".join(lines)


def write_json_report(report: DatasetReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(report.to_dict(), f, indent=2, ensure_ascii=False)
        f.write("\n")

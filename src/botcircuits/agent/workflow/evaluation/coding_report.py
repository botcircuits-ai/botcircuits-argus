"""Render a coding evaluation as a three-column text table."""

from __future__ import annotations

import json
from pathlib import Path

from botcircuits.agent.workflow.evaluation.coding_metrics import (
    CodingReport,
    ModeSummary,
)

_MODE_LABEL = {
    "pipeline": "pipeline",
    "native": "native",
    "claude_cli": "claude-cli",
}


def _pct(x: float) -> str:
    return f"{x * 100:4.0f}%"


def _row(label: str, cells: list[str], width: int) -> str:
    body = "".join(c.rjust(width) for c in cells)
    return f"  {label:<20}{body}"


def render_coding_report(report: CodingReport) -> str:
    modes = list(report.mode_summaries.keys())
    summaries: list[ModeSummary] = [report.mode_summaries[m] for m in modes]
    width = 13

    lines: list[str] = []
    lines.append("")
    lines.append(f"Coding evaluation — dataset '{report.dataset}' "
                 f"({report.repeats} repeat(s) per case × mode)")
    lines.append("")

    header = _row("metric", [_MODE_LABEL.get(m, m) for m in modes], width)
    lines.append(header)
    lines.append("  " + "-" * (20 + width * len(modes)))

    def metric_row(label: str, fmt) -> str:
        return _row(label, [fmt(s) for s in summaries], width)

    lines.append(metric_row("tests pass",
                            lambda s: _pct(s.tests_pass_rate)))
    lines.append(metric_row("no regressions",
                            lambda s: _pct(s.no_regression_rate)))
    lines.append(metric_row("judge (0-1)",
                            lambda s: f"{s.judge_mean:5.2f}"))
    lines.append(metric_row("consistency",
                            lambda s: _pct(s.consistency_mean)))
    lines.append(metric_row("avg in-tok",
                            lambda s: f"{int(s.avg_input_tokens)}"))
    lines.append(metric_row("avg out-tok",
                            lambda s: f"{int(s.avg_output_tokens)}"))
    lines.append(metric_row("avg llm calls",
                            lambda s: f"{s.avg_llm_calls:.1f}"))
    lines.append(metric_row("avg latency s",
                            lambda s: f"{s.avg_elapsed_s:.1f}"))
    lines.append(metric_row("errors",
                            lambda s: f"{s.errors}/{s.runs}"))
    lines.append("")

    # Per-case, per-mode pass grid (quick eyeball of which task each mode won).
    lines.append("  per-case tests-pass (fraction of repeats):")
    per_case: dict[str, dict[str, list[bool]]] = {}
    for run in report.per_run:
        cid = run["case_id"]
        per_case.setdefault(cid, {}).setdefault(run["mode"], []).append(
            bool(run["tests_pass"]))
    for cid in sorted(per_case):
        cells = []
        for m in modes:
            flags = per_case[cid].get(m, [])
            cells.append(f"{sum(flags)}/{len(flags)}" if flags else "-")
        lines.append(_row(cid, cells, width))
    lines.append("")
    return "\n".join(lines)


def write_coding_report(report: CodingReport, path: Path) -> None:
    path.write_text(json.dumps(report.to_dict(), indent=2, ensure_ascii=False)
                    + "\n", encoding="utf-8")


__all__ = ["render_coding_report", "write_coding_report"]

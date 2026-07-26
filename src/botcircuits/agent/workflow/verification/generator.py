"""Build-time verification-gate generation.

`generate_gate` is the ONLY "random"/generated part of the verification
gate framework — it asks an LLM to look at a just-built workflow's shape
(`flow.result`, `flow.variables`, its steps) and propose a small set of
checks that would catch a wrong or incomplete run. The executor those
checks run through (`executor.py::run_gate`) never changes; only this
generation step varies per workflow.

Called from both build entrypoints (`build_workflow` tool and the
`workflow build` CLI command) right after a successful build. Callers
treat generation as best-effort: a failure here must never turn a
successful workflow build into a failed one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from botcircuits.agent.workflow._json_repair import extract_json
from botcircuits.providers.base import LLMProvider
from botcircuits.types import Message

from ..paths import gate_checks_dir, gate_manifest_path, verifications_dir_for

_SYSTEM = (
    "You design verification checks for an automated workflow runner. "
    "Output ONLY strict JSON matching the requested schema — no "
    "commentary, no markdown fences."
)

_MIN_CHECKS = 2
_MAX_CHECKS = 5

_PROMPT_TEMPLATE = """\
A workflow named {name!r} was just built. Here is its flow definition \
(steps, declared result shape, and variable schema):

{flow_json}

Propose between {min_checks} and {max_checks} verification checks that \
would catch a wrong or incomplete run of this workflow, evaluated \
against the run's final `slots` / declared `result` / `decisions`. \
Prefer a "script" check (deterministic Python) wherever the declared \
output shape makes a checkable invariant expressible (e.g. numeric \
totals that should sum, a required field that must be non-empty, a \
count that should match an input list's length). Use "llm_judge" only \
for inherently subjective criteria (tone, policy compliance, whether \
free-text output actually addresses the request).

Return JSON of this exact shape:
{{
  "checks": [
    {{
      "id": "<short_snake_case_id>",
      "type": "script",
      "description": "<what this check verifies>",
      "severity": "blocking" | "advisory",
      "code": "<complete Python source. Reads one JSON object from \
stdin shaped {{workflow_name, slots, summary, decisions, done}}. Must \
print exactly one line of JSON {{\\"passed\\": bool, \\"detail\\": \
str}} to stdout and exit 0. Do not read/write any file, network, or \
env var — only stdin/stdout.>"
    }},
    {{
      "id": "<short_snake_case_id>",
      "type": "llm_judge",
      "description": "<what this check verifies>",
      "severity": "blocking" | "advisory",
      "rubric": "<natural-language judging criteria>",
      "inputs": ["<dotted path into the run record, e.g. slots.some_field>"]
    }}
  ]
}}
"""


def _flow_context(flow: dict) -> dict:
    """Trim the built flow to what's useful for gate generation — the
    step map (types + actions, not the full expression internals),
    the declared result shape, and the variable schema. Keeps the
    prompt focused and avoids leaking `choices`/`segments` noise that
    doesn't help a check-design decision."""
    steps = {}
    for sid, step in (flow.get("steps") or {}).items():
        if not isinstance(step, dict):
            continue
        settings = step.get("settings") or {}
        steps[sid] = {
            "type": step.get("type"),
            "action": settings.get("action"),
        }
    return {
        "steps": steps,
        "result": flow.get("result"),
        "variables": flow.get("variables"),
    }


def _validate_checks(checks: Any) -> list[dict]:
    """Filter the model's proposed checks down to well-formed entries.
    Malformed entries are dropped rather than failing generation
    outright — a partial, valid gate is better than none."""
    if not isinstance(checks, list):
        return []
    out: list[dict] = []
    for c in checks:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        ctype = c.get("type")
        severity = c.get("severity") if c.get("severity") in ("blocking", "advisory") else "advisory"
        if not isinstance(cid, str) or not cid:
            continue
        if ctype == "script":
            code = c.get("code")
            if not isinstance(code, str) or not code.strip():
                continue
            out.append({
                "id": cid, "type": "script", "severity": severity,
                "description": c.get("description") or "",
                "code": code,
            })
        elif ctype == "llm_judge":
            rubric = c.get("rubric")
            if not isinstance(rubric, str) or not rubric.strip():
                continue
            inputs = c.get("inputs") if isinstance(c.get("inputs"), list) else []
            out.append({
                "id": cid, "type": "llm_judge", "severity": severity,
                "description": c.get("description") or "",
                "rubric": rubric,
                "inputs": [p for p in inputs if isinstance(p, str)],
            })
    return out[:_MAX_CHECKS]


async def generate_gate(
    flow: dict,
    workflow_name: str,
    provider: LLMProvider,
) -> dict:
    """Ask `provider` to design a gate for `flow`, write it to
    `.build/<workflow_name>/verifications/` (manifest + any script check
    sources), and return the manifest dict.

    Raises on any failure (LLM call, JSON parse, empty check list, disk
    write) — callers are responsible for catching this and treating it
    as non-fatal to the overall build, per this module's docstring.
    """
    prompt = _PROMPT_TEMPLATE.format(
        name=workflow_name,
        flow_json=json.dumps(_flow_context(flow), indent=2, ensure_ascii=False),
        min_checks=_MIN_CHECKS,
        max_checks=_MAX_CHECKS,
    )
    messages = [Message(role="user", blocks=[{"type": "text", "text": prompt}])]
    response = await provider.complete(
        system=_SYSTEM, messages=messages, tools=[], hosted_mcp=[],
        skills=[], max_tokens=8192,
    )
    parsed = extract_json(response.text or "")
    checks = _validate_checks((parsed or {}).get("checks"))
    if not checks:
        raise RuntimeError(
            f"gate generation for {workflow_name!r} produced no valid checks"
        )

    verifications_dir = verifications_dir_for(workflow_name)
    checks_dir = gate_checks_dir(workflow_name)
    checks_dir.mkdir(parents=True, exist_ok=True)

    manifest_checks: list[dict] = []
    for check in checks:
        if check["type"] == "script":
            script_name = f"{check['id']}.py"
            (checks_dir / script_name).write_text(check["code"], encoding="utf-8")
            manifest_checks.append({
                "id": check["id"],
                "type": "script",
                "description": check["description"],
                "script": f"verifications/checks/{script_name}",
                "severity": check["severity"],
            })
        else:
            manifest_checks.append({
                "id": check["id"],
                "type": "llm_judge",
                "description": check["description"],
                "rubric": check["rubric"],
                "inputs": check["inputs"],
                "severity": check["severity"],
            })

    manifest = {
        "workflow_name": workflow_name,
        "version": 1,
        "checks": manifest_checks,
    }
    manifest_path = gate_manifest_path(workflow_name)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return manifest


def load_gate(workflow_name: str) -> dict | None:
    """Read `.build/<workflow_name>/verifications/gate.json` if it
    exists, else None. `None` means "this workflow has no gate" — the
    runtime treats that as zero overhead, not an error."""
    path = gate_manifest_path(workflow_name)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

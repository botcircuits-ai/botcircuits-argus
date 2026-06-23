#!/usr/bin/env python3
"""Measure the REAL token cost of AUTHORING a workflow from its NL spec.

`workflow generate` drives an LLM to turn a TASK.md into a workflow JSON. Under
the claude-code runtime that work bills tokens inside the spawned `claude -p`
calls, but `CliLLMProvider` discards their usage (the host CLI bills its own
tokens). This script runs the same generate pipeline and recovers the per-call
usage from each call's JSON stdout (`usage_from_stdout`), summing it into the
one-time authoring footprint for a workflow.

It prints ONE JSON object so an evaluator can fold authoring tokens in alongside
the per-run usage it already captures:

    {"name": "<wf>", "usage": {input_tokens, output_tokens,
                               cache_read_tokens, cache_write_tokens, llm_calls}}

Usage:
    python scripts/authoring_usage.py --name <wf> --from <TASK.md> \\
        [--resources <manifest>]

Run from the botcircuits-agent repo (cwd) so the claude-code runtime is detected
and file-path checks resolve. The generated draft is written to a throwaway temp
workflows dir (BOTCIRCUITS_WORKFLOWS_DIR) so it never clobbers a hand-authored
workflow.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="authoring_usage")
    ap.add_argument("--name", required=True, help="workflow name (temp draft)")
    ap.add_argument("--from", dest="from_file", required=True,
                    help="NL spec (TASK.md / instructions)")
    ap.add_argument("--resources", default=None,
                    help="optional resources manifest passed to the generator")
    args = ap.parse_args(argv)

    from_path = Path(args.from_file).expanduser()
    if not from_path.is_file():
        print(json.dumps({"error": f"--from not found: {from_path}"}))
        return 2
    instructions = from_path.read_text()
    resources = ""
    if args.resources:
        rp = Path(args.resources).expanduser()
        if rp.is_file():
            resources = rp.read_text()

    from botcircuits.agent.workflow.generator import generate_workflow
    from botcircuits.runtime.detect import (
        NATIVE, detect_runtime_name, runtime_config,
    )
    from botcircuits.runtime.cli_llm_provider import CliLLMProvider
    from botcircuits.usage.run_usage import usage_from_stdout

    rt = detect_runtime_name(settings=None)
    if rt == NATIVE:
        print(json.dumps({"error": "no host CLI runtime detected (need claude-code)"}))
        return 1
    provider = CliLLMProvider(runtime_config(rt, settings=None))

    # Recover usage from each generate call's JSON stdout (the provider discards
    # it). Wrap complete() to parse + sum per call.
    tot = {"input_tokens": 0, "output_tokens": 0,
           "cache_read_tokens": 0, "cache_write_tokens": 0, "llm_calls": 0}
    _orig = provider.complete

    async def _wrapped(*a, **k):
        resp = await _orig(*a, **k)
        u = usage_from_stdout(getattr(resp, "raw", "") or "")
        if u:
            d = u.to_dict() if hasattr(u, "to_dict") else dict(u)
            for key in ("input_tokens", "output_tokens",
                        "cache_read_tokens", "cache_write_tokens"):
                tot[key] += int(d.get(key, 0) or 0)
            tot["llm_calls"] += 1
        return resp

    provider.complete = _wrapped  # type: ignore[method-assign]

    # Author into a throwaway workflows dir so we never touch real workflows.
    import os
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["BOTCIRCUITS_WORKFLOWS_DIR"] = tmp
        try:
            asyncio.run(generate_workflow(
                instructions, args.name, provider, resources,
                validate_loop=0, base_dir=Path.cwd(),
            ))
        except Exception as e:  # noqa: BLE001 - report, don't crash the eval
            print(json.dumps({"name": args.name, "error":
                              f"{type(e).__name__}: {e}", "usage": tot}))
            return 1
        finally:
            try:
                asyncio.run(provider.aclose())
            except Exception:
                pass

    print(json.dumps({"name": args.name, "usage": tot}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

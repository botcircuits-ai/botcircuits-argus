"""Coding-eval runner: drive one coding case in one MODE against a sandbox.

Three modes compared:

  - ``pipeline``   — the BotCircuits Agent with `enable_coding_pipeline=True`.
                     A coding request routes into `safe_agentic_workflow`.
  - ``native``     — the SAME Agent with `enable_coding_pipeline=False` and NO
                     injected procedure: raw request only (true freewheeling).
  - ``claude_cli`` — the real `claude` CLI (claude-code runtime) driving the
                     task agentically in the sandbox via `run_cli`.

Each run: copy the fixture into a throwaway git sandbox → drive the mode
(mutating the process cwd, so runs are SERIAL) → capture the diff + usage →
score objectively (and optionally by LLM judge) → tear down.

Modeled on `runner_agent.py::_drive_agent` (shared `_NonClosingProvider`,
`_usage_snapshot`/`_apply_usage_delta`) for the two agent modes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from botcircuits.providers.base import LLMProvider
from botcircuits.agent.workflow.evaluation.coding_dataset import (
    CodingCase,
    CodingDataset,
    fixture_path,
)
from botcircuits.agent.workflow.evaluation.coding_scorers import (
    JudgeScore,
    ObjectiveScore,
    run_judge,
    run_objective,
)
from botcircuits.agent.workflow.evaluation.runner_agent import (
    _NonClosingProvider,
    _apply_usage_delta,
    _usage_snapshot,
    AgentRunResult,
)

MODES = ("pipeline", "native", "claude_cli")

#: Step budget for the agent modes — a coding task with the pipeline needs many
#: tool rounds (generate + run the sub-workflow, validate, loop).
_MAX_STEPS = 60


@dataclass
class CodingRunResult:
    case_id: str
    fixture: str
    mode: str
    objective: ObjectiveScore | None = None
    judge: JudgeScore | None = None
    final_text: str = ""
    diff: str = ""
    tool_calls: list[str] = field(default_factory=list)
    workflow_invocations: int = 0
    error: str | None = None
    elapsed_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    llm_calls: int = 0
    usage_by_purpose: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        obj = self.objective
        jdg = self.judge
        return {
            "case_id": self.case_id,
            "fixture": self.fixture,
            "mode": self.mode,
            "tests_pass": obj.tests_pass if obj else None,
            "no_regressions": obj.no_regressions if obj else None,
            "judge_score": jdg.score if jdg else None,
            "judge_reason": jdg.reason if jdg else "",
            "workflow_invocations": self.workflow_invocations,
            "tool_calls": self.tool_calls,
            "elapsed_s": round(self.elapsed_s, 2),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "llm_calls": self.llm_calls,
            "usage_by_purpose": self.usage_by_purpose,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------


def _make_sandbox(src: Path) -> Path:
    """Copy the fixture repo into a fresh temp dir and `git init` it, so the
    post-run diff is capturable and cleanup is one rmtree. Caller removes it."""
    dst = Path(tempfile.mkdtemp(prefix="botcircuits-coding-eval-"))
    # copytree needs the leaf to not exist; use a child dir.
    work = dst / "repo"
    shutil.copytree(src, work)
    try:
        subprocess.run(["git", "init", "-q"], cwd=work, check=False,
                       capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=work, check=False,
                       capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=eval@local", "-c", "user.name=eval",
             "commit", "-q", "-m", "seed"],
            cwd=work, check=False, capture_output=True,
        )
    except Exception:
        pass  # diff capture is best-effort; scoring uses tests, not the diff
    return work


def _capture_diff(sandbox: Path) -> str:
    try:
        r = subprocess.run(["git", "diff", "HEAD"], cwd=sandbox, check=False,
                           capture_output=True, text=True)
        return r.stdout or ""
    except Exception:
        return ""


def _cleanup_sandbox(sandbox: Path) -> None:
    # sandbox is <tmp>/repo — remove the whole tmp parent.
    try:
        shutil.rmtree(sandbox.parent, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Mode drivers
# ---------------------------------------------------------------------------


async def _drive_agent_mode(
    *,
    case: CodingCase,
    sandbox: Path,
    provider: LLMProvider,
    enable_coding_pipeline: bool,
) -> AgentRunResult:
    """Drive the BotCircuits Agent (pipeline OR native) in `sandbox`.

    The agent's file tools operate on the process cwd, so we chdir into the
    sandbox for the duration. `agents_dir=sandbox` points the verification gate
    at the fixture's AGENTS.md test command. `BOTCIRCUITS_WORKFLOWS_DIR` is
    forced ABSOLUTE (repo workflows) so the pipeline can still load
    safe_agentic_workflow and write generated coding workflows to the real
    workflows dir — not into the sandbox.
    """
    from botcircuits.agent import Agent, default_registry
    from botcircuits.agent.workflow import register_workflows

    mode_label = "pipeline" if enable_coding_pipeline else "native"
    out = AgentRunResult(case_id=case.id, workflow="safe_agentic_workflow",
                         mode=mode_label)

    usage_before = _usage_snapshot(provider)
    prev_cwd = os.getcwd()
    started = time.perf_counter()
    try:
        registry = default_registry(
            {
                "shell_exec": {"auto": True},
                "write_file": {"auto": True},
                "edit_file": {"auto": True},
                "shell_stop": {"auto": True},
                "plan_and_confirm": {"auto": True},
            },
            provider=provider,
        )
        # The pipeline mode needs the workflow tools registered so the coding
        # route can fire safe_agentic_workflow; native does not.
        if enable_coding_pipeline:
            await register_workflows(registry, provider=provider,
                                     normalize_enabled=True)

        agent = Agent(
            provider=_NonClosingProvider(provider),
            tools=registry,
            enable_workflows=enable_coding_pipeline,
            enable_coding_pipeline=enable_coding_pipeline,
            max_steps=_MAX_STEPS,
            agents_dir=str(sandbox),
        )
        os.chdir(sandbox)
        async with agent:
            final_text, sid = await agent.chat(case.prompt)
            out.final_text = final_text
            if sid:
                convo = agent.store._sessions.get(sid)
                if convo is not None:
                    for m in convo.messages:
                        for b in m.blocks:
                            if b.get("type") == "tool_call":
                                name = b.get("name") or ""
                                out.tool_calls.append(name)
                                if name == "safe_agentic_workflow":
                                    out.workflow_invocations += 1
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"
    finally:
        os.chdir(prev_cwd)

    _apply_usage_delta(out, provider, usage_before)
    out.elapsed_s = time.perf_counter() - started
    return out


async def _drive_claude_cli(
    *,
    case: CodingCase,
    sandbox: Path,
    timeout: float = 600.0,
) -> tuple[str, str, float, str | None]:
    """Drive the real claude-code CLI agentically in `sandbox`.

    Returns (final_text, raw_stdout, elapsed_s, error). Uses the runtime's argv
    template (`claude -p {prompt} ...`) via `run_cli` with cwd pinned to the
    sandbox so the CLI edits the fixture directly. `raw_stdout` is returned so
    the caller can parse token usage from the CLI's JSON output."""
    from botcircuits.runtime.detect import CLAUDE_CODE, runtime_config
    from botcircuits.runtime.cli_exec import CliExecError, run_cli
    from botcircuits.runtime.result import assistant_text_from_stdout

    config = runtime_config(CLAUDE_CODE, settings=None)
    # The eval sandbox is a throwaway temp copy, so the CLI must be allowed to
    # edit files without an interactive approval — otherwise every run reports
    # the fix but never applies it (Edit permission-denied) and scores 0,
    # making the comparison meaningless. `--dangerously-skip-permissions` is
    # the documented flag for exactly this ("recommended only for sandboxes").
    # Appended here (eval-only), NOT baked into the shared RuntimeConfig used
    # in production.
    command = list(config.command)
    if "--dangerously-skip-permissions" not in command:
        command.append("--dangerously-skip-permissions")
    started = time.perf_counter()
    try:
        result = await run_cli(
            command, case.prompt,
            cwd=str(sandbox), timeout=timeout,
        )
    except CliExecError as e:
        return "", "", time.perf_counter() - started, str(e)
    if result.timed_out:
        return "", "", time.perf_counter() - started, "claude CLI timed out"
    text = assistant_text_from_stdout(result.stdout) or result.stdout
    return text, result.stdout, time.perf_counter() - started, None


def _cli_usage(stdout: str) -> dict:
    """Best-effort token usage for the claude_cli column, parsed from the
    CLI's JSON stdout. Empty dict when nothing parses."""
    try:
        from botcircuits.usage.run_usage import usage_from_stdout
        u = usage_from_stdout(stdout)
        if u is None:
            return {}
        return {
            "input": getattr(u, "input_tokens", 0) or 0,
            "output": getattr(u, "output_tokens", 0) or 0,
        }
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Case × mode entry point
# ---------------------------------------------------------------------------


async def run_coding_case(
    case: CodingCase,
    dataset: CodingDataset,
    provider: LLMProvider | None,
    *,
    mode: str,
    judge: bool = True,
) -> CodingRunResult:
    """Run one coding `case` in one `mode` end to end: sandbox → drive → diff →
    objective score → optional judge → teardown.

    `provider` may be None ONLY for the claude_cli mode (it shells out); the
    agent modes and the judge require a provider.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {MODES}")

    src = fixture_path(dataset, case)
    if not src.is_dir():
        return CodingRunResult(
            case_id=case.id, fixture=case.fixture, mode=mode,
            error=f"fixture not found: {src}")

    sandbox = _make_sandbox(src)
    result = CodingRunResult(case_id=case.id, fixture=case.fixture, mode=mode)
    cli_stdout = ""
    try:
        if mode == "claude_cli":
            text, cli_stdout, elapsed, err = await _drive_claude_cli(
                case=case, sandbox=sandbox)
            result.final_text = text
            result.elapsed_s = elapsed
            result.error = err
            usage = _cli_usage(cli_stdout)
            result.input_tokens = usage.get("input", 0)
            result.output_tokens = usage.get("output", 0)
        else:
            agent_res = await _drive_agent_mode(
                case=case, sandbox=sandbox, provider=provider,
                enable_coding_pipeline=(mode == "pipeline"),
            )
            result.final_text = agent_res.final_text
            result.tool_calls = agent_res.tool_calls
            result.workflow_invocations = agent_res.workflow_invocations
            result.error = agent_res.error
            result.elapsed_s = agent_res.elapsed_s
            result.input_tokens = agent_res.input_tokens
            result.output_tokens = agent_res.output_tokens
            result.cache_read_tokens = agent_res.cache_read_tokens
            result.llm_calls = agent_res.llm_calls
            result.usage_by_purpose = agent_res.usage_by_purpose

        result.diff = _capture_diff(sandbox)

        # Objective score — the headline metric — always runs.
        result.objective = await run_objective(
            sandbox,
            test_command=case.test_command,
            guard_command=case.guard_command,
        )

        # LLM judge — secondary — only when asked and a provider is available.
        if judge and provider is not None:
            result.judge = await run_judge(
                provider,
                goal=case.goal or case.prompt,
                transcript=result.final_text,
                diff=result.diff,
                objective=result.objective,
            )
    finally:
        _cleanup_sandbox(sandbox)
    return result


__all__ = [
    "MODES",
    "CodingRunResult",
    "run_coding_case",
]

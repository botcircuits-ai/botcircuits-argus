"""`botcircuits-cli workflow ...` subcommands.

  workflow build --name <workflow_name>   Generate expressions + variables
                                          for the choice steps of a workflow
                                          and write them back to the file.
  workflow run --name <workflow_name> [--initial-args <json>] [--reply <text>]
                                          Run a built workflow: drive the
                                          deterministic engine and dispatch
                                          each step to the agent runtime.
  workflow eval [--dataset <path>] [--repeats N] [--report <path>]
                                          Run the evaluation framework
                                          comparing the STM engine to a
                                          prompt-only baseline on the
                                          dataset's cases.

The builder runs its LLM work (compiling conditions, optimizing actions,
authoring drafts) through the SAME host agent runtime that executes step
actions — claude-code, codex, … auto-detected via `select_runtime`. So no
third-party API key is needed and the inference quality matches the rest of
the session. When no host agent is detected (standalone CLI), it falls back
to the configured direct provider/model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from botcircuits.agent.workflow.action_optimizer import optimize_actions
from botcircuits.agent.workflow.condition_processor import generate_expressions_and_variables
from botcircuits.agent.workflow.engine.segments import compute_segments
from botcircuits.agent.workflow.graph_optimizer import optimize_graph
from botcircuits.agent.workflow.workflow_defaults import apply_defaults
from botcircuits.agent.workflow.evaluation import (
    EvalDatasetError,
    discover_datasets,
    load_dataset,
    render_text,
    resolve_eval_dir,
    run_evaluation_datasets,
    write_json_report,
)
from botcircuits.agent.workflow.local import (
    DEFAULT_WORKFLOWS_DIR,
    LocalWorkflowError,
    WORKFLOWS_DIR_ENV,
    _resolve_build_dir,
    _resolve_workflows_dir,
)
from botcircuits.cli.ansi import C, out
from botcircuits.cli.config import ConfigError


# ---------------------------------------------------------------------------
# Subparser wiring
# ---------------------------------------------------------------------------


def add_workflow_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Wire the `workflow` command and its sub-subcommands.

    Mirrors the layout of `add_mcp_subparser` so the two read consistently.
    """
    wf = subparsers.add_parser(
        "workflow",
        help="Manage local BotCircuits workflows (build conditions, etc.)",
    )
    wf_subs = wf.add_subparsers(dest="workflow_cmd", required=True)

    build_p = wf_subs.add_parser(
        "build",
        help="Convert natural-language conditions into rule-engine "
             "expressions and emit flow.variables for the given workflow.",
    )
    # The positional fallback keeps `workflow build <name>` working too.
    build_p.add_argument(
        "--name", dest="workflow_name", default=None,
        help="Workflow name (matches the file's `name` field or its "
             "filename stem in the workflows directory).",
    )
    build_p.add_argument(
        "workflow_name_pos", nargs="?", default=None,
        help=argparse.SUPPRESS,
    )
    build_p.add_argument(
        "--no-optimize", dest="no_optimize", action="store_true",
        help="Skip the action-optimizer pass (keep authored step action text "
             "verbatim). Optimization rewrites verbose actions into terse, "
             "tool-directed instructions to cut per-run tokens.",
    )

    run_p = wf_subs.add_parser(
        "run",
        help="Run a built workflow: drive the deterministic engine, "
             "dispatching each action step to the selected agent runtime. "
             "Pauses for human feedback; resume with --reply.",
    )
    run_p.add_argument(
        "--name", dest="workflow_name", default=None,
        help="Workflow name (the built workflow's `name` / filename stem).",
    )
    run_p.add_argument(
        "workflow_name_pos", nargs="?", default=None,
        help=argparse.SUPPRESS,
    )
    run_p.add_argument(
        "--initial-args", dest="initial_args", default="",
        help="JSON object of initial slot values to seed the run "
             "(e.g. '{\"order_id\": \"1024\"}').",
    )
    run_p.add_argument(
        "--runtime", dest="runtime_name", default=None,
        help="Force a runtime (claude-code, codex, …). Default: auto-detect.",
    )
    run_p.add_argument(
        "--reply", dest="reply", default=None,
        help="User's answer to a prior human-feedback pause; resumes the run.",
    )

    gen_p = wf_subs.add_parser(
        "generate",
        help="Generate an intent-only workflow SOURCE file from a "
             "natural-language description (then run `workflow build`).",
    )
    gen_p.add_argument(
        "--from", dest="from_file", required=True,
        help="Path to a plain-text/Markdown file describing the process.",
    )
    gen_p.add_argument(
        "--name", dest="workflow_name", required=True,
        help="Name for the generated workflow (file stem + tool name). Use a "
             "distinct name so it never overwrites a hand-authored workflow.",
    )
    gen_p.add_argument(
        "--resources", dest="resources_file", default=None,
        help="Path to a text manifest of workspace files/scripts the workflow "
             "may read or run (input record path, data files, scripts). Helps "
             "the generator wire deterministic resolvers/itemFacts instead of "
             "pausing to ask the user.",
    )
    gen_p.add_argument(
        "--validate-loop", dest="validate_loop", type=int, default=0,
        metavar="N",
        help="After generating, validate the draft and feed any problems back "
             "to the model to repair, up to N rounds (0 = off). Catches "
             "mis-wired itemSource paths, dict itemVariables, missing "
             "description, question-for-file-data, etc.",
    )
    gen_p.add_argument(
        "--dry-run-samples", dest="dry_run_samples", default=None,
        help="JSON file of [{input, expected}] cases. With --validate-loop, the "
             "draft is run on each input (deterministic item path) and any "
             "decision that mismatches `expected` is fed back to the model to "
             "repair — catches value-level wiring bugs static checks can't see.",
    )
    gen_p.add_argument(
        "--build", dest="also_build", action="store_true",
        help="Also run `workflow build` on the generated file immediately.",
    )

    eval_p = wf_subs.add_parser(
        "eval",
        help="Run the workflow evaluation framework: compare the STM "
             "engine against a prompt-only baseline on the dataset's "
             "cases, scoring accuracy and consistency.",
    )
    eval_p.add_argument(
        "--dataset", dest="eval_dataset", default=None,
        help="Path to a single dataset JSON file. When omitted, every "
             "*.json file under $BOTCIRCUITS_EVAL_DIR (or "
             ".botcircuits/evaluation) is loaded.",
    )
    eval_p.add_argument(
        "--repeats", dest="eval_repeats", type=int, default=3,
        help="How many times to run each case for consistency scoring "
             "(default 3). The workflow engine is deterministic so "
             "extra repeats only stress the prompt-only baseline.",
    )
    eval_p.add_argument(
        "--report", dest="eval_report", default=None,
        help="Optional path to write the full JSON report to. The "
             "summary table is always printed to stdout.",
    )
    eval_p.add_argument(
        "--skip-prompt-baseline", dest="eval_skip_prompt",
        action="store_true",
        help="Run only the workflow side. Useful for fast local checks "
             "that don't want to spend LLM credits on the baseline.",
    )
    eval_p.add_argument(
        "--cleanup-inline-workflow", dest="eval_cleanup_inline",
        action="store_true",
        help="After running an inline-build dataset, delete the "
             "generated workflow files (source and indexed build "
             "artifact). Off by default — the generated files are "
             "left on disk so you can inspect what the LLM authored "
             "and re-run the eval against the same workflow without "
             "rebuilding. Pass this flag when you want each run to "
             "leave the project clean.",
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def run_workflow_command(args: argparse.Namespace) -> int:
    """Entry point for `botcircuits-cli workflow ...`. Returns exit code."""
    if args.workflow_cmd == "build":
        return _cmd_build(args)
    if args.workflow_cmd == "run":
        return _cmd_run(args)
    if args.workflow_cmd == "generate":
        return _cmd_generate(args)
    if args.workflow_cmd == "eval":
        return _cmd_eval(args)

    out(C.red(f"[workflow] unknown subcommand: {args.workflow_cmd}"))
    return 2


# ---------------------------------------------------------------------------
# Subcommand bodies
# ---------------------------------------------------------------------------


def _make_dry_run(samples: list, base_dir):
    """Build an async `dry_run(doc) -> list[str]` for the generator's validate
    loop. For each sample {input, expected}, it stages the input where the
    draft's listDecision reads its items, runs the DETERMINISTIC item path, and
    returns a message for each per-item decision that doesn't match `expected`.

    Compares only the fields each `expected` item specifies (decision +
    line_total), keyed by sku/id — so extra fact fields don't cause false
    mismatches. All in-process, no LLM.
    """
    import copy
    import json as _json
    from pathlib import Path
    from botcircuits.agent.workflow.workflow_defaults import apply_defaults
    from botcircuits.agent.workflow.workflow_validator import dry_run_decisions

    def _id(d):
        for k in ("sku", "product_id", "id"):
            if d.get(k) not in (None, ""):
                return str(d[k]).strip()
        return ""

    def _num(v):
        try:
            return None if v is None else round(float(v), 2)
        except (TypeError, ValueError):
            return None

    async def _dry_run(doc: dict) -> list[str]:
        flow = copy.deepcopy(doc.get("flow") or {})
        apply_defaults(flow)
        # Where does the deciding listDecision read its items from?
        src = None
        for s in (flow.get("steps") or {}).values():
            if isinstance(s, dict) and s.get("type") == "listDecision" \
                    and s.get("itemFacts") and s.get("itemSource"):
                src = s["itemSource"]
                break
        if not src or not src.get("file"):
            return []  # nothing deterministic to dry-run
        order_path = base_dir / src["file"]
        problems: list[str] = []
        for case in samples:
            if not isinstance(case, dict):
                continue
            inp, expected = case.get("input"), case.get("expected") or []
            try:
                order_path.parent.mkdir(parents=True, exist_ok=True)
                order_path.write_text(_json.dumps(inp))
                got = dry_run_decisions(flow, base_dir=base_dir) or []
            except Exception as e:
                problems.append(f"On sample input, the workflow errored: {e}")
                continue
            got_by = {_id(g): g for g in got}
            for exp in expected:
                g = got_by.get(_id(exp))
                if g is None:
                    problems.append(
                        f"Item {_id(exp)!r}: expected a decision but the "
                        "workflow produced none for it.")
                    continue
                for k, ev in exp.items():
                    if k in ("sku", "product_id", "id"):
                        continue
                    gv = g.get(k)
                    same = (_num(gv) == _num(ev)) if isinstance(ev, (int, float)) \
                        else (str(gv).strip().lower() == str(ev).strip().lower())
                    if not same:
                        problems.append(
                            f"Item {_id(exp)!r}: expected {k}={ev!r} but the "
                            f"workflow decided {k}={gv!r}. Fix the condition "
                            "wiring (the fact derive and the branch test must "
                            "agree on value type/encoding).")
        # De-dupe while preserving order; cap so feedback stays focused.
        seen, uniq = set(), []
        for p in problems:
            if p not in seen:
                seen.add(p); uniq.append(p)
        return uniq[:12]

    return _dry_run


def _cmd_run(args: argparse.Namespace) -> int:
    """Run a built workflow through the deterministic engine + agent runtime.

    Thin CLI wrapper over `botcircuits.runtime.run_workflow._run` so the
    workflow-running skill can call one clean verb
    (`botcircuits workflow run --name <wf>`). The engine navigates the state
    machine and dispatches each step to the agent runtime; this command just
    starts it and prints the OUTCOME the calling agent reads:

      {"status": "success", "message": "<summary>"}   — workflow completed
      {"status": "failure", "message": "<reason>"}    — workflow could not run
      {"status": "paused",  "question": "<ask user>"} — needs human feedback

    `paused` is the only non-terminal outcome: the engine reached a step that
    needs the user. The caller relays the question and resumes with --reply.
    A `failure` is terminal — the caller surfaces it and does NOT retry.
    """
    from botcircuits.runtime.run_workflow import _run
    from botcircuits.agent.workflow.local import LocalWorkflowError

    def _fail(message: str, code: int) -> int:
        print(json.dumps({"status": "failure", "message": message}))
        return code

    workflow_name = args.workflow_name or args.workflow_name_pos
    if not workflow_name:
        return _fail("`run` requires --name=<workflow name>", 2)

    initial_args: dict = {}
    raw = (args.initial_args or "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return _fail(f"--initial-args not valid JSON: {e}", 2)
        if not isinstance(parsed, dict):
            return _fail("--initial-args must be a JSON object", 2)
        initial_args = parsed

    try:
        result = asyncio.run(_run(
            workflow_name,
            initial_args=initial_args,
            runtime_name=args.runtime_name,
            reply=args.reply,
        ))
    except LocalWorkflowError as e:
        return _fail(str(e), 1)
    except Exception as e:  # pragma: no cover - defensive top-level guard
        return _fail(f"{type(e).__name__}: {e}", 1)

    # Map the engine's internal result to the caller-facing outcome contract.
    # `usage` (real per-step + total tokens the run billed) rides along when the
    # runtime reported any; absent for runtimes that don't surface usage.
    status = result.get("status")
    usage = result.get("usage")
    if status == "paused":
        out_obj = {"status": "paused", "question": result.get("question") or ""}
        if usage:
            out_obj["usage"] = usage
        print(json.dumps(out_obj, ensure_ascii=False))
        return 0
    if status == "done":
        out_obj = {"status": "success", "message": result.get("summary") or ""}
        if usage:
            out_obj["usage"] = usage
        print(json.dumps(out_obj, ensure_ascii=False))
        return 0
    # `error` or anything unexpected → terminal failure.
    return _fail(result.get("error") or "workflow run failed", 1)


def _make_build_provider(cfg):
    """Pick the LLM that powers the build/generate pipeline.

    Prefer the host AGENT runtime already running this session (claude-code,
    codex, …) — the same way `workflow run` dispatches step actions via
    `select_runtime`. The build helpers then compile conditions / optimize
    actions / author drafts through that agent (no third-party API key), and
    quality matches the rest of the session.

    Falls back to the configured direct provider (`make_provider`) only when no
    host agent is detected — e.g. running the CLI standalone with an API key.
    Returns `(provider, label)`; `label` goes in the build log.
    """
    from .app import make_provider
    from botcircuits.runtime.detect import NATIVE, detect_runtime_name
    from botcircuits.runtime.cli_llm_provider import CliLLMProvider

    # Mirror run_workflow's detection (env markers + PATH probe; the
    # $BOTCIRCUITS_RUNTIME override still wins). `native` here means "no host
    # CLI" — fall through to the direct provider rather than failing.
    runtime_name = detect_runtime_name(settings=None)
    if runtime_name != NATIVE:
        from botcircuits.runtime.detect import runtime_config
        config = runtime_config(runtime_name, settings=None)
        return CliLLMProvider(config), f"runtime={runtime_name}"

    provider = make_provider(cfg.provider, cfg.model)
    return provider, f"provider={cfg.provider} model={provider.model}"


def _cmd_generate(args: argparse.Namespace) -> int:
    """`workflow generate --from <instructions> --name <name>` — author an
    intent-only workflow SOURCE from a natural-language description and write it
    to the workflows dir. Optionally build it (`--build`)."""
    from .app import load_cli_config
    from botcircuits.agent.workflow.generator import generate_workflow

    name = args.workflow_name
    from_path = Path(args.from_file).expanduser()
    if not from_path.is_file():
        out(C.red(f"[workflow] --from file not found: {from_path}"))
        return 2
    instructions = from_path.read_text()

    resources = ""
    res_file = getattr(args, "resources_file", None)
    if res_file:
        res_path = Path(res_file).expanduser()
        if not res_path.is_file():
            out(C.red(f"[workflow] --resources file not found: {res_path}"))
            return 2
        resources = res_path.read_text()

    try:
        cfg = load_cli_config(args)
    except ConfigError as e:
        out(C.red(f"[config] {e}"))
        return 2

    provider, label = _make_build_provider(cfg)
    out(C.dim(
        f"generating workflow {name!r} from {from_path} using {label}"
    ))
    # File-path / item-list checks resolve relative to the run cwd (the agent
    # runs with cwd = the workspace, where data/ and the input record live).
    from pathlib import Path as _Path
    base_dir = _Path.cwd()

    # Optional dry-run repair: run the draft on sample inputs and feed any
    # decision mismatches back to the model (see _make_dry_run).
    dry_run = None
    samples_file = getattr(args, "dry_run_samples", None)
    if samples_file:
        sp = _Path(samples_file).expanduser()
        if not sp.is_file():
            out(C.red(f"[workflow] --dry-run-samples not found: {sp}"))
            return 2
        try:
            samples = json.loads(sp.read_text())
        except ValueError as e:
            out(C.red(f"[workflow] --dry-run-samples not valid JSON: {e}"))
            return 2
        dry_run = _make_dry_run(samples, base_dir)

    try:
        doc = asyncio.run(generate_workflow(
            instructions, name, provider, resources,
            validate_loop=getattr(args, "validate_loop", 0),
            base_dir=base_dir, dry_run=dry_run,
        ))
    except Exception as e:
        out(C.red(f"[workflow] generate failed: {type(e).__name__}: {e}"))
        return 1
    finally:
        try:
            asyncio.run(provider.aclose())
        except Exception:
            pass

    # Write the SOURCE file. Never clobber an existing file (e.g. a
    # hand-authored workflow) — the name must be distinct.
    workflows_dir = _resolve_workflows_dir()
    try:
        workflows_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        out(C.red(f"[workflow] cannot create {workflows_dir}: {e}"))
        return 1
    dest = workflows_dir / f"{name}.json"
    if dest.exists():
        out(C.red(
            f"[workflow] {dest} already exists — refusing to overwrite. "
            f"Use a different --name."
        ))
        return 2
    dest.write_text(json.dumps(doc, indent=2) + "\n")
    out(C.green(f"[workflow] wrote generated source: {dest}"))

    if getattr(args, "also_build", False):
        # Re-enter the build path on the just-written file.
        build_args = argparse.Namespace(
            workflow_name=name, workflow_name_pos=None, no_optimize=False,
            provider=getattr(args, "provider", None),
            model=getattr(args, "model", None),
        )
        return _cmd_build(build_args)
    out(C.dim(f"  next: botcircuits workflow build --name {name}"))
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    # Imported locally to dodge the app <-> commands_workflow circular import.
    from .app import load_cli_config

    workflow_name = args.workflow_name or args.workflow_name_pos
    if not workflow_name:
        out(C.red("[workflow] `build` requires --name=<workflow name>"))
        return 2

    try:
        cfg = load_cli_config(args)
    except ConfigError as e:
        out(C.red(f"[config] {e}"))
        return 2

    try:
        source_path, record = _locate_workflow_file(workflow_name)
    except LocalWorkflowError as e:
        out(C.red(f"[workflow] {e}"))
        return 2

    flow = record.get("flow")
    if not isinstance(flow, dict):
        out(C.red(
            f"[workflow] {source_path} is missing flow; "
            f"nothing to index."
        ))
        return 2

    # Static lint (pure, no LLM) before compiling. Surfaces the authoring traps
    # that otherwise compile silently — OR-conditions that drop branches, a
    # listDecision decision word that's actually a step name, a missing
    # itemSource file — so the author fixes them from the build output instead of
    # diving into the framework source. Warnings only: the build still proceeds.
    try:
        from pathlib import Path as _Path
        from botcircuits.agent.workflow.workflow_validator import static_issues
        # itemSource / resolver paths are relative to the run cwd (the workspace),
        # which is where the workflow is later run from.
        lint = static_issues(record, base_dir=_Path.cwd())
    except Exception:
        lint = []
    if lint:
        out(C.yellow(f"[workflow] {len(lint)} lint warning(s):"))
        for msg in lint:
            out(C.yellow(f"  - {msg}"))

    provider, label = _make_build_provider(cfg)
    out(C.dim(f"building {workflow_name!r} using {label}"))

    try:
        summary = asyncio.run(
            generate_expressions_and_variables(flow, provider)
        )
    except Exception as e:
        out(C.red(f"[workflow] build failed: {type(e).__name__}: {e}"))
        return 1
    else:
        # Defaults inference (pure, no LLM). Fill the mechanical fields the
        # author can omit — the `deterministic` skip flag, listDecision
        # decisionKey/collectInto/emit/nullOn, dataType upgrades, and a
        # `flow.result` shape — so the SOURCE stays intent-only. Runs after
        # indexing (needs `choices`/`variables`) and before the optimizers.
        df = apply_defaults(flow)
        if any(df.values()):
            out(C.dim(
                f"  defaults filled: {df['deterministic']} deterministic, "
                f"{df['listDecision_defaults']} listDecision, "
                f"{df['result']} result"
            ))
        if not getattr(args, "no_optimize", False):
            # Passes 2+3 — structural graph optimizer (pure, no LLM). Fuse
            # adjacent independent branch steps and fold a terminal restatement
            # step into its producer, so a naturally-drawn graph runs in fewer
            # segments / less redundant output. Runs AFTER indexing (needs
            # `choices`) and BEFORE the action optimizer (so the terse rewrite
            # sees the fused/folded action text).
            g = optimize_graph(flow)
            if g.get("branches_fused") or g.get("emits_folded"):
                out(C.dim(
                    f"  graph optimized: {g['branches_fused']} branch fusion(s), "
                    f"{g['emits_folded']} emit fold(s)"
                ))
        # Pass 1 — action optimizer. Rewrite verbose authored step actions into
        # terse, tool-directed instructions BEFORE segmentation, so a workflow
        # written in natural language runs lean without the author hand-tuning
        # each step. Best-effort and opt-out (`--no-optimize`): a failure leaves
        # the authored text untouched. Runs after indexing (structure settled),
        # before compute_segments (which is unaffected by action wording).
        if not getattr(args, "no_optimize", False):
            opt = asyncio.run(optimize_actions(flow, provider))
            if opt.get("steps_optimized"):
                saved = opt["chars_before"] - opt["chars_after"]
                out(C.dim(
                    f"  actions optimized: {opt['steps_optimized']} step(s), "
                    f"-{saved} chars ({opt['chars_before']}→{opt['chars_after']})"
                ))
        # Branch-delimited segments are derived AFTER the indexer so the
        # `choices` it emits are present. The engine runner reads
        # `flow["segments"]` to batch consecutive non-branching steps into
        # one LLM call.
        flow["segments"] = compute_segments(flow)
    finally:
        # `make_provider` builds a fresh provider for this run; release any
        # async clients it opened.
        try:
            asyncio.run(provider.aclose())
        except Exception:
            pass

    build_dir = _resolve_build_dir()
    try:
        build_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        out(C.red(
            f"[workflow] failed to create build dir {build_dir}: "
            f"{type(e).__name__}: {e}"
        ))
        return 1

    # Mirror the source filename so `<name>.json` in the source aligns
    # with `<name>.json` in the build dir.
    build_path = build_dir / source_path.name
    _write_workflow(build_path, record)

    out(C.dim(
        f"  steps processed: {summary['steps_processed']}  |  "
        f"expressions: {summary['expressions']}  |  "
        f"variables: {summary['variables']}"
    ))

    # Static token footprint of the workflow DEFINITION — how many tokens the
    # raw source and the built artifact occupy (their context cost), counted
    # with the tokenizer for the provider that built it. A size estimate, not
    # tokens the build's LLM calls billed. Best-effort; never fails the build.
    try:
        from botcircuits.usage.token_counter import token_footprint
        raw_record = _load_json(source_path)
        fp = token_footprint(
            raw=raw_record, built=record,
            provider=getattr(provider, "name", None),
        )
        out(C.dim(
            f"  token footprint [{fp['provider']}]: "
            f"raw {fp['raw_tokens']}  |  built {fp['built_tokens']}  |  "
            f"total {fp['total_tokens']}"
        ))
    except Exception:
        pass

    out(C.dim(f"(source: {source_path})"))
    out(C.dim(f"(built:  {build_path})"))
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _locate_workflow_file(workflow_name: str) -> tuple[Path, dict]:
    """Find the workflow.json file that matches `workflow_name`.

    Strategy:
      1. Try `<dir>/<workflow_name>.json` directly.
      2. Otherwise scan every `*.json` and match on its `name` field.
    """
    directory = _resolve_workflows_dir()
    if not directory.is_dir():
        raise LocalWorkflowError(
            f"workflows directory does not exist: {directory}. "
            f"Set ${WORKFLOWS_DIR_ENV} or create {DEFAULT_WORKFLOWS_DIR}/."
        )

    direct = directory / f"{workflow_name}.json"
    if direct.exists():
        return direct, _load_json(direct)

    for path in sorted(directory.glob("*.json")):
        try:
            data = _load_json(path)
        except LocalWorkflowError:
            continue
        if data.get("name") == workflow_name:
            return path, data

    raise LocalWorkflowError(
        f"no workflow with name {workflow_name!r} in {directory}"
    )


def _load_json(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise LocalWorkflowError(f"failed to load {path}: {e}") from e
    if not isinstance(data, dict):
        raise LocalWorkflowError(
            f"{path} must be a JSON object at the top level"
        )
    return data


def _write_workflow(path: Path, record: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
        f.write("\n")


# ---------------------------------------------------------------------------
# `workflow eval`
# ---------------------------------------------------------------------------


def _cmd_eval(args: argparse.Namespace) -> int:
    # Imported locally to dodge the app <-> commands_workflow circular import.
    from .app import load_cli_config, make_provider

    try:
        cfg = load_cli_config(args)
    except ConfigError as e:
        out(C.red(f"[config] {e}"))
        return 2

    # Load datasets. `--dataset` targets a single file; otherwise we
    # scan the configured eval directory.
    try:
        if args.eval_dataset:
            datasets = [load_dataset(Path(args.eval_dataset))]
        else:
            datasets = discover_datasets()
    except EvalDatasetError as e:
        out(C.red(f"[eval] {e}"))
        return 2

    case_count = sum(len(ds.cases) for ds in datasets)
    if not case_count:
        eval_dir = resolve_eval_dir()
        out(C.yellow(
            f"[eval] no cases found"
            f"{' in ' + str(eval_dir) if not args.eval_dataset else ''}. "
            f"Create a JSON dataset under {eval_dir} or pass --dataset."
        ))
        return 2

    # `--skip-prompt-baseline` only suppresses the prompt-only RUNNER.
    # The provider is still constructed and forwarded so inline-build
    # and Layer-B normalization stay available. The two-flag split lets
    # callers say "I want to measure just the workflow side, but it
    # still has to be the real workflow side" without forcing them to
    # stub out the LLM the engine depends on.
    inline_count = sum(1 for ds in datasets if ds.is_inline)
    needs_provider = inline_count > 0 or not args.eval_skip_prompt
    provider = make_provider(cfg.provider, cfg.model) if needs_provider else None

    if args.eval_skip_prompt:
        baseline_note = "prompt-only baseline skipped"
    else:
        baseline_note = f"baseline provider={cfg.provider} model={provider.model}"
    out(C.dim(
        f"running {case_count} case(s) across {len(datasets)} dataset(s) "
        f"({inline_count} inline) x {args.eval_repeats} repeat(s); "
        f"{baseline_note}"
    ))

    try:
        report = asyncio.run(run_evaluation_datasets(
            datasets,
            provider=provider,
            repeats=args.eval_repeats,
            run_prompt_baseline=not args.eval_skip_prompt,
            cleanup_inline_workflow=args.eval_cleanup_inline,
        ))
    except Exception as e:
        out(C.red(f"[eval] failed: {type(e).__name__}: {e}"))
        return 1
    finally:
        if provider is not None:
            try:
                asyncio.run(provider.aclose())
            except Exception:
                pass

    out(render_text(report))
    if args.eval_report:
        report_path = Path(args.eval_report)
        try:
            write_json_report(report, report_path)
            out(C.dim(f"(report: {report_path})"))
        except OSError as e:
            out(C.red(
                f"[eval] failed to write report {report_path}: "
                f"{type(e).__name__}: {e}"
            ))
            return 1
    return 0

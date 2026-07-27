"""End-to-end verification-gate test against the `security_patch_gate`
example under `examples/security_patch_gate/` (see its README + TASK.md).

Unlike `test_verification_gate_order_fulfillment_example.py`, whose
generated gate checks a numeric invariant over the run's own `slots`, this
example's gate check is the application's OWN unit test suite: the
generated `script` check shells out to `npm test` (`node --test`) inside
`examples/security_patch_gate/app/` and reports pass/fail from that real
subprocess run — proving the verification-gate framework can wrap an
arbitrary existing test runner, not just a synthetic slot-invariant script.

The scenario: a demo zero-dependency Node.js login API
(`app/server.js`) ships with a real, demonstrable auth-bypass vulnerability
(a regex built from the raw, unescaped password lets any regex
metacharacter string authenticate as any user). `app/test/auth.test.js` is
the application's pre-existing test suite: it already covers legitimate
logins, PLUS three "SECURITY: ..." regression tests that fail against the
vulnerable code and pass only once the bypass is closed. The workflow's
`patch_vulnerability` step is meant to fix `server.js` in place; the
generated gate's blocking check is exactly `npm test` in `app/` — the
patch is verified against the real, independently-authored test suite, not
an LLM judge or a bespoke slot check.

Only the three `agentAction` steps' "LLM calls" are faked (via a scripted
provider / a fake `run_segment`) — `patch_vulnerability`'s fake performs
the REAL file edit to `app/server.js` (the same one-line fix a real coding
agent would make), so the gate is validated against a real edited file and
a real `node --test` run, not a synthetic fixture.

Requires `node` on PATH to run `npm test` (`node --test`) inside the
example's app; skipped otherwise (this is an example/integration test,
not a framework unit test).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from botcircuits.agent.workflow.condition_processor import generate_expressions_and_variables
from botcircuits.agent.workflow.engine.runner import EngineResult, SegmentResult, run_workflow_engine
from botcircuits.agent.workflow.engine.segments import compute_segments
from botcircuits.agent.workflow.paths import build_dir_for, gate_checks_dir
from botcircuits.agent.workflow.verification import generate_gate, run_gate
from botcircuits.agent.workflow.verification.generator import load_gate
from botcircuits.agent.workflow.workflow_defaults import apply_defaults
from botcircuits.agent.workflow.workflow_validator import static_issues

from tests.fakes import ScriptedProvider, text_response

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "examples" / "security_patch_gate"
APP_DIR = EXAMPLE_DIR / "app"
SERVER_JS = APP_DIR / "server.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="requires `node` to run the example app's own test suite"
)

# ---------------------------------------------------------------------------
# The exact one-line fix a real coding agent would make: replace the
# RegExp-based comparison with an exact, type-checked string comparison.
# ---------------------------------------------------------------------------

_VULNERABLE_SNIPPET = (
    '  if (typeof password !== "string") return { ok: false, reason: "bad_credentials" };\n'
    "\n"
    "  // VULNERABLE: builds a RegExp straight out of the raw, unescaped\n"
    "  // `password` input and uses pattern matching where an EXACT secret\n"
    "  // comparison is required. Any regex metacharacter string (e.g. `.*`)\n"
    "  // matches every stored password, bypassing auth for any user.\n"
    '  const pattern = new RegExp("^" + password + "$");\n'
    "  if (pattern.test(record.password)) {"
)

_PATCHED_SNIPPET = (
    '  if (typeof password !== "string") return { ok: false, reason: "bad_credentials" };\n'
    "\n"
    "  // FIXED: exact string comparison instead of regex pattern matching.\n"
    "  if (record.password === password) {"
)

_INCOMPLETE_SNIPPET = (
    '  if (typeof password !== "string") return { ok: false, reason: "bad_credentials" };\n'
    "\n"
    "  // \"FIXED\": still vulnerable -- only guards non-string input, keeps\n"
    "  // using the RegExp comparison for real string passwords (the actual\n"
    "  // bug a careless/incomplete patch could leave in place).\n"
    '  const pattern = new RegExp("^" + password + "$");\n'
    "  if (pattern.test(record.password)) {"
)


def _apply_fix(snippet: str) -> None:
    original = SERVER_JS.read_text()
    assert _VULNERABLE_SNIPPET in original, "example app's vulnerable snippet moved/changed"
    SERVER_JS.write_text(original.replace(_VULNERABLE_SNIPPET, snippet))


# ---------------------------------------------------------------------------
# Build the example workflow through the REAL pipeline (indexer + gate gen)
# ---------------------------------------------------------------------------

_INDEXER_REPLY = json.dumps({
    "expressions": [
        {"step_id": "load_finding", "idx": 0, "expCondition": "approved_for_auto_patch is true"},
    ],
    "variables": [],
})

def _gate_reply(app_dir: Path) -> str:
    """The scripted gate-generation reply. The check script hardcodes
    `app_dir` as an absolute path baked in at generation time (mirroring
    how a real generator would resolve the workflow's own declared file
    references at build time) rather than walking up from its own
    `.build/.../verifications/checks/` location, which moves depth
    depending on `$BOTCIRCUITS_WORKFLOWS_DIR` and must not be guessed."""
    return json.dumps({"checks": [
        {
            "id": "app_test_suite_passes",
            "type": "script",
            "description": "the patched application's own `npm test` suite (behavioral + security regression tests) must pass",
            "severity": "blocking",
            "code": (
                "import json, subprocess, sys\n"
                "run = json.loads(sys.stdin.read())\n"
                f"app_dir = {str(app_dir)!r}\n"
                "proc = subprocess.run(\n"
                "    ['npm', 'test', '--silent'], cwd=app_dir,\n"
                "    capture_output=True, text=True, timeout=30,\n"
                ")\n"
                "passed = proc.returncode == 0\n"
                "tail = (proc.stdout + proc.stderr)[-1500:]\n"
                "print(json.dumps({'passed': passed, 'detail': tail}))\n"
            ),
        },
        {
            "id": "patch_summary_is_plain_english",
            "type": "llm_judge",
            "description": "patch_summary reads as a plain, non-jargon description of the fix",
            "severity": "advisory",
            "rubric": "Pass only if the summary is a short, readable sentence, not a raw diff.",
            "inputs": ["slots.patch_summary"],
        },
    ]})


@pytest.fixture()
def built_flow(tmp_path, monkeypatch):
    """Build examples/security_patch_gate/security_patch_gate.json through
    the real indexer + defaults + segments + gate-generation pipeline,
    writing the gate under an isolated `$BOTCIRCUITS_WORKFLOWS_DIR`.
    Returns the built `flow` dict."""
    monkeypatch.setenv("BOTCIRCUITS_WORKFLOWS_DIR", str(tmp_path))

    source = json.loads((EXAMPLE_DIR / "security_patch_gate.json").read_text())
    assert static_issues(source, base_dir=REPO_ROOT) == []

    flow = source["flow"]
    provider = ScriptedProvider([text_response(_INDEXER_REPLY)])
    asyncio.run(generate_expressions_and_variables(flow, provider))
    apply_defaults(flow)
    flow["segments"] = compute_segments(flow)

    gate_provider = ScriptedProvider([text_response(_gate_reply(APP_DIR))])
    manifest = asyncio.run(generate_gate(flow, "security_patch_gate", gate_provider))
    assert len(manifest["checks"]) == 2

    assert (gate_checks_dir("security_patch_gate") / "app_test_suite_passes.py").is_file()

    return flow


def _fake_run_segment(fix_mode: str | None):
    """`fix_mode`: None (no patch attempted), "correct", or "incomplete" --
    selects which fix `patch_vulnerability` applies to the real
    `server.js` file."""

    async def run_segment(*, actions, branch_variables, system_notes, slots,
                           item_variables=None, data_variables=None, agent=None):
        joined = " ".join(actions)
        if "Read the security finding" in joined:
            finding = json.loads((EXAMPLE_DIR / "config" / "finding.json").read_text())
            return SegmentResult(text="loaded finding", captured_slots=dict(finding))
        if "Open the vulnerable file" in joined:
            if fix_mode == "correct":
                _apply_fix(_PATCHED_SNIPPET)
                summary = "Replaced the RegExp-based password check with an exact string comparison."
            elif fix_mode == "incomplete":
                _apply_fix(_INCOMPLETE_SNIPPET)
                summary = "Added a type guard for non-string passwords."
            else:
                summary = "No fix applied."
            return SegmentResult(text="patched", captured_slots={"patch_summary": summary})
        # write_patch_report / write_abort_report -- no slots to capture.
        return SegmentResult(text="report written", captured_slots={})

    return run_segment


async def _resolve_unfilled(**kw):
    return {}


def _run_workflow(built_flow, fix_mode: str | None) -> EngineResult:
    return asyncio.run(run_workflow_engine(
        built_flow,
        workflow_name="security_patch_gate",
        run_segment=_fake_run_segment(fix_mode),
        slots={},
        resolve_unfilled=_resolve_unfilled,
    ))


def _rebuild_gate_manifest() -> dict:
    manifest = load_gate("security_patch_gate")
    assert manifest is not None
    return manifest


def _run_gate_for(result: EngineResult, judge_passed: bool):
    manifest = _rebuild_gate_manifest()
    judge_provider = ScriptedProvider([
        text_response(json.dumps({"passed": judge_passed, "detail": "n/a"})),
    ])
    run_record = {
        "workflow_name": "security_patch_gate",
        "slots": {k: v for k, v in result.slots.items() if not k.startswith("__")},
        "summary": result.summary,
        "decisions": result.decisions,
        "done": True,
    }
    return asyncio.run(run_gate(
        manifest, run_record, provider=judge_provider,
        base_dir=build_dir_for("security_patch_gate"),
    ))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_correct_patch_passes_the_apps_own_test_suite(built_flow):
    """A correct, minimal fix (exact string comparison) makes `app/`'s own
    `npm test` pass in full -- both the pre-existing behavioral tests and
    the security regression tests -- so the generated gate's blocking
    `app_test_suite_passes` check passes."""
    original = SERVER_JS.read_text()
    try:
        result = _run_workflow(built_flow, fix_mode="correct")
        assert result.done is True
        assert result.slots["approved_for_auto_patch"] is True

        gate_result = _run_gate_for(result, judge_passed=True)
        assert gate_result.passed is True
        assert gate_result.blocking_failures() == []
    finally:
        SERVER_JS.write_text(original)


def test_incomplete_patch_fails_the_apps_own_test_suite(built_flow):
    """A patch that only guards against non-string input but leaves the
    RegExp-based comparison in place for real strings is still exploitable
    (`.*` still bypasses auth) -- `app/`'s own security regression tests
    catch it, failing `npm test` and the gate's blocking check, regardless
    of a cooperating advisory judge."""
    original = SERVER_JS.read_text()
    try:
        result = _run_workflow(built_flow, fix_mode="incomplete")
        assert result.done is True

        gate_result = _run_gate_for(result, judge_passed=True)
        assert gate_result.passed is False
        blocking = gate_result.blocking_failures()
        assert len(blocking) == 1
        assert blocking[0].check_id == "app_test_suite_passes"
        assert "SECURITY" in blocking[0].detail
    finally:
        SERVER_JS.write_text(original)


def test_unapproved_finding_aborts_before_patching(built_flow, monkeypatch, tmp_path):
    """When `approved_for_auto_patch` is false, the workflow must route to
    the abort report and never touch `server.js` -- the vulnerable file is
    untouched, so a real gate run against it would (correctly) still fail
    the app's own security tests; this test only asserts the routing
    itself, since patch_vulnerability's fake never runs in this branch."""
    finding_path = EXAMPLE_DIR / "config" / "finding.json"
    original_finding = finding_path.read_text()
    original_server = SERVER_JS.read_text()
    try:
        finding = json.loads(original_finding)
        finding["approved_for_auto_patch"] = False
        finding_path.write_text(json.dumps(finding))

        result = _run_workflow(built_flow, fix_mode=None)
        assert result.done is True
        assert result.slots["approved_for_auto_patch"] is False
        # patch_vulnerability's action text ("Open the vulnerable file")
        # never appears in the executed segments for this run.
        assert SERVER_JS.read_text() == original_server
    finally:
        finding_path.write_text(original_finding)
        SERVER_JS.write_text(original_server)

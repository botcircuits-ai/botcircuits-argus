# Security Patch Gate — Workflow Example

A complete example of a **verification-gate-driven** BotCircuits workflow in
the **AppSec / secure coding** domain. Unlike the other `*_gate` examples
(which check a numeric invariant or an LLM judge over the run's own
`slots`), this one's generated verification gate is the target application's
**own unit test suite** — `npm test` inside [app/](app/) — run as a real
subprocess and treated as the ground truth for whether the agent's patch is
correct.

It decides — fully unattended — whether a single reported security finding
is approved for auto-patch, has the agent fix the vulnerable source file in
place, and writes a patch report. It uses a **file read** (the finding), a
**file edit** (the actual fix, applied by the `agentAction` step's
underlying coding agent), a **branch condition** (approved vs. not), and a
**file write** (the patch report) — plus the build's automatic
**verification gate**, whose blocking check is the app's real test runner.

## Contents

| Path | What it is |
|------|------------|
| [TASK.md](TASK.md) | The natural-language prompt you paste into the `botcircuits-workflow-authoring` skill to generate the workflow. |
| [config/finding.json](config/finding.json) | The one security finding to patch: file, function, severity, description, remediation hint, and an `approved_for_auto_patch` flag. |
| [app/server.js](app/server.js) | A small, zero-dependency Node.js demo login API — ships with a real, demonstrable auth-bypass vulnerability for the workflow to fix. |
| [app/test/auth.test.js](app/test/auth.test.js) | The app's OWN pre-existing unit test suite (`node:test`) — behavioral tests (legitimate logins) plus security regression tests (the bypass must be closed). This is what the generated gate runs. |
| [app/package.json](app/package.json) | `npm test` runner (`node --test`) — no `npm install` needed, zero dependencies. |

## 1. The vulnerability

`authenticate()` in [app/server.js](app/server.js) builds a `RegExp` directly
out of the raw, unescaped `password` field from the request body and tests it
against the stored password with `RegExp.test()`, instead of comparing the
two strings exactly:

```js
const pattern = new RegExp("^" + password + "$");
if (pattern.test(record.password)) { /* authenticated */ }
```

Any regex metacharacter string — e.g. `.*` — matches every stored password,
authenticating as any user without knowing their real password:

```bash
node app/server.js &
curl -s -X POST http://localhost:4600/api/login \
  -H 'content-type: application/json' \
  -d '{"username":"alice","password":".*"}'
# {"ok":true,"user":{"username":"alice","role":"admin"}}   <- auth bypass
```

Check the app's own test suite fails on this (before any patch):

```bash
cd app && npm test
# 5 pass, 2 fail — the "SECURITY: ..." regex-bypass regression tests fail
```

## 2. Author the workflow

Paste the prompt in [TASK.md](TASK.md) into the `botcircuits-workflow-authoring`
skill. It generates the `security_patch_gate` workflow, which:

1. reads the finding from [config/finding.json](config/finding.json),
2. aborts (writing a `patch-abort-<date>.json`) if `approved_for_auto_patch`
   is `false`,
3. otherwise has the agent open `app/server.js`, apply the smallest fix that
   closes the hole (an exact string comparison instead of the `RegExp`
   pattern match), and save it in place, and
4. writes `patch-report-<date>.json` with the finding id, patched file, and
   a short summary of the change.

Building the workflow also **automatically generates a verification gate**
(this is the whole point of the example): a `script` check whose code is

```python
subprocess.run(["npm", "test"], cwd=<app_dir>, ...)
```

marked `severity: "blocking"`. `workflow run` runs this check against the
completed run and self-repairs if it fails — so an incomplete or wrong patch
(one that still lets `.*` bypass auth) is caught by the **same test suite a
human reviewer would run**, not a bespoke slot-invariant script or an LLM
judge.

## 3. Drive both outcomes

- **Correct patch:** a fix that replaces the `RegExp` comparison with
  `record.password === password` (after a `typeof password === "string"`
  guard) makes all 7 of `app/test/auth.test.js`'s tests pass — the gate
  passes.
- **Incomplete patch:** a fix that only adds the `typeof` guard but leaves
  the `RegExp` comparison in place for real strings is still exploitable
  (`.*` still bypasses auth) — the app's own security regression tests
  catch it, `npm test` exits non-zero, and the gate's blocking check fails,
  regardless of what an advisory LLM judge thinks of the patch summary.
- **Not approved:** set `"approved_for_auto_patch": false` in
  [config/finding.json](config/finding.json) — the workflow aborts before
  touching `server.js` at all.

See [`tests/test_verification_gate_security_patch_gate_example.py`](../../tests/test_verification_gate_security_patch_gate_example.py)
for all three driven end-to-end against the real build pipeline, the real
engine, and a real `npm test` subprocess run.

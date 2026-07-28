// Unit tests for `authenticate()` in ../server.js.
//
// This suite is the application's OWN test suite — the workflow's
// verification gate runs it (via `npm test`, see ../../bin/run_app_tests.py)
// as the BLOCKING check that the `patch_vulnerability` step's edit didn't
// break legitimate logins while it closed the auth-bypass hole. Uses only
// `node:test` + `node:assert` (ships with Node 18+, zero npm installs).

const test = require("node:test");
const assert = require("node:assert/strict");
const { authenticate } = require("../server.js");

test("correct password authenticates", () => {
  const result = authenticate("alice", "correct-horse-battery-staple");
  assert.equal(result.ok, true);
  assert.equal(result.user.username, "alice");
  assert.equal(result.user.role, "admin");
});

test("wrong password is rejected", () => {
  const result = authenticate("alice", "not-the-password");
  assert.equal(result.ok, false);
  assert.equal(result.reason, "bad_credentials");
});

test("unknown user is rejected", () => {
  const result = authenticate("mallory", "anything");
  assert.equal(result.ok, false);
  assert.equal(result.reason, "no_such_user");
});

test("second real user (bob) still authenticates after a fix", () => {
  const result = authenticate("bob", "hunter2");
  assert.equal(result.ok, true);
  assert.equal(result.user.role, "user");
});

// --- Security regression tests: the auth-bypass this example's finding is
// about. These MUST pass once `patch_vulnerability` fixes server.js; they
// FAIL against the original vulnerable code (a regex built from the raw
// password lets any regex metacharacter string match every stored
// password) -- exactly what the gate should catch if the agent's patch is
// missing or incomplete.

test("SECURITY: regex wildcard password (.*) must not authenticate", () => {
  const result = authenticate("alice", ".*");
  assert.equal(result.ok, false);
});

test("SECURITY: regex alternation password must not authenticate", () => {
  const result = authenticate("bob", "hunter2|.*");
  assert.equal(result.ok, false);
});

test("SECURITY: non-string password must not authenticate", () => {
  const result = authenticate("alice", { $ne: null });
  assert.equal(result.ok, false);
});

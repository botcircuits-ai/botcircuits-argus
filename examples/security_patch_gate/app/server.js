// Demo login API for the `security_patch_gate` workflow example.
//
// Zero-dependency Node.js HTTP server (uses only `node:http`, `node:url`,
// `node:querystring` — no `npm install` needed to run it or its tests).
//
// VULNERABILITY (the one `bin/finding.json` reports, and the one the
// workflow's `patch_vulnerability` step must fix): `authenticate()` below
// checks the submitted password by building a `RegExp` out of the raw,
// unescaped input and testing it against the stored password, instead of
// comparing the two strings directly. Submitting a regex metacharacter
// string as the password — e.g. `.*` — matches ANY stored password,
// authenticating as any user without knowing their real password. This
// mirrors a real class of "used a pattern-matching primitive where an
// exact-equality check was needed" auth-bypass bugs.
//
// Endpoints:
//   POST /api/login   { "username": "...", "password": "..." } -> { ok, user? }
//   GET  /healthz      -> { ok: true }
//
// `users` is an in-memory fixture — this is a demo, not a real user store.

const http = require("http");

const PORT = process.env.PORT || 4600;

const USERS = {
  alice: { password: "correct-horse-battery-staple", role: "admin" },
  bob: { password: "hunter2", role: "user" },
};

function authenticate(username, password) {
  const record = USERS[username];
  if (!record) return { ok: false, reason: "no_such_user" };

  if (typeof password !== "string") return { ok: false, reason: "bad_credentials" };

  if (record.password === password) {
    return { ok: true, user: { username, role: record.role } };
  }
  return { ok: false, reason: "bad_credentials" };
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 1_000_000) req.destroy();
    });
    req.on("end", () => resolve(raw));
    req.on("error", reject);
  });
}

const server = http.createServer(async (req, res) => {
  if (req.method === "GET" && req.url === "/healthz") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
    return;
  }

  if (req.method === "POST" && req.url === "/api/login") {
    let body;
    try {
      const raw = await readBody(req);
      body = raw ? JSON.parse(raw) : {};
    } catch {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: false, reason: "bad_json" }));
      return;
    }

    const { username, password } = body || {};
    if (typeof username !== "string" || !username) {
      res.writeHead(400, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ ok: false, reason: "missing_username" }));
      return;
    }

    const result = authenticate(username, password);
    res.writeHead(result.ok ? 200 : 401, { "Content-Type": "application/json" });
    res.end(JSON.stringify(result));
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ ok: false, reason: "not_found" }));
});

if (require.main === module) {
  server.listen(PORT, () => {
    console.log(`Demo login API listening on http://localhost:${PORT}`);
  });
}

module.exports = { server, authenticate, USERS };

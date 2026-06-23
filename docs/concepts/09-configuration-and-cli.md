# 8. Configuration & CLI

[← Index](00-index.md)

---

How the agent is set up and run.

## Configuration

Settings live in **layered JSON files** — a global set under the agent's home
directory and a project-local set in your project. Local files override global
ones, and personal `*.local.json` files override both.

What you configure:

- **provider & model** — which LLM to use.
- **tools** — which built-in tools are on, and their settings.
- **MCP servers** — external tool servers to connect to.
- **limits** — like the maximum steps per turn.

The rule: **settings go in config; code goes in code.** You never put tool
*implementations* in JSON — only their parameters.

## The CLI

The command-line tool runs the agent interactively or in a script:

- **chat** — talk to the agent in the terminal; supports streaming and showing
  tool results.
- **workflow generate** — draft an intent-only workflow source from a
  natural-language description.
- **workflow build** — compile a workflow source into its runnable form.
- **mcp / setup** — manage MCP servers and initial setup.

Because the CLI can read piped input, it doubles as a way to script multi-turn
runs.

Next: [Gateway](10-gateway.md).

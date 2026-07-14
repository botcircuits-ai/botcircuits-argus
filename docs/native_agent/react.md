# ReAct (`agent/react.py`)

Text-mode tool use as an alternative to native function calling — the classic
ReAct (Yao et al., 2022) pattern. Selected with `mode="react"` (config:
`mode` in `settings.json`).

Tools are described *in the system prompt* (`render_react_preamble`); the
model emits a `Thought / Action / Action Input` block that
`parse_react_step` extracts (one action per turn); the tool result is fed
back as a plain-text `Observation:` (`format_observation`), and the loop
repeats until a `Final Answer`.

Why keep it: works on any provider regardless of native tool-use quality,
yields a visible reasoning trace, and serves as an eval baseline. Trade-off:
format brittleness — the parser is deliberately lenient, and an unparseable
reply is treated as terminal text.

The loop keeps the full Thought/Action trace in history on non-terminal
turns (so the model sees its own prior format) but persists only the clean
Final Answer on the terminal turn.

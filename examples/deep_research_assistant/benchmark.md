# Claude Code vs Claude Code + Argus — Comparison

_Generated 2026-06-27 00:53 · 1 use case(s)_

Two agents on the identical `claude` binary + model. **claude-code** free-runs the task from the prompt; **claude-code-argus** drives the built BotCircuits workflow through the deterministic engine (one `claude -p` call per branch segment).

## Headline

| | claude-code (bare) | claude-code-argus | Workflow advantage |
|---|---|---|---|
| Mean accuracy | 100% | 100% | +0 pts |
| Mean consistency | 1.00 | 1.00 | = |
| Total tokens (sum) | 191,114 | 5,807 | 33× fewer |
| Total cost (sum) | $0.5652 | $0.0870 | 6.5× cheaper |
| Total latency (sum) | 146.0s | 76.4s | 1.9× faster |

## Per use case

### deep_research_assistant

| Metric | claude-code | claude-code-argus | Δ |
|---|---|---|---|
| Accuracy | 100% | 100% | |
| Consistency | 1.00 | 1.00 | |
| Avg tokens | 191,114 | 5,807 | 33× |
| Avg cost | $0.5652 | $0.0870 | 6.5× |
| Avg latency | 146.0s | 76.4s | 1.9× |
| Run status | ok | ok | |

---
_Accuracy = per-item decisions vs the deterministic oracle. Consistency = fraction of repeats agreeing on the modal answer. Cost/tokens/latency are per-run averages; usage is the agents' real reported usage._
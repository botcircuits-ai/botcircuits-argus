# UI (`cli/`)

Agent = Model + Harness + **UI**. Two front-ends over the same agent; the
harness never imports either.

```
              botcircuits                    botcircuits --tui
                  │                                │
                  ▼                                ▼
        line REPL (cli/tui.py)          Textual app (cli/tui_app.py)
        prompt_toolkit prompt           ┌───────────────┬──────────┐
        pinned at the bottom;           │ conversation  │ activity │
        streams print above it          │ (markdown,    │ (tools,  │
                  │                     │  streaming)   │  prints) │
                  │                     │───────────────│          │
                  │                     │ input         │          │
                  │                     └───────┬───────┴──────────┘
     pause / y/N: the question                  │
     REPLACES the prompt in place     y/N gate: ApprovalModal takes the
     (redrawn immediately)            screen — allow (y) / deny (n),
                                      fail-closed; questions land in the
                                      conversation pane and the next
                                      input line is the reply
```

## Line REPL (`cli/tui.py`)

`TUISession` runs chat turns as background tasks so the input prompt stays
live during streaming. Pauses (y/N gates, `human_feedback`, workflow
questions) go through one channel — `pause(question)` — and the question
**becomes the visible prompt immediately**: the prompt text is re-evaluated
on every redraw and the pending `prompt_async` is invalidated when a pause
arrives, so a confirmation can never sit invisible behind a bare `| > `.

## Textual TUI (`cli/tui_app.py`, `--tui`, needs the `tui` extra)

Modeled on gemma's `ui/tui.py`, adapted to the async agent (a turn is a
task on the same loop — no worker-thread bridge). Three seams into the
existing harness, no agent changes:

- `_confirm.set_confirmer(...)` — gated tools (`shell_exec`, `write_file`,
  `edit_file`, `plan_and_confirm`) surface as a fail-closed **modal**
  showing the full proposal.
- `set_tui_session(adapter)` — `human_feedback` / background-workflow
  pauses show the question in the conversation pane; the next input line
  is routed back as the reply.
- `App.begin_capture_print` — stray tool `print()`/stderr output lands in
  the activity pane instead of corrupting the screen.

Commands: `/plan <task>`, `/reset`, `/new`, `/quit`; anything else is
delegated to the shared slash dispatcher (its output appears in the
activity pane).

# Events (`agent/events.py`)

Translating loop internals into UI-facing signals.

```
 workflow engine segment                       chat_stream consumer (UI)
 (runs INSIDE a tool handler,
  invisible to the main loop)

   ("text", str)        ──┐
   ("tool_call", tc)    ──┼─► segment_stream_events ─► StreamEvent
   ("tool_result", ...) ──┘         (same shapes         text_delta
                                     the loop yields)    tool_call
                                                         tool_result

 tool round results ──► human_feedback_pause ──► question │ None
                        (did the model ask          │
                         the user something?)       ▼
                                              loop pauses; the reply
                                              is the question
```

## `segment_stream_events(kind, payload, sid)`

An engine-driven workflow runs inside a tool handler, not in the main loop —
its segment activity would be invisible to a streaming UI. Segment sinks emit
`("text", str)`, `("tool_call", ToolCall)`, `("tool_result", (tc, out, err))`;
this function maps them to the *same* `StreamEvent` shapes the main loop
yields, so workflow-internal calls look live in `chat_stream`.

## `human_feedback_pause(tool_calls, results)`

Detects that the model asked the user a question via the `human_feedback`
tool this round and returns the question text (else `None`). The loop uses it
to pause: the question becomes the turn's reply, and the user's next message
resumes — instead of spinning another provider call.

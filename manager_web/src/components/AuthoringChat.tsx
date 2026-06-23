"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { SparkleIcon } from "@/components/icons";
import { api, type WorkflowDoc } from "@/lib/api";
import { useAuth } from "@/lib/auth";

/**
 * Natural-language authoring + run chat.
 *
 * Two flows over one chat surface:
 *   - Authoring: sends an instruction to the backend's SSE authoring endpoint,
 *     which drives the configured agent runtime to write + build the workflow.
 *     On completion we show a concise success/error message (not the raw JSON)
 *     and, on success, a suggestion chip to run the workflow.
 *   - Running: a `run` instruction is routed to the run SSE endpoint, which
 *     drives the deterministic engine. A paused run asks a question; the user's
 *     next message resumes it with their reply.
 *
 * Suggestions are rendered as chips that, when clicked, drop their command into
 * the input box so the user can review and send it.
 */

type Suggestion = { label: string; fill: string };

type ChatMsg =
  | { role: "user"; text: string }
  | {
      role: "assistant";
      text: string;
      done?: boolean;
      ok?: boolean;
      suggestions?: Suggestion[];
    };

/** A leading "run" verb marks a run request; everything else is authoring. */
function isRunInstruction(text: string): boolean {
  return /^\s*(run|start|execute|kick off)\b/i.test(text);
}

export function AuthoringChat({
  name,
  onWorkflow,
}: {
  name: string;
  onWorkflow: (doc: WorkflowDoc) => void;
}) {
  const { token } = useAuth();
  const [input, setInput] = useState("");
  const [running, setRunning] = useState(false);
  // True while the agent is authoring/building (drives the page glow). Run
  // activity sets `running` but not this, so the glow is build-only.
  const [authoring, setAuthoring] = useState(false);
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  // When a run pauses on a question, the next user message is its reply.
  const [awaitingReply, setAwaitingReply] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  useEffect(() => () => esRef.current?.close(), []);

  const appendLog = useCallback((line: string) => {
    setMessages((m) => {
      const last = m[m.length - 1];
      if (last?.role !== "assistant") return m;
      const text = last.text ? last.text + "\n" + line : line;
      return [...m.slice(0, -1), { ...last, text }];
    });
  }, []);

  // Replace the trailing assistant bubble with a terminal message + chips.
  const finishAssistant = useCallback(
    (text: string, ok: boolean, suggestions?: Suggestion[]) => {
      setMessages((m) => {
        const last = m[m.length - 1];
        if (last?.role !== "assistant") return m;
        return [...m.slice(0, -1), { ...last, text, done: true, ok, suggestions }];
      });
    },
    [],
  );

  const clickSuggestion = useCallback((s: Suggestion) => {
    setInput(s.fill);
    inputRef.current?.focus();
  }, []);

  // --- Authoring (create / edit) -------------------------------------------
  const startAuthoring = useCallback(
    (instruction: string) => {
      if (!token) return;
      setAuthoring(true);
      const es = new EventSource(api.authorStreamUrl(token, name, instruction));
      esRef.current = es;

      es.addEventListener("start", (e) => {
        const d = JSON.parse((e as MessageEvent).data);
        appendLog(`▸ working with ${d.runtime}…`);
      });
      es.addEventListener("log", (e) => {
        const d = JSON.parse((e as MessageEvent).data);
        appendLog(d.line);
      });
      es.addEventListener("error", (e) => {
        const data = (e as MessageEvent).data;
        let msg = "Connection lost.";
        if (data) {
          try {
            msg = JSON.parse(data).message;
          } catch {
            msg = "Stream error.";
          }
        }
        finishAssistant("⚠ " + msg, false);
        setRunning(false);
        setAuthoring(false);
        es.close();
      });
      es.addEventListener("done", (e) => {
        const d = JSON.parse((e as MessageEvent).data);
        if (d.ok && d.workflow) {
          onWorkflow(d.workflow as WorkflowDoc);
          const wfName = (d.name as string) || name;
          const built = d.built as boolean;
          const text = built
            ? `✓ Workflow "${wfName}" saved and built — it's ready to run.\n\nTo run it, send: run ${wfName}`
            : `✓ Workflow "${wfName}" saved. Build it (Save & Build), then run it.\n\nTo run it once built, send: run ${wfName}`;
          finishAssistant(
            text,
            true,
            built ? [{ label: `▶ Run ${wfName}`, fill: `run ${wfName}` }] : undefined,
          );
        } else {
          finishAssistant("⚠ Finished, but no workflow file was produced.", false);
        }
        setRunning(false);
        setAuthoring(false);
        es.close();
      });
    },
    [token, name, appendLog, finishAssistant, onWorkflow],
  );

  // --- Running -------------------------------------------------------------
  const startRun = useCallback(
    (reply?: string) => {
      if (!token) return;
      appendLog(reply ? "▸ resuming run…" : `▸ running ${name}…`);
      const es = new EventSource(api.runStreamUrl(token, name, reply));
      esRef.current = es;

      es.addEventListener("error", (e) => {
        const data = (e as MessageEvent).data;
        let msg = "Connection lost.";
        if (data) {
          try {
            msg = JSON.parse(data).message;
          } catch {
            msg = "Stream error.";
          }
        }
        finishAssistant("⚠ " + msg, false);
        setAwaitingReply(false);
        setRunning(false);
        es.close();
      });
      es.addEventListener("result", (e) => {
        const d = JSON.parse((e as MessageEvent).data);
        if (d.status === "success") {
          finishAssistant(`✓ ${d.message || "Workflow completed."}`, true);
          setAwaitingReply(false);
        } else if (d.status === "paused") {
          finishAssistant(
            `❓ ${d.question || "The workflow needs your input."}\n\nReply below to continue.`,
            true,
          );
          setAwaitingReply(true);
        } else {
          finishAssistant(`⚠ ${d.message || "Workflow run failed."}`, false);
          setAwaitingReply(false);
        }
        setRunning(false);
        es.close();
      });
    },
    [token, name, appendLog, finishAssistant],
  );

  const send = useCallback(() => {
    const text = input.trim();
    if (!text || !token || running || !name) return;

    setInput("");
    setMessages((m) => [
      ...m,
      { role: "user", text },
      { role: "assistant", text: "" },
    ]);
    setRunning(true);

    if (awaitingReply) {
      // Resume the paused run with this message as the reply.
      startRun(text);
    } else if (isRunInstruction(text)) {
      startRun();
    } else {
      startAuthoring(text);
    }
  }, [input, token, running, name, awaitingReply, startRun, startAuthoring]);

  return (
    <div className="flex flex-col h-full border-l border-border bg-surface">
      {/* Page-wide green glow ring while the agent builds the workflow. */}
      {authoring && <div className="ai-glow-overlay" aria-hidden />}
      <div className="h-12 shrink-0 flex items-center gap-2 px-4 border-b border-border">
        <SparkleIcon className="w-[18px] h-[18px] text-brand-600 dark:text-brand-400" />
        <span className="text-sm font-medium text-fg">Author & run with AI</span>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-y-auto p-4 space-y-3">
        {messages.length === 0 && (
          <p className="text-sm text-muted">
            Describe the workflow you want and the agent will write and build it.
            Once it&apos;s built, send{" "}
            <span className="font-mono text-fg">run {name || "<name>"}</span> to
            run it right here.
          </p>
        )}
        {messages.map((m, i) => {
          const isAssistant = m.role === "assistant";
          // Terminal assistant summaries read as prose; live logs stay mono.
          const mono = isAssistant && !m.done;
          return (
            <div key={i} className={m.role === "user" ? "text-right" : ""}>
              <div
                className={
                  "inline-block rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap max-w-full text-left " +
                  (m.role === "user"
                    ? "bg-brand text-zinc-900"
                    : mono
                      ? "bg-elevated text-fg font-mono text-[12px]"
                      : "bg-elevated text-fg")
                }
              >
                {m.text || (running && i === messages.length - 1 ? "…" : "")}
              </div>
              {isAssistant && m.suggestions && m.suggestions.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-2">
                  {m.suggestions.map((s, si) => (
                    <button
                      key={si}
                      onClick={() => clickSuggestion(s)}
                      className="inline-flex items-center gap-1 rounded-full border border-brand/40 bg-brand/10 px-3 py-1 text-[12px] font-medium text-brand-700 hover:bg-brand/20 dark:text-brand-300"
                    >
                      {s.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="shrink-0 p-3 border-t border-border">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            placeholder={
              name
                ? awaitingReply
                  ? "Type your answer to continue the run…"
                  : "Describe a change, or: run " + name
                : "Name the workflow first"
            }
            rows={2}
            disabled={!name || running}
            className="flex-1 resize-none rounded-lg border border-border bg-bg px-3 py-2 text-sm text-fg placeholder:text-muted focus:outline-none focus:ring-2 focus:ring-brand/40 disabled:opacity-60"
          />
          <button
            onClick={send}
            disabled={!input.trim() || !name || running}
            className="h-9 px-3 rounded-lg text-sm font-semibold bg-brand text-zinc-900 hover:bg-brand-300 disabled:opacity-50"
          >
            {running ? "…" : "Send"}
          </button>
        </div>
      </div>
    </div>
  );
}

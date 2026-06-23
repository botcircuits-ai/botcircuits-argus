"use client";

import { useState } from "react";
import type { TraceEvent } from "@/lib/api";
import { cx, eventDotColor, eventLabel, fmtDuration, fmtTokens } from "@/lib/format";

/**
 * Vertical event timeline, grouped by step.
 *
 * The raw trace is a flat sequence (session_start, then per step: step_enter →
 * action_before → action_after → branch, …). Here we fold each step's events
 * into one **step block** that names the step and nests its action start/stop
 * (and slot/branch) as sub-items, instead of listing them in the main
 * sequence. Events outside any step (session_start / session_end / paused)
 * remain top-level rows.
 */

type StepGroup = {
  kind: "step";
  step: string;
  enter: TraceEvent;
  children: TraceEvent[];
};
type Single = { kind: "single"; ev: TraceEvent };
type Row = StepGroup | Single;

// Event types that belong to the step currently in progress (nested as
// sub-items rather than shown in the main sequence).
const CHILD_TYPES = new Set([
  "action_before",
  "action_after",
  "slot_resolve",
  "branch",
]);

function group(events: TraceEvent[]): Row[] {
  const rows: Row[] = [];
  let current: StepGroup | null = null;
  for (const ev of events) {
    if (ev.type === "step_enter") {
      current = { kind: "step", step: ev.step ?? "step", enter: ev, children: [] };
      rows.push(current);
      continue;
    }
    if (current && CHILD_TYPES.has(ev.type)) {
      // action_* events carry no step id; attribute them to the open step.
      current.children.push(ev);
      continue;
    }
    // Boundary event (session_start/end, paused, anything unexpected) closes
    // the current step grouping and stands on its own.
    current = null;
    rows.push({ kind: "single", ev });
  }
  return rows;
}

export function TraceTimeline({
  events,
  highlightStep,
}: {
  events: TraceEvent[];
  highlightStep: string | null;
}) {
  const rows = group(events);
  return (
    <ol className="relative">
      {rows.map((row, i) => {
        const last = i === rows.length - 1;
        if (row.kind === "single") {
          return <SingleRow key={row.ev.seq} ev={row.ev} last={last} />;
        }
        return (
          <StepBlock
            key={row.enter.seq}
            group={row}
            last={last}
            highlight={!!highlightStep && row.step === highlightStep}
          />
        );
      })}
    </ol>
  );
}

/** A standalone (non-step) event row, e.g. session start/end. */
function SingleRow({ ev, last }: { ev: TraceEvent; last: boolean }) {
  return (
    <li className="relative pl-8 pb-4">
      {!last && <Spine />}
      <Dot type={ev.type} />
      <EventCard ev={ev} />
    </li>
  );
}

/** A step block: names the step and nests its action start/stop + slot/branch. */
function StepBlock({
  group,
  last,
  highlight,
}: {
  group: StepGroup;
  last: boolean;
  highlight: boolean;
}) {
  const totalMs = group.children
    .filter((c) => c.type === "action_after")
    .reduce((sum, c) => sum + (c.duration_ms ?? 0), 0);
  // Sum the real tokens this step's action call(s) billed, when reported.
  const stepTokens = group.children
    .filter((c) => c.type === "action_after")
    .reduce(
      (sum, c) => sum + (((c.data as any)?.output?.usage?.total_tokens as number) ?? 0),
      0,
    );

  return (
    <li className="relative pl-8 pb-4">
      {!last && <Spine />}
      <Dot type="step_enter" />
      <div
        className={cx(
          "rounded-xl border",
          highlight ? "border-brand/50 bg-brand/5" : "border-border bg-surface",
        )}
      >
        {/* Step header. A segment can bundle several steps; show the full
            path (e.g. not_found → ask_retry) so a non-head step isn't hidden
            behind the segment's primary step name. */}
        <div className="px-3 py-2 flex items-center gap-2 border-b border-border">
          <span className="text-[11px] uppercase tracking-wide text-muted">
            {stepsOf(group.enter).length > 1 ? "Steps" : "Step"}
          </span>
          <span className="font-medium text-fg text-sm">
            {stepsOf(group.enter).length > 1
              ? stepsOf(group.enter).join(" → ")
              : group.step}
          </span>
          {totalMs > 0 && (
            <span className="text-xs text-brand-700 dark:text-brand-300">
              {fmtDuration(totalMs)}
            </span>
          )}
          {stepTokens > 0 && (
            <span className="text-[10px] font-medium text-brand bg-brand/10 rounded px-1 py-px tabular-nums">
              {fmtTokens(stepTokens)} tok
            </span>
          )}
          <span className="ml-auto text-xs text-muted tabular-nums">
            #{group.enter.seq}
          </span>
        </div>

        {/* Step's action text (from step_enter) */}
        {actionsOf(group.enter).length > 0 && (
          <div className="px-3 pt-2">
            <ul className="list-disc pl-4 text-sm text-fg space-y-0.5">
              {actionsOf(group.enter).map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </div>
        )}

        {/* Nested sub-items: action start/stop, slot resolve, branch */}
        <ul className="px-3 py-2 space-y-1.5">
          {group.children.map((c) => (
            <SubItem key={c.seq} ev={c} />
          ))}
          {group.children.length === 0 && (
            <li className="text-xs text-muted">No sub-events.</li>
          )}
        </ul>
      </div>
    </li>
  );
}

/** A nested sub-event (action_before / action_after / slot_resolve / branch). */
function SubItem({ ev }: { ev: TraceEvent }) {
  const [open, setOpen] = useState(false);
  const hasDetail =
    Object.keys(ev.data ?? {}).length > 0 ||
    Object.keys(ev.slots ?? {}).length > 0;
  return (
    <li className="rounded-lg border border-border bg-elevated/40">
      <button
        onClick={() => hasDetail && setOpen((o) => !o)}
        className="w-full flex items-center gap-2 text-left px-2.5 py-1.5"
      >
        <span className={cx("h-2 w-2 rounded-full shrink-0", eventDotColor(ev.type))} />
        <span className="text-xs font-medium text-fg">{eventLabel(ev.type)}</span>
        {ev.duration_ms != null && (
          <span className="text-[11px] text-brand-700 dark:text-brand-300">
            {fmtDuration(ev.duration_ms)}
          </span>
        )}
        <span className="ml-auto text-[11px] text-muted tabular-nums">
          #{ev.seq}
        </span>
        {hasDetail && (
          <span className="text-muted text-[11px]">{open ? "▾" : "▸"}</span>
        )}
      </button>
      {open && (
        <div className="px-2.5 pb-2 space-y-2">
          <EventData ev={ev} />
          {Object.keys(ev.slots ?? {}).length > 0 && (
            <Section title="Memory at this point">
              <KeyVals obj={ev.slots} />
            </Section>
          )}
        </div>
      )}
    </li>
  );
}

/** Card body for a standalone event (session start/end, paused). */
function EventCard({ ev }: { ev: TraceEvent }) {
  const [open, setOpen] = useState(false);
  const hasDetail =
    Object.keys(ev.data ?? {}).length > 0 ||
    Object.keys(ev.slots ?? {}).length > 0;
  return (
    <div className="rounded-xl border border-border bg-surface px-3 py-2">
      <button
        onClick={() => hasDetail && setOpen((o) => !o)}
        className="w-full flex items-center gap-2 text-left"
      >
        <span className="text-sm font-medium text-fg">{eventLabel(ev.type)}</span>
        {ev.step && <span className="font-mono text-xs text-muted">{ev.step}</span>}
        <span className="ml-auto text-xs text-muted tabular-nums">#{ev.seq}</span>
        {hasDetail && <span className="text-muted text-xs">{open ? "▾" : "▸"}</span>}
      </button>
      {open && (
        <div className="mt-2 space-y-2">
          <EventData ev={ev} />
          {Object.keys(ev.slots ?? {}).length > 0 && (
            <Section title="Memory at this point">
              <KeyVals obj={ev.slots} />
            </Section>
          )}
        </div>
      )}
    </div>
  );
}

function actionsOf(ev: TraceEvent): string[] {
  const a = (ev.data as any)?.actions;
  return Array.isArray(a) ? a : [];
}

/** The actual steps a `step_enter` segment ran (the head plus any bundled
 *  follow-on steps), falling back to the single event step. */
function stepsOf(ev: TraceEvent): string[] {
  const s = (ev.data as any)?.steps;
  if (Array.isArray(s) && s.length > 0) return s.filter(Boolean);
  return ev.step ? [ev.step] : [];
}

function EventData({ ev }: { ev: TraceEvent }) {
  const d = ev.data ?? {};
  if (ev.type === "action_after") {
    const out = (d as any).output ?? {};
    return (
      <>
        {(d as any).input?.actions && (
          <Section title="Action (input)">
            <ul className="list-disc pl-4 text-sm text-fg space-y-0.5">
              {((d as any).input.actions as string[]).map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </Section>
        )}
        <Section title="Sub-agent output">
          {out.text ? (
            <p className="text-sm text-fg whitespace-pre-wrap">{out.text}</p>
          ) : (
            <p className="text-sm text-muted">—</p>
          )}
          {out.captured_slots &&
            Object.keys(out.captured_slots).length > 0 && (
              <div className="mt-1">
                <KeyVals obj={out.captured_slots} />
              </div>
            )}
        </Section>
        {out.usage && (
          <Section title="Token usage">
            <div className="flex flex-wrap gap-2 text-xs text-fg tabular-nums">
              <UsageStat label="total" value={out.usage.total_tokens} strong />
              <UsageStat label="input" value={out.usage.input_tokens} />
              <UsageStat label="output" value={out.usage.output_tokens} />
              {out.usage.cache_read_tokens > 0 && (
                <UsageStat label="cache read" value={out.usage.cache_read_tokens} />
              )}
              {out.usage.cache_write_tokens > 0 && (
                <UsageStat label="cache write" value={out.usage.cache_write_tokens} />
              )}
            </div>
          </Section>
        )}
      </>
    );
  }
  if (ev.type === "branch") {
    return (
      <Section title="Branch decision">
        <div className="text-sm text-fg">
          → <code className="font-mono">{(d as any).chosen_next ?? "end"}</code>
          {(d as any).branched ? (
            <span className="ml-2 text-xs text-warn">(branched)</span>
          ) : (
            <span className="ml-2 text-xs text-muted">(default)</span>
          )}
        </div>
      </Section>
    );
  }
  if (ev.type === "slot_resolve") {
    return (
      <Section title="Resolved memory">
        <KeyVals obj={(d as any).resolved ?? {}} />
      </Section>
    );
  }
  if (Object.keys(d).length === 0) return null;
  return (
    <Section title="Data">
      <KeyVals obj={d} />
    </Section>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-lg bg-elevated/60 border border-border px-2.5 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted mb-1">
        {title}
      </div>
      {children}
    </div>
  );
}

function UsageStat({
  label,
  value,
  strong,
}: {
  label: string;
  value: number;
  strong?: boolean;
}) {
  return (
    <span
      className={cx(
        "rounded px-1.5 py-px",
        strong ? "bg-brand/15 text-brand font-semibold" : "bg-elevated text-muted",
      )}
    >
      {fmtTokens(value)}
      <span className="ml-1 text-[10px] uppercase tracking-wide opacity-70">{label}</span>
    </span>
  );
}

function KeyVals({ obj }: { obj: Record<string, unknown> }) {
  const entries = Object.entries(obj);
  if (entries.length === 0)
    return <span className="text-sm text-muted">—</span>;
  return (
    <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
      {entries.map(([k, v]) => (
        <div key={k} className="contents">
          <span className="font-mono text-xs text-muted">{k}</span>
          <span className="font-mono text-xs text-fg break-all">
            {typeof v === "string" ? v : JSON.stringify(v)}
          </span>
        </div>
      ))}
    </div>
  );
}

// --- shared timeline chrome --------------------------------------------------

function Spine() {
  return <span className="absolute left-[7px] top-4 bottom-0 w-px bg-border" />;
}

function Dot({ type }: { type: string }) {
  return (
    <span
      className={cx(
        "absolute left-0 top-1.5 h-3.5 w-3.5 rounded-full ring-4 ring-bg",
        eventDotColor(type),
      )}
    />
  );
}

/** Small presentation helpers shared across views. */

export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return String(iso);
  return d.toLocaleString();
}

export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null) return "";
  if (ms === 0) return "0 ms";
  if (ms < 1) return `${ms.toFixed(3)} ms`;
  if (ms < 10) return `${ms.toFixed(1)} ms`;
  if (ms < 1000) return `${ms.toFixed(0)} ms`;
  return `${(ms / 1000).toFixed(2)} s`;
}

/** Compact token count: 1234 → "1.2k", 1_200_000 → "1.2M". */
export function fmtTokens(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

/** Tailwind classes for a session/event status badge. */
export function statusClasses(status: string): string {
  switch (status) {
    case "done":
    case "success":
      return "bg-ok/15 text-ok border-ok/30";
    case "paused":
      return "bg-warn/15 text-warn border-warn/30";
    case "failure":
    case "error":
      return "bg-danger/15 text-danger border-danger/30";
    case "running":
      return "bg-info/15 text-info border-info/30";
    default:
      return "bg-muted/15 text-muted border-border";
  }
}

/** A short, human label for a trace event type. */
export function eventLabel(type: string): string {
  return (
    {
      session_start: "Session start",
      step_enter: "Step",
      action_before: "Action ▸ start",
      action_after: "Action ▸ done",
      slot_resolve: "Memory resolve",
      branch: "Branch",
      usage: "Token usage",
      paused: "Paused",
      session_end: "Session end",
    }[type] ?? type
  );
}

export function eventDotColor(type: string): string {
  switch (type) {
    case "session_start":
      return "bg-info";
    case "action_after":
      return "bg-brand-500";
    case "branch":
      return "bg-warn";
    case "slot_resolve":
      return "bg-info";
    case "paused":
      return "bg-warn";
    case "session_end":
      return "bg-ok";
    default:
      return "bg-muted";
  }
}

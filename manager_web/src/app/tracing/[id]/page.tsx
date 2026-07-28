"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { GateBadge } from "@/components/GateBadge";
import { RefreshButton } from "@/components/RefreshButton";
import { RequireAuth } from "@/components/RequireAuth";
import { StatusBadge } from "@/components/StatusBadge";
import { TraceGraph } from "@/components/TraceGraph";
import { TraceTimeline } from "@/components/TraceTimeline";
import {
  api,
  type RetryEventData,
  type SessionDoc,
  type VerificationEventData,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cx, fmtTime } from "@/lib/format";

export default function SessionDetailPage() {
  return (
    <RequireAuth>
      <SessionDetail />
    </RequireAuth>
  );
}

function deriveStatus(doc: SessionDoc): string {
  const last = doc.trace[doc.trace.length - 1];
  if (last?.type === "session_end")
    return (last.data as any)?.status ?? "done";
  if (last?.type === "paused") return "paused";
  return "running";
}

/** One gate evaluation, with the retry that followed it (if any) — the
 * auto-repair loop's story in order: run → verify → (fail → retry →
 * verify)* → done. */
type GateAttempt = { verification: VerificationEventData; retry: RetryEventData | null };

function gateAttemptsOf(doc: SessionDoc): GateAttempt[] {
  const out: GateAttempt[] = [];
  for (let i = 0; i < doc.trace.length; i++) {
    const ev = doc.trace[i];
    if (ev.type !== "verification") continue;
    const next = doc.trace[i + 1];
    out.push({
      verification: ev.data as unknown as VerificationEventData,
      retry: next?.type === "retry" ? (next.data as unknown as RetryEventData) : null,
    });
  }
  return out;
}

/** Collapse a session's gate attempts into the same `GateSummary` shape the
 * list endpoints return, so `GateBadge` renders identically everywhere. */
function lastGateSummary(attempts: GateAttempt[]) {
  const last = attempts[attempts.length - 1].verification;
  const retries = attempts.filter((a) => a.retry).length;
  return {
    passed: last.passed,
    attempts: attempts.length,
    retries,
    checks: last.checks,
  };
}

function SessionDetail() {
  const { id } = useParams<{ id: string }>();
  const { token, signOut } = useAuth();
  const [doc, setDoc] = useState<SessionDoc | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedStep, setSelectedStep] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token || !id) return;
    setError(null);
    try {
      setDoc(await api.getSession(token, id));
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setError(err?.message ?? "Failed to load session");
    }
  }, [token, id, signOut]);

  useEffect(() => {
    load();
  }, [load]);

  if (error)
    return (
      <div className="rounded-xl border border-danger/30 bg-danger/10 text-danger px-4 py-3 text-sm">
        {error}{" "}
        <Link href="/tracing" className="underline">
          Back to sessions
        </Link>
      </div>
    );
  if (!doc) return <div className="text-sm text-muted">Loading session…</div>;

  const status = deriveStatus(doc);
  const gateAttempts = gateAttemptsOf(doc);

  return (
    <div>
      {/* header */}
      <div className="flex items-start justify-between gap-4 mb-5">
        <div>
          <Link
            href="/tracing"
            className="text-sm text-muted hover:text-fg inline-flex items-center gap-1"
          >
            ← Tracing
          </Link>
          <h1 className="text-xl font-semibold text-fg mt-1">
            {doc.workflow.name ?? "Workflow run"}
          </h1>
          <div className="mt-1 flex items-center gap-3 text-sm text-muted">
            <StatusBadge status={status} />
            {gateAttempts.length > 0 && (
              <GateBadge gate={lastGateSummary(gateAttempts)} />
            )}
            <span>runtime: {doc.agent.runtime ?? "—"}</span>
            <code className="font-mono text-xs">{doc.session_id}</code>
          </div>
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {doc.workflow.name && (
            <Link
              href={`/workflows/${encodeURIComponent(doc.workflow.name)}`}
              className="inline-flex items-center gap-1 h-9 px-3 rounded-lg text-sm font-medium border border-border text-fg hover:bg-elevated"
            >
              Open in editor →
            </Link>
          )}
          <RefreshButton onRefresh={load} />
        </div>
      </div>

      {/* meta strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
        <Meta label="Started" value={fmtTime(doc.workflow.start)} />
        <Meta label="Ended" value={fmtTime(doc.workflow.end)} />
        <Meta label="Events" value={String(doc.trace.length)} />
        <Meta
          label="Initial memory"
          value={String(Object.keys(doc.workflow.initial_slots ?? {}).length)}
        />
      </div>

      {/* verification gate + auto-repair */}
      {gateAttempts.length > 0 && <GateCard attempts={gateAttempts} />}

      {/* graph + timeline */}
      <div className="grid lg:grid-cols-[1fr_24rem] gap-6 items-start">
        <section>
          <h2 className="text-sm font-medium text-fg mb-2">
            Trace & memory flow
          </h2>
          <TraceGraph
            doc={doc}
            selectedStep={selectedStep}
            onSelectStep={setSelectedStep}
          />
          <p className="text-xs text-muted mt-2">
            Steps run top-to-bottom. dashed lime edges show memory each step
            produced (the memory flow). Click a step to highlight it in the
            timeline.
          </p>
        </section>

        <section>
          <h2 className="text-sm font-medium text-fg mb-2">Event timeline</h2>
          <div className="max-h-[560px] overflow-auto pr-1">
            <TraceTimeline events={doc.trace} highlightStep={selectedStep} />
          </div>
        </section>
      </div>
    </div>
  );
}

/** Verification-gate summary card: overall verdict up top, then every
 * attempt in order (checks + why a retry followed), so the auto-repair loop
 * — run → verify → (fail → retry → verify)* → done/failure — reads as one
 * story instead of scattered trace-timeline rows. */
function GateCard({ attempts }: { attempts: GateAttempt[] }) {
  const summary = lastGateSummary(attempts);
  return (
    <section className="mb-6 rounded-2xl border border-border bg-surface overflow-hidden">
      <div className="px-4 py-3 flex items-center gap-3 border-b border-border">
        <h2 className="text-sm font-medium text-fg">Verification gate</h2>
        <GateBadge gate={summary} />
        <span className="ml-auto text-xs text-muted tabular-nums">
          {summary.attempts} attempt{summary.attempts === 1 ? "" : "s"}
          {summary.retries > 0 &&
            ` · ${summary.retries} auto-repair retr${summary.retries === 1 ? "y" : "ies"}`}
        </span>
      </div>
      <ol className="divide-y divide-border">
        {attempts.map((a, i) => (
          <GateAttemptRow key={i} attempt={a} index={i} isLast={i === attempts.length - 1} />
        ))}
      </ol>
    </section>
  );
}

function GateAttemptRow({
  attempt,
  index,
  isLast,
}: {
  attempt: GateAttempt;
  index: number;
  isLast: boolean;
}) {
  const v = attempt.verification;
  return (
    <li className="px-4 py-3">
      <div className="flex items-center gap-2">
        <span
          className={cx(
            "h-2 w-2 rounded-full shrink-0",
            v.passed ? "bg-ok" : "bg-danger",
          )}
        />
        <span className="text-sm font-medium text-fg">Attempt {index + 1}</span>
        <span
          className={cx(
            "text-[10px] font-medium uppercase tracking-wide rounded px-1.5 py-px",
            v.passed ? "bg-ok/15 text-ok" : "bg-danger/15 text-danger",
          )}
        >
          {v.passed ? "passed" : "failed"}
        </span>
      </div>
      <ul className="mt-2 space-y-1 pl-4">
        {(v.checks ?? []).map((c, i) => (
          <li key={i} className="flex items-start gap-2 text-sm">
            <span
              className={cx(
                "mt-0.5 h-1.5 w-1.5 rounded-full shrink-0",
                c.passed ? "bg-ok" : "bg-danger",
              )}
            />
            <span className="font-mono text-xs text-fg">{c.id}</span>
            <span className="text-[10px] uppercase tracking-wide text-muted">
              {c.severity}
            </span>
            {(c.error || c.detail) && (
              <span className="text-xs text-muted">{c.error || c.detail}</span>
            )}
          </li>
        ))}
      </ul>
      {attempt.retry && (
        <div className="mt-2 ml-4 rounded-lg border border-warn/30 bg-warn/10 px-2.5 py-1.5">
          <div className="text-[11px] font-medium uppercase tracking-wide text-warn">
            Auto-repair retry {attempt.retry.attempt}/{attempt.retry.max_attempts}
          </div>
          <ul className="list-disc pl-4 text-xs text-fg mt-0.5 space-y-0.5">
            {attempt.retry.reason.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
          <p className="text-[11px] text-muted mt-1">
            Workflow re-ran from the start with this feedback folded into memory.
          </p>
        </div>
      )}
      {!attempt.retry && !v.passed && isLast && index > 0 && (
        <p className="mt-2 ml-4 text-xs text-danger">
          Repair budget exhausted — run surfaced as a failure.
        </p>
      )}
    </li>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-xl border border-border bg-surface px-3 py-2">
      <div className="text-[11px] uppercase tracking-wide text-muted">
        {label}
      </div>
      <div className="text-sm text-fg mt-0.5 truncate">{value}</div>
    </div>
  );
}

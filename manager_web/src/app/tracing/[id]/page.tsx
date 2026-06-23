"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { RefreshButton } from "@/components/RefreshButton";
import { RequireAuth } from "@/components/RequireAuth";
import { StatusBadge } from "@/components/StatusBadge";
import { TraceGraph } from "@/components/TraceGraph";
import { TraceTimeline } from "@/components/TraceTimeline";
import { api, type SessionDoc } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtTime } from "@/lib/format";

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

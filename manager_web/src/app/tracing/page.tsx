"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshButton } from "@/components/RefreshButton";
import { RequireAuth } from "@/components/RequireAuth";
import { StatusBadge } from "@/components/StatusBadge";
import { api, type SessionSummary } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtTime } from "@/lib/format";

export default function TracingPage() {
  return (
    <RequireAuth>
      <TracingList />
    </RequireAuth>
  );
}

function TracingList() {
  const { token, signOut } = useAuth();
  const router = useRouter();
  const searchParams = useSearchParams();
  const workflowFilter = searchParams.get("workflow");
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const visibleSessions = useMemo(() => {
    if (!sessions || !workflowFilter) return sessions;
    return sessions.filter((s) => s.workflow === workflowFilter);
  }, [sessions, workflowFilter]);

  const load = useCallback(async () => {
    if (!token) return;
    setError(null);
    try {
      setSessions(await api.listSessions(token));
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setError(err?.message ?? "Failed to load sessions");
    }
  }, [token, signOut]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div>
      <div className="flex items-end justify-between gap-4 mb-6">
        <div>
          <h1 className="text-xl font-semibold text-fg">Tracing</h1>
          <p className="text-sm text-muted mt-1">
            Workflow execution sessions. Click a session to view its trace and
            memory flow.
          </p>
          {workflowFilter && (
            <div className="mt-2 inline-flex items-center gap-2 text-sm">
              <span className="inline-flex items-center gap-1 rounded-full border border-border bg-elevated px-2.5 py-0.5 text-xs text-fg">
                workflow: <span className="font-mono">{workflowFilter}</span>
              </span>
              <Link href="/tracing" className="text-muted hover:text-fg underline">
                clear
              </Link>
            </div>
          )}
        </div>
        <RefreshButton onRefresh={load} />
      </div>

      {error && (
        <div className="rounded-xl border border-danger/30 bg-danger/10 text-danger px-4 py-3 text-sm">
          {error}
        </div>
      )}

      {!error && sessions === null && (
        <div className="text-sm text-muted">Loading sessions…</div>
      )}

      {!error && visibleSessions?.length === 0 && (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-10 text-center">
          <p className="text-fg font-medium">
            {workflowFilter ? "No sessions for this workflow" : "No sessions yet"}
          </p>
          <p className="text-sm text-muted mt-1">
            Run a workflow (<code className="font-mono">botcircuits workflow run</code>)
            and its trace will appear here.
          </p>
        </div>
      )}

      {visibleSessions && visibleSessions.length > 0 && (
        <div className="overflow-hidden rounded-2xl border border-border bg-surface">
          <table className="w-full text-sm">
            <thead className="text-muted text-xs uppercase tracking-wide bg-elevated/50">
              <tr>
                <Th>Workflow</Th>
                <Th>Status</Th>
                <Th>Runtime</Th>
                <Th>Events</Th>
                <Th>Started</Th>
                <Th>Session</Th>
                <Th></Th>
              </tr>
            </thead>
            <tbody>
              {visibleSessions.map((s) => (
                <tr
                  key={s.session_id}
                  onClick={() => router.push(`/tracing/${s.session_id}`)}
                  className="border-t border-border hover:bg-elevated/50 cursor-pointer"
                >
                  <Td>
                    <Link
                      href={`/tracing/${s.session_id}`}
                      className="font-medium text-brand-600 dark:text-brand-400"
                    >
                      {s.workflow ?? "—"}
                    </Link>
                  </Td>
                  <Td>
                    <StatusBadge status={s.status} />
                  </Td>
                  <Td className="text-muted">{s.runtime ?? "—"}</Td>
                  <Td className="text-muted tabular-nums">{s.event_count}</Td>
                  <Td className="text-muted">{fmtTime(s.start)}</Td>
                  <Td>
                    <code className="font-mono text-xs text-muted">
                      {s.session_id.slice(0, 10)}
                    </code>
                  </Td>
                  <Td className="text-right w-10">
                    <Link
                      href={`/tracing/${s.session_id}`}
                      aria-label={`View trace for ${s.workflow ?? s.session_id}`}
                      className="inline-flex text-brand-600 dark:text-brand-400 hover:bg-elevated rounded-lg p-1.5"
                    >
                      <ChevronIcon />
                    </Link>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

const ChevronIcon = () => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2.5"
    strokeLinecap="round"
    strokeLinejoin="round"
    className="inline-block"
  >
    <path d="m9 18 6-6-6-6" />
  </svg>
);

const Th = ({ children }: { children?: React.ReactNode }) => (
  <th className="text-left font-medium px-4 py-3">{children}</th>
);
const Td = ({
  children,
  className = "",
}: {
  children: React.ReactNode;
  className?: string;
}) => <td className={`px-4 py-3 ${className}`}>{children}</td>;

"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { RefreshButton } from "@/components/RefreshButton";
import { RequireAuth } from "@/components/RequireAuth";
import { EditIcon, PlusIcon, TrashIcon } from "@/components/icons";
import { api, type WorkflowSummary } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { fmtTime } from "@/lib/format";

export default function WorkflowsPage() {
  return (
    <RequireAuth>
      <WorkflowList />
    </RequireAuth>
  );
}

function WorkflowList() {
  const { token, signOut } = useAuth();
  const router = useRouter();
  const [items, setItems] = useState<WorkflowSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<WorkflowSummary | null>(null);

  const load = useCallback(async () => {
    if (!token) return;
    setError(null);
    try {
      setItems(await api.listWorkflows(token));
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setError(err?.message ?? "Failed to load workflows");
    }
  }, [token, signOut]);

  useEffect(() => {
    load();
  }, [load]);

  const confirmDelete = useCallback(async () => {
    if (!token || !deleting) return;
    try {
      await api.deleteWorkflow(token, deleting.name);
      setDeleting(null);
      load();
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setError(err?.message ?? "Failed to delete workflow");
      setDeleting(null);
    }
  }, [token, deleting, load, signOut]);

  return (
    <div>
      <div className="flex items-end justify-between gap-4 mb-6">
        <div>
          <h1 className="text-xl font-semibold text-fg">Workflows</h1>
          <p className="text-sm text-muted mt-1">
            Author and edit deterministic workflows. Edit visually or as JSON,
            or describe one in natural language.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <RefreshButton onRefresh={load} indicator={false} />
          <Link
            href="/workflows/new"
            className="inline-flex items-center gap-2 h-9 px-3 rounded-lg text-sm font-semibold bg-brand text-zinc-900 hover:bg-brand-300"
          >
            <PlusIcon className="w-[18px] h-[18px]" />
            New workflow
          </Link>
        </div>
      </div>

      {error && (
        <div className="rounded-xl border border-danger/30 bg-danger/10 text-danger px-4 py-3 text-sm mb-4">
          {error}
        </div>
      )}

      {!error && items === null && (
        <div className="text-sm text-muted">Loading workflows…</div>
      )}

      {!error && items?.length === 0 && (
        <div className="rounded-2xl border border-dashed border-border bg-surface p-10 text-center">
          <p className="text-fg font-medium">No workflows yet</p>
          <p className="text-sm text-muted mt-1">
            Create one with{" "}
            <Link href="/workflows/new" className="text-brand-600 dark:text-brand-400">
              New workflow
            </Link>
            .
          </p>
        </div>
      )}

      {items && items.length > 0 && (
        <div className="overflow-hidden rounded-2xl border border-border bg-surface">
          <table className="w-full text-sm">
            <thead className="text-muted text-xs uppercase tracking-wide bg-elevated/50">
              <tr>
                <Th>Name</Th>
                <Th>Description</Th>
                <Th>Steps</Th>
                <Th>Build</Th>
                <Th>Updated</Th>
                <Th></Th>
              </tr>
            </thead>
            <tbody>
              {items.map((w) => (
                <tr
                  key={w.name}
                  onClick={() => router.push(`/workflows/${encodeURIComponent(w.name)}`)}
                  className="border-t border-border hover:bg-elevated/50 cursor-pointer"
                >
                  <Td>
                    <span className="font-medium text-brand-600 dark:text-brand-400 font-mono">
                      {w.name}
                    </span>
                  </Td>
                  <Td className="text-muted max-w-md">
                    <span className="line-clamp-1">{w.description || "—"}</span>
                  </Td>
                  <Td className="text-muted tabular-nums">{w.step_count}</Td>
                  <Td>
                    <span
                      className={
                        "inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium " +
                        (w.built
                          ? "bg-ok/15 text-ok border-ok/30"
                          : "bg-warn/15 text-warn border-warn/30")
                      }
                    >
                      {w.built ? "built" : "not built"}
                    </span>
                  </Td>
                  <Td className="text-muted">{fmtTime(new Date(w.updated_at * 1000).toISOString())}</Td>
                  <Td className="text-right w-24">
                    <div
                      className="flex items-center justify-end gap-1"
                      onClick={(e) => e.stopPropagation()}
                    >
                      <Link
                        href={`/workflows/${encodeURIComponent(w.name)}`}
                        aria-label={`Edit ${w.name}`}
                        title="Edit"
                        className="inline-flex text-muted hover:text-fg hover:bg-elevated rounded-lg p-1.5"
                      >
                        <EditIcon className="w-[18px] h-[18px]" />
                      </Link>
                      <button
                        onClick={() => setDeleting(w)}
                        aria-label={`Delete ${w.name}`}
                        title="Delete"
                        className="inline-flex text-muted hover:text-danger hover:bg-danger/10 rounded-lg p-1.5"
                      >
                        <TrashIcon className="w-[18px] h-[18px]" />
                      </button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {deleting && (
        <ConfirmDialog
          title={`Delete workflow “${deleting.name}”?`}
          body="This removes the source file and its built copy. This cannot be undone."
          confirmLabel="Delete"
          onCancel={() => setDeleting(null)}
          onConfirm={confirmDelete}
        />
      )}
    </div>
  );
}

function ConfirmDialog({
  title,
  body,
  confirmLabel,
  onConfirm,
  onCancel,
}: {
  title: string;
  body: string;
  confirmLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-black/40 backdrop-blur-sm p-4"
      onClick={onCancel}
    >
      <div
        className="w-full max-w-md rounded-2xl border border-border bg-surface p-5 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-base font-semibold text-fg">{title}</h2>
        <p className="text-sm text-muted mt-2">{body}</p>
        <div className="mt-5 flex justify-end gap-2">
          <button
            onClick={onCancel}
            className="h-9 px-3 rounded-lg text-sm text-muted hover:text-fg hover:bg-elevated"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="h-9 px-3 rounded-lg text-sm font-medium bg-danger text-white hover:bg-danger/90"
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}

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

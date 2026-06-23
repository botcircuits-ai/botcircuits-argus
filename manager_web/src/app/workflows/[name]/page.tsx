"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { RequireAuth } from "@/components/RequireAuth";
import { WorkflowEditor } from "@/components/WorkflowEditor";
import { api, type WorkflowDoc } from "@/lib/api";
import { useAuth } from "@/lib/auth";

export default function WorkflowEditPage() {
  return (
    <RequireAuth>
      <WorkflowEdit />
    </RequireAuth>
  );
}

function WorkflowEdit() {
  const { name } = useParams<{ name: string }>();
  const { token, signOut } = useAuth();
  const [doc, setDoc] = useState<WorkflowDoc | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!token || !name) return;
    setError(null);
    try {
      setDoc(await api.getWorkflow(token, name));
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setError(err?.message ?? "Failed to load workflow");
    }
  }, [token, name, signOut]);

  useEffect(() => {
    load();
  }, [load]);

  if (error)
    return (
      <div>
        <div className="rounded-xl border border-danger/30 bg-danger/10 text-danger px-4 py-3 text-sm">
          {error}
        </div>
        <Link
          href="/workflows"
          className="inline-block mt-4 text-sm text-brand-600 dark:text-brand-400"
        >
          ← Back to workflows
        </Link>
      </div>
    );

  if (doc === null)
    return <div className="text-sm text-muted">Loading workflow…</div>;

  return (
    <WorkflowEditor initialName={decodeURIComponent(name)} initialDoc={doc} isNew={false} />
  );
}

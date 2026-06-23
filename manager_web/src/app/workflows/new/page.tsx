"use client";

import { RequireAuth } from "@/components/RequireAuth";
import { WorkflowEditor } from "@/components/WorkflowEditor";

export default function NewWorkflowPage() {
  return (
    <RequireAuth>
      <WorkflowEditor initialName="" initialDoc={null} isNew />
    </RequireAuth>
  );
}

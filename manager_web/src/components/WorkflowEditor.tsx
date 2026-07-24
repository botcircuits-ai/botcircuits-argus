"use client";

import { useCallback, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { AuthoringChat } from "@/components/AuthoringChat";
import { StepPanel } from "@/components/StepPanel";
import { WorkflowGraph, type EdgeKind, type EdgeRef } from "@/components/WorkflowGraph";
import { CodeIcon, SparkleIcon, WorkflowIcon } from "@/components/icons";
import {
  api,
  STEP_TYPE_AGENT_ACTION,
  STEP_TYPE_PARALLEL,
  type BuildResult,
  type WorkflowDoc,
  type WorkflowStep,
} from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cx } from "@/lib/format";

type Mode = "flow" | "json";

/**
 * Normalize a step's branching in place: if it has no default `next` and a
 * single condition whose test is empty, that lone connection is really the
 * default path — promote it to `next` and drop the empty `conditions`. Keeps
 * the model honest after edits that leave a step with one unconditioned edge.
 *
 * A `parallel` step has no `conditions` at all (its branches ARE the
 * multi-way fan-out) — never apply this collapse to one.
 */
function normalizeStep(step: WorkflowStep): void {
  if (!step || step.type === STEP_TYPE_PARALLEL) return;
  const conditions = step.conditions ?? [];
  if (!step.next && conditions.length === 1 && !(conditions[0].condition ?? "").trim()) {
    step.next = conditions[0].next;
    delete step.conditions;
  }
}

/** Every step id claimed by some `parallel` step's branches, mapped to
 * `{parallelStep, branch, index}` — the step's position within that branch's
 * chain. Used by the connect/rename/delete handlers below to route a
 * mutation into `branches` instead of `next`/`conditions` when the step in
 * question is part of a fan-out. */
function branchIndex(
  steps: Record<string, WorkflowStep>,
): Map<string, { parallelStep: string; branch: string; index: number }> {
  const idx = new Map<string, { parallelStep: string; branch: string; index: number }>();
  for (const [id, s] of Object.entries(steps)) {
    if (s.type !== STEP_TYPE_PARALLEL) continue;
    for (const [branch, chain] of Object.entries(s.branches ?? {})) {
      chain.forEach((stepId, index) => idx.set(stepId, { parallelStep: id, branch, index }));
    }
  }
  return idx;
}

function emptyDoc(name: string): WorkflowDoc {
  return {
    name,
    description: "",
    flow: {
      start: "start",
      steps: {
        start: { type: "start", next: "", id: "start" },
      },
    },
  };
}

/**
 * Workflow create/edit view. Two synced modes over a single `WorkflowDoc`
 * source of truth:
 *   - "flow": ReactFlow canvas + per-step side panel.
 *   - "json": raw JSON textarea.
 * Both edit the same `doc` state, so switching modes never loses changes.
 * A natural-language authoring chat (right rail) can replace the whole doc.
 */
export function WorkflowEditor({
  initialName,
  initialDoc,
  isNew,
}: {
  initialName: string;
  initialDoc: WorkflowDoc | null;
  isNew: boolean;
}) {
  const { token, signOut } = useAuth();
  const router = useRouter();

  const [name, setName] = useState(initialName);
  const [doc, setDoc] = useState<WorkflowDoc>(
    initialDoc ?? emptyDoc(initialName || "new_workflow"),
  );
  const [mode, setMode] = useState<Mode>("flow");
  const [showChat, setShowChat] = useState(isNew);
  const [selectedStep, setSelectedStep] = useState<string | null>(null);

  // JSON-mode buffer + parse error. We keep the raw text separate so an
  // in-progress (temporarily invalid) edit isn't discarded; on every valid
  // parse we push it back into `doc` so the flow view stays in sync.
  const [jsonText, setJsonText] = useState(() => JSON.stringify(doc, null, 2));
  const [jsonError, setJsonError] = useState<string | null>(null);

  const [saving, setSaving] = useState(false);
  const [building, setBuilding] = useState(false);
  const [banner, setBanner] = useState<{ kind: "ok" | "err"; text: string } | null>(
    null,
  );
  const [buildOutput, setBuildOutput] = useState<BuildResult | null>(null);

  const nameValid = /^[a-zA-Z0-9_-]+$/.test(name);

  // Replace the whole doc (from JSON edits, the side panel, or AI authoring)
  // and refresh the JSON buffer so both modes reflect it.
  const applyDoc = useCallback((next: WorkflowDoc) => {
    setDoc(next);
    setJsonText(JSON.stringify(next, null, 2));
    setJsonError(null);
  }, []);

  // Switching INTO json: serialize current doc. Switching FROM json: try to
  // commit the buffer (block the switch if it's invalid).
  const switchMode = useCallback(
    (next: Mode) => {
      if (next === mode) return;
      if (mode === "json") {
        try {
          const parsed = JSON.parse(jsonText);
          setDoc(parsed);
          setJsonError(null);
        } catch (e: any) {
          setJsonError(e?.message ?? "Invalid JSON");
          return;
        }
      } else {
        setJsonText(JSON.stringify(doc, null, 2));
      }
      setMode(next);
    },
    [mode, jsonText, doc],
  );

  const onJsonChange = useCallback((text: string) => {
    setJsonText(text);
    try {
      const parsed = JSON.parse(text);
      setDoc(parsed);
      setJsonError(null);
    } catch (e: any) {
      setJsonError(e?.message ?? "Invalid JSON");
    }
  }, []);

  const onAuthored = useCallback(
    (authored: WorkflowDoc) => {
      applyDoc(authored);
      if (authored.name && typeof authored.name === "string") setName(authored.name);
      setBanner({ kind: "ok", text: "Workflow generated and synced into the editor." });
    },
    [applyDoc],
  );

  const steps = doc.flow?.steps ?? {};
  const selected = selectedStep ? steps[selectedStep] : null;

  // Pending "create new step?" prompt raised by a drag-to-empty-canvas; holds
  // the source step the new node would be connected from.
  const [pendingNewFrom, setPendingNewFrom] = useState<string | null>(null);

  // --- Graph mutation helpers (canvas edits) -------------------------------
  // All produce a new doc through applyDoc so the JSON view stays in sync.
  const mutateSteps = useCallback(
    (fn: (steps: Record<string, WorkflowStep>) => Partial<WorkflowDoc> | void) => {
      const nextSteps: Record<string, WorkflowStep> = JSON.parse(
        JSON.stringify(doc.flow?.steps ?? {}),
      );
      const extra = fn(nextSteps) || {};
      for (const id of Object.keys(nextSteps)) normalizeStep(nextSteps[id]);
      applyDoc({ ...doc, ...extra, flow: { ...(doc.flow ?? {}), steps: nextSteps } });
    },
    [doc, applyDoc],
  );

  const renameStep = useCallback(
    (oldId: string, newId: string) => {
      if (!newId || newId === oldId) return;
      const cur = doc.flow?.steps ?? {};
      if (cur[newId]) return; // name collision — ignore
      const nextSteps: Record<string, WorkflowStep> = JSON.parse(JSON.stringify(cur));
      const old = nextSteps[oldId];
      if (!old) return;
      delete nextSteps[oldId];
      nextSteps[newId] = { ...old, id: newId };
      for (const v of Object.values(nextSteps)) {
        if (v.next === oldId) v.next = newId;
        if (v.onError === oldId) v.onError = newId;
        if (v.conditions)
          v.conditions = v.conditions.map((c) =>
            c.next === oldId ? { ...c, next: newId } : c,
          );
        if (v.branches)
          v.branches = Object.fromEntries(
            Object.entries(v.branches).map(([branch, chain]) => [
              branch,
              chain.map((sid) => (sid === oldId ? newId : sid)),
            ]),
          );
      }
      const start = doc.flow?.start === oldId ? newId : doc.flow?.start;
      applyDoc({ ...doc, flow: { ...(doc.flow ?? {}), start, steps: nextSteps } });
      if (selectedStep === oldId) setSelectedStep(newId);
    },
    [doc, applyDoc, selectedStep],
  );

  const updateAction = useCallback(
    (id: string, action: string) =>
      mutateSteps((s) => {
        if (s[id]) s[id] = { ...s[id], settings: { ...(s[id].settings ?? {}), action } };
      }),
    [mutateSteps],
  );

  const updateEdgeCondition = useCallback(
    (from: string, kind: EdgeKind, condIndex: number, condition: string) => {
      // `parallel`-derived edges (fan-out/branch chain/join/onError) are
      // read-only labels — `ConditionEdge` renders `ParallelEdgeLabel` for
      // them instead of an editable `EdgeConditionInput`, so this callback
      // should never actually fire for those kinds. No-op defensively.
      if (kind !== "condition" && kind !== "default") return;
      mutateSteps((s) => {
        const step = s[from];
        if (!step) return;
        if (kind === "condition" && step.conditions?.[condIndex]) {
          const conditions = [...step.conditions];
          conditions[condIndex] = { ...conditions[condIndex], condition };
          s[from] = { ...step, conditions };
        } else if (kind === "default") {
          // Editing the default ("otherwise") edge: setting a condition converts
          // it into a regular branch, leaving the step with no default path
          // (the backend tolerates that). Clearing it again leaves it default.
          if (condition.trim() && step.next) {
            const conditions = [...(step.conditions ?? []), { condition, next: step.next }];
            s[from] = { ...step, conditions, next: "" };
          }
        }
      });
    },
    [mutateSteps],
  );

  // Connect two existing steps: fill the default `next` if empty, otherwise add
  // a new (empty-condition) branch the user can then label inline.
  // Ambiguous "connect from a parallel step with 2+ empty branches" case:
  // holds the pending (from, to) pair while the user picks which branch.
  const [pendingBranchPick, setPendingBranchPick] = useState<
    { parallelStep: string; to: string; emptyBranches: string[] } | null
  >(null);

  /** Append `stepId` to `branches[branch]` on the given parallel step. */
  const appendToBranch = useCallback(
    (parallelStep: string, branch: string, stepId: string) => {
      mutateSteps((s) => {
        const step = s[parallelStep];
        if (!step) return;
        const branches = { ...(step.branches ?? {}) };
        branches[branch] = [...(branches[branch] ?? []), stepId];
        s[parallelStep] = { ...step, branches };
      });
    },
    [mutateSteps],
  );

  const connectSteps = useCallback(
    (from: string, to: string) => {
      if (from === to) return;
      const cur = doc.flow?.steps ?? {};
      const fromStep = cur[from];
      if (!fromStep || !cur[to]) return;

      // Dragging FROM a parallel step: route into `branches`, not `next`.
      if (fromStep.type === STEP_TYPE_PARALLEL) {
        const branches = fromStep.branches ?? {};
        const already = Object.values(branches).some((chain) => chain.includes(to));
        if (already) return; // already wired into some branch
        const empty = Object.keys(branches).filter((b) => (branches[b] ?? []).length === 0);
        if (empty.length === 1) {
          appendToBranch(from, empty[0], to);
        } else if (empty.length > 1) {
          setPendingBranchPick({ parallelStep: from, to, emptyBranches: empty });
        } else {
          // No empty branch to fill — create a new one named after `to`.
          appendToBranch(from, to, to);
        }
        return;
      }

      // Dragging FROM a step that's already the LAST step of some branch
      // chain: extend that chain instead of setting `next`/`conditions` (a
      // branch step is validated build-time to carry neither).
      const idx = branchIndex(cur).get(from);
      if (idx && idx.index === (cur[idx.parallelStep]?.branches?.[idx.branch]?.length ?? 0) - 1) {
        appendToBranch(idx.parallelStep, idx.branch, to);
        return;
      }

      mutateSteps((s) => {
        const step = s[from];
        if (!step || !s[to]) return;
        if (!step.next) {
          s[from] = { ...step, next: to };
        } else if (step.next === to) {
          // already the default — nothing to do
        } else {
          const conditions = [...(step.conditions ?? [])];
          if (!conditions.some((c) => c.next === to)) {
            conditions.push({ condition: "", next: to });
          }
          s[from] = { ...step, conditions };
        }
      });
    },
    [doc, mutateSteps, appendToBranch],
  );

  const pickBranch = useCallback(
    (branch: string) => {
      const pick = pendingBranchPick;
      setPendingBranchPick(null);
      if (!pick) return;
      appendToBranch(pick.parallelStep, branch, pick.to);
    },
    [pendingBranchPick, appendToBranch],
  );

  // Drag ended on empty canvas — ask before creating.
  const requestNewStep = useCallback((from: string) => setPendingNewFrom(from), []);

  const createConnectedStep = useCallback(() => {
    const from = pendingNewFrom;
    setPendingNewFrom(null);
    if (!from) return;
    const cur = doc.flow?.steps ?? {};
    let n = Object.keys(cur).length + 1;
    let id = `step_${n}`;
    while (cur[id]) id = `step_${++n}`;
    const fromStep = cur[from];
    const idx = branchIndex(cur).get(from);

    mutateSteps((s) => {
      s[id] = { type: STEP_TYPE_AGENT_ACTION, settings: { action: "" }, next: "", id };

      if (fromStep?.type === STEP_TYPE_PARALLEL) {
        // New step becomes a brand-new branch (named after itself) unless
        // there's exactly one empty branch to fill — ambiguity here (2+
        // empty branches) isn't expected from an empty-canvas drop, since
        // that flow always creates a FRESH step rather than reusing one.
        const branches = { ...(fromStep.branches ?? {}) };
        const empty = Object.keys(branches).filter((b) => (branches[b] ?? []).length === 0);
        const branch = empty[0] ?? id;
        branches[branch] = [...(branches[branch] ?? []), id];
        s[from] = { ...fromStep, branches };
        return;
      }

      if (idx && idx.index === (fromStep?.branches?.[idx.branch]?.length ?? 0) - 1) {
        // `from` is the last step of a branch chain — extend it.
        const parallelStep = s[idx.parallelStep];
        if (parallelStep) {
          const branches = { ...(parallelStep.branches ?? {}) };
          branches[idx.branch] = [...(branches[idx.branch] ?? []), id];
          s[idx.parallelStep] = { ...parallelStep, branches };
        }
        return;
      }

      const step = s[from];
      if (step) {
        if (!step.next) s[from] = { ...step, next: id };
        else s[from] = { ...step, conditions: [...(step.conditions ?? []), { condition: "", next: id }] };
      }
    });
    setSelectedStep(id);
  }, [pendingNewFrom, doc, mutateSteps]);

  // --- Deletion (confirm-then-mutate) --------------------------------------
  const [pendingDeleteStep, setPendingDeleteStep] = useState<string | null>(null);
  const [pendingDeleteEdge, setPendingDeleteEdge] = useState<EdgeRef | null>(null);

  const confirmDeleteStep = useCallback(() => {
    const id = pendingDeleteStep;
    setPendingDeleteStep(null);
    if (!id) return;
    mutateSteps((s) => {
      delete s[id];
      // Clear dangling references so the graph/build stay consistent.
      for (const v of Object.values(s)) {
        if (v.next === id) v.next = "";
        if (v.onError === id) v.onError = undefined;
        if (v.conditions) v.conditions = v.conditions.filter((c) => c.next !== id);
        if (v.type === STEP_TYPE_PARALLEL && v.branches) {
          // Splice the deleted step out of any branch chain it was in — the
          // chain reconnects automatically (the step before it now leads
          // straight to the step after it; if it was the only step, the
          // branch becomes empty rather than being removed, so the author
          // can still fill it back in from the canvas).
          v.branches = Object.fromEntries(
            Object.entries(v.branches).map(([branch, chain]) => [
              branch,
              chain.filter((sid) => sid !== id),
            ]),
          );
        }
      }
    });
    if (selectedStep === id) setSelectedStep(null);
  }, [pendingDeleteStep, mutateSteps, selectedStep]);

  const confirmDeleteEdge = useCallback(() => {
    const ref = pendingDeleteEdge;
    setPendingDeleteEdge(null);
    if (!ref) return;
    mutateSteps((s) => {
      const step = s[ref.from];
      if (!step) return;
      if (ref.kind === "default") {
        s[ref.from] = { ...step, next: "" };
      } else if (ref.kind === "onError") {
        s[ref.from] = { ...step, onError: undefined };
      } else if (ref.kind === "parallelFanout") {
        // Remove the whole branch this fan-out edge starts (its label IS
        // the branch name — see `buildGraph`'s makeEdge(..., branch, ...)).
        const branches = { ...(step.branches ?? {}) };
        for (const [branch, chain] of Object.entries(branches)) {
          if (chain[0] === ref.to) delete branches[branch];
        }
        s[ref.from] = { ...step, branches };
      } else if (ref.kind === "branchJoin") {
        // `ref.from` is a BRANCH's last step, not the parallel step itself
        // (see `buildGraph`: makeEdge(last, parallel.next, ...)) — every
        // branch shares the one `next` on the owning parallel step, so
        // resolve that step and clear its `next` there.
        const owner = branchIndex(s).get(ref.from);
        if (owner && s[owner.parallelStep]) {
          s[owner.parallelStep] = { ...s[owner.parallelStep], next: "" };
        }
      } else if (step.conditions) {
        s[ref.from] = {
          ...step,
          conditions: step.conditions.filter((_, i) => i !== ref.condIndex),
        };
      }
    });
  }, [pendingDeleteEdge, mutateSteps]);

  const save = useCallback(async () => {
    if (!token) return;
    if (!nameValid) {
      setBanner({ kind: "err", text: "Name must match ^[a-zA-Z0-9_-]+$." });
      return;
    }
    if (mode === "json" && jsonError) {
      setBanner({ kind: "err", text: "Fix the JSON before saving." });
      return;
    }
    let toSave = doc;
    if (mode === "json") {
      try {
        toSave = JSON.parse(jsonText);
      } catch {
        setBanner({ kind: "err", text: "Fix the JSON before saving." });
        return;
      }
    }
    setSaving(true);
    setBanner(null);
    try {
      const saved = await api.saveWorkflow(token, name, { ...toSave, name });
      applyDoc(saved);
      setBanner({ kind: "ok", text: "Saved." });
      if (isNew) router.replace(`/workflows/${encodeURIComponent(name)}`);
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setBanner({ kind: "err", text: err?.message ?? "Save failed" });
    } finally {
      setSaving(false);
    }
  }, [token, nameValid, mode, jsonError, doc, jsonText, name, applyDoc, isNew, router, signOut]);

  const build = useCallback(async () => {
    if (!token || !nameValid) return;
    setBuilding(true);
    setBanner(null);
    setBuildOutput(null);
    try {
      // Persist first so the build compiles the latest source.
      await save();
      const result = await api.buildWorkflow(token, name);
      setBuildOutput(result);
      setBanner(
        result.ok
          ? { kind: "ok", text: "Built — the workflow is now runnable." }
          : { kind: "err", text: "Build failed. See output below." },
      );
    } catch (err: any) {
      if (err?.status === 401) return signOut();
      setBanner({ kind: "err", text: err?.message ?? "Build failed" });
    } finally {
      setBuilding(false);
    }
  }, [token, nameValid, name, save, signOut]);

  return (
    <div className="flex flex-col h-[calc(100vh-7rem)]">
      {/* Header / toolbar — sticky so it stays visible while editing. */}
      <div className="sticky top-0 z-20 shrink-0 bg-bg/95 backdrop-blur border-b border-border pb-3">
        <div className="flex items-center justify-between gap-3 mb-2">
          <Link
            href="/workflows"
            className="text-sm text-muted hover:text-fg inline-flex items-center gap-1"
          >
            ← Workflows
          </Link>
          <Link
            href={name ? `/tracing?workflow=${encodeURIComponent(name)}` : "/tracing"}
            className="text-sm text-muted hover:text-fg inline-flex items-center gap-1"
          >
            View traces →
          </Link>
        </div>
        <p className="text-sm text-muted mb-3">
          Design your workflow as a flow of steps and branches, then save &amp;
          build to make it runnable. Inspect past runs in{" "}
          <Link
            href={name ? `/tracing?workflow=${encodeURIComponent(name)}` : "/tracing"}
            className="text-fg underline hover:text-brand-400"
          >
            tracing
          </Link>
          .
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2 min-w-0">
            <WorkflowIcon className="w-5 h-5 text-brand-600 dark:text-brand-400 shrink-0" />
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="workflow_name"
              className={cx(
                "font-mono text-sm rounded-lg border bg-bg px-3 h-9 text-fg focus:outline-none focus:ring-2 focus:ring-brand/40",
                nameValid ? "border-border" : "border-danger/50",
              )}
            />
          </div>

          <ModeToggle mode={mode} onChange={switchMode} />

        <div className="flex-1" />

        <button
          onClick={() => setShowChat((s) => !s)}
          className={cx(
            "inline-flex items-center gap-2 h-9 px-3 rounded-lg text-sm",
            showChat
              ? "bg-brand/15 text-fg ring-1 ring-brand/30"
              : "text-muted hover:text-fg hover:bg-elevated",
          )}
        >
          <SparkleIcon className="w-[18px] h-[18px]" />
          AI
        </button>
        <button
          onClick={save}
          disabled={saving}
          className="h-9 px-3 rounded-lg text-sm font-medium border border-border text-fg hover:bg-elevated disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          onClick={build}
          disabled={building}
          className="h-9 px-3 rounded-lg text-sm font-semibold bg-brand text-zinc-900 hover:bg-brand-300 disabled:opacity-50"
        >
          {building ? "Building…" : "Save & Build"}
        </button>
        </div>
      </div>

      {banner && (
        <div
          className={cx(
            "shrink-0 mt-3 rounded-xl border px-4 py-2 text-sm",
            banner.kind === "ok"
              ? "border-ok/30 bg-ok/10 text-ok"
              : "border-danger/30 bg-danger/10 text-danger",
          )}
        >
          {banner.text}
        </div>
      )}

      {/* Body */}
      <div className="flex-1 min-h-0 mt-3 flex gap-3">
        <div className="flex-1 min-w-0 flex gap-3">
          {mode === "flow" ? (
            <>
              <div className="flex-1 min-w-0">
                <WorkflowGraph
                  doc={doc}
                  selectedStep={selectedStep}
                  onSelectStep={setSelectedStep}
                  onRenameStep={renameStep}
                  onUpdateAction={updateAction}
                  onUpdateEdgeCondition={updateEdgeCondition}
                  onConnect={connectSteps}
                  onConnectToEmpty={requestNewStep}
                  onRequestDeleteStep={setPendingDeleteStep}
                  onRequestDeleteEdge={setPendingDeleteEdge}
                />
              </div>
              <div className="w-[320px] shrink-0 overflow-y-auto">
                <StepPanel
                  doc={doc}
                  selectedStep={selectedStep}
                  step={selected ?? null}
                  onSelectStep={setSelectedStep}
                  onChange={applyDoc}
                  onRequestDeleteStep={setPendingDeleteStep}
                />
              </div>
            </>
          ) : (
            <div className="flex-1 min-w-0 flex flex-col">
              {jsonError && (
                <div className="mb-2 text-xs text-danger font-mono">{jsonError}</div>
              )}
              <textarea
                value={jsonText}
                onChange={(e) => onJsonChange(e.target.value)}
                spellCheck={false}
                className="flex-1 w-full resize-none rounded-2xl border border-border bg-bg p-4 font-mono text-[13px] leading-relaxed text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
              />
            </div>
          )}
        </div>

        {showChat && (
          <div className="w-[360px] shrink-0 h-full min-h-0">
            <AuthoringChat name={name} onWorkflow={onAuthored} />
          </div>
        )}
      </div>

      {buildOutput && (
        <pre className="shrink-0 mt-3 max-h-40 overflow-y-auto rounded-xl border border-border bg-bg p-3 font-mono text-[11px] text-muted whitespace-pre-wrap">
          {buildOutput.stdout}
          {buildOutput.stderr}
        </pre>
      )}

      {pendingNewFrom && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/40 backdrop-blur-sm p-4"
          onClick={() => setPendingNewFrom(null)}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-border bg-surface p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold text-fg">Create a new step?</h2>
            <p className="text-sm text-muted mt-2">
              Add a new step connected from{" "}
              <span className="font-mono text-fg">{pendingNewFrom}</span>.
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                onClick={() => setPendingNewFrom(null)}
                className="h-9 px-3 rounded-lg text-sm text-muted hover:text-fg hover:bg-elevated"
              >
                Cancel
              </button>
              <button
                onClick={createConnectedStep}
                className="h-9 px-3 rounded-lg text-sm font-semibold bg-brand text-zinc-900 hover:bg-brand-300"
              >
                Create step
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingBranchPick && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/40 backdrop-blur-sm p-4"
          onClick={() => setPendingBranchPick(null)}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-border bg-surface p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <h2 className="text-base font-semibold text-fg">Add to which branch?</h2>
            <p className="text-sm text-muted mt-2">
              <span className="font-mono text-fg">{pendingBranchPick.parallelStep}</span> has
              more than one empty branch — pick which one{" "}
              <span className="font-mono text-fg">{pendingBranchPick.to}</span> starts.
            </p>
            <div className="mt-3 space-y-1.5">
              {pendingBranchPick.emptyBranches.map((branch) => (
                <button
                  key={branch}
                  onClick={() => pickBranch(branch)}
                  className="w-full text-left rounded-lg border border-border px-3 py-2 text-sm font-mono text-fg hover:bg-elevated hover:border-violet-400/50"
                >
                  {branch}
                </button>
              ))}
            </div>
            <div className="mt-4 flex justify-end">
              <button
                onClick={() => setPendingBranchPick(null)}
                className="h-9 px-3 rounded-lg text-sm text-muted hover:text-fg hover:bg-elevated"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      )}

      {pendingDeleteStep && (
        <ConfirmDialog
          title="Delete step?"
          body={
            <>
              Delete <span className="font-mono text-fg">{pendingDeleteStep}</span> and
              remove every connection pointing to it. This cannot be undone.
            </>
          }
          confirmLabel="Delete step"
          onCancel={() => setPendingDeleteStep(null)}
          onConfirm={confirmDeleteStep}
        />
      )}

      {pendingDeleteEdge && (
        <ConfirmDialog
          title={
            pendingDeleteEdge.kind === "parallelFanout"
              ? "Remove branch?"
              : pendingDeleteEdge.kind === "onError"
                ? "Clear on-error route?"
                : "Delete connection?"
          }
          body={
            pendingDeleteEdge.kind === "parallelFanout" ? (
              <>
                Remove this branch from{" "}
                <span className="font-mono text-fg">{pendingDeleteEdge.from}</span> and every
                step in its chain. The steps themselves are kept (only the
                branch membership is removed).
              </>
            ) : pendingDeleteEdge.kind === "onError" ? (
              <>
                Clear the on-error route from{" "}
                <span className="font-mono text-fg">{pendingDeleteEdge.from}</span>. A branch
                failure will stop the run instead.
              </>
            ) : pendingDeleteEdge.kind === "branchJoin" ? (
              <>
                Clear the join step every branch reconverges on after{" "}
                <span className="font-mono text-fg">{pendingDeleteEdge.from}</span>&apos;s
                parallel step finishes.
              </>
            ) : (
              <>
                Remove the connection{" "}
                <span className="font-mono text-fg">
                  {pendingDeleteEdge.from} → {pendingDeleteEdge.to}
                </span>
                {pendingDeleteEdge.kind === "default"
                  ? " (the default path)."
                  : " (a branch condition)."}
              </>
            )
          }
          confirmLabel={
            pendingDeleteEdge.kind === "parallelFanout"
              ? "Remove branch"
              : pendingDeleteEdge.kind === "onError"
                ? "Clear route"
                : "Delete connection"
          }
          onCancel={() => setPendingDeleteEdge(null)}
          onConfirm={confirmDeleteEdge}
        />
      )}
    </div>
  );
}

function ConfirmDialog({
  title,
  body,
  confirmLabel,
  onCancel,
  onConfirm,
}: {
  title: string;
  body: React.ReactNode;
  confirmLabel: string;
  onCancel: () => void;
  onConfirm: () => void;
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

function ModeToggle({ mode, onChange }: { mode: Mode; onChange: (m: Mode) => void }) {
  return (
    <div className="inline-flex rounded-lg border border-border bg-surface p-0.5">
      <ModeBtn active={mode === "flow"} onClick={() => onChange("flow")}>
        <WorkflowIcon className="w-4 h-4" /> Flow
      </ModeBtn>
      <ModeBtn active={mode === "json"} onClick={() => onChange("json")}>
        <CodeIcon className="w-4 h-4" /> JSON
      </ModeBtn>
    </div>
  );
}

function ModeBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={cx(
        "inline-flex items-center gap-1.5 h-8 px-3 rounded-md text-sm font-medium",
        active ? "bg-brand/15 text-fg" : "text-muted hover:text-fg",
      )}
    >
      {children}
    </button>
  );
}

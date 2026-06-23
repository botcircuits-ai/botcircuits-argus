"use client";

import { useEffect, useState } from "react";
import { ExpandIcon, PlusIcon, TrashIcon } from "@/components/icons";
import { type WorkflowDoc, type WorkflowStep } from "@/lib/api";
import { cx } from "@/lib/format";

/**
 * Side panel for the flow editor, organized into two collapsible groups:
 *
 *   1. "Workflow" — description + a searchable Steps dropdown to jump to / add
 *      a step.
 *   2. "Step settings" — the fields for the selected step (name, action,
 *      default next, conditions).
 *
 * Every mutation produces a NEW `WorkflowDoc` handed back via `onChange`, so
 * the parent's doc stays the single source of truth and the JSON view syncs.
 * The UI editor only authors `agentAction` today, so the step type is implicit
 * and not shown.
 */
export function StepPanel({
  doc,
  selectedStep,
  step,
  onSelectStep,
  onChange,
  onRequestDeleteStep,
}: {
  doc: WorkflowDoc;
  selectedStep: string | null;
  step: WorkflowStep | null;
  onSelectStep: (id: string | null) => void;
  onChange: (doc: WorkflowDoc) => void;
  /** Ask the parent to confirm-then-delete a step (same dialog as the graph
   *  node right-click delete). */
  onRequestDeleteStep: (id: string) => void;
}) {
  const steps = doc.flow?.steps ?? {};
  const stepIds = Object.keys(steps);

  const [workflowOpen, setWorkflowOpen] = useState(true);
  const [settingsOpen, setSettingsOpen] = useState(true);

  // Selecting a step anywhere (graph node, its inline editors, the dropdown)
  // should reveal the Step settings group so its fields are immediately visible.
  useEffect(() => {
    if (selectedStep) setSettingsOpen(true);
  }, [selectedStep]);

  const mutate = (fn: (steps: Record<string, WorkflowStep>) => void) => {
    const nextSteps: Record<string, WorkflowStep> = JSON.parse(JSON.stringify(steps));
    fn(nextSteps);
    onChange({ ...doc, flow: { ...(doc.flow ?? {}), steps: nextSteps } });
  };

  const addStep = () => {
    let n = stepIds.length + 1;
    let id = `step_${n}`;
    while (steps[id]) id = `step_${++n}`;
    mutate((s) => {
      s[id] = { type: "agentAction", settings: { action: "" }, next: "", id };
    });
    onSelectStep(id);
    setSettingsOpen(true);
  };

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 overflow-y-auto space-y-3">
        {/* Group 1: Workflow (description + searchable steps nav) */}
        <Group title="Workflow" open={workflowOpen} onToggle={() => setWorkflowOpen((o) => !o)}>
          <Field label="Description">
            <ExpandableTextarea
              value={doc.description ?? ""}
              onChange={(v) => onChange({ ...doc, description: v })}
              rows={2}
              placeholder="When to run this workflow"
              dialogTitle="Workflow description"
            />
          </Field>

          <div className="mt-3">
            <div className="flex items-center justify-between mb-1">
              <label className="text-xs font-medium uppercase tracking-wide text-muted">
                Steps ({stepIds.length})
              </label>
              <button
                onClick={addStep}
                className="inline-flex items-center gap-1 text-xs text-brand-600 dark:text-brand-400 hover:bg-elevated rounded-md px-1.5 py-1"
              >
                <PlusIcon className="w-3.5 h-3.5" /> Add
              </button>
            </div>
            <StepSearchSelect
              stepIds={stepIds}
              selected={selectedStep}
              onSelect={(id) => {
                onSelectStep(id);
                setSettingsOpen(true);
              }}
            />
          </div>
        </Group>

        {/* Group 2: Step settings */}
        <Group
          title={selectedStep ? `Step settings · ${selectedStep}` : "Step settings"}
          open={settingsOpen}
          onToggle={() => setSettingsOpen((o) => !o)}
        >
          {!step || !selectedStep ? (
            <p className="text-sm text-muted">Select a step to edit, or add one.</p>
          ) : (
            <StepFields
              id={selectedStep}
              step={step}
              stepIds={stepIds}
              isStart={doc.flow?.start === selectedStep || step.type === "start"}
              onRename={(newId) => {
                if (!newId || newId === selectedStep || steps[newId]) return;
                mutate((s) => {
                  const old = s[selectedStep];
                  delete s[selectedStep];
                  s[newId] = { ...old, id: newId };
                  for (const v of Object.values(s)) {
                    if (v.next === selectedStep) v.next = newId;
                    if (v.conditions)
                      v.conditions = v.conditions.map((c) =>
                        c.next === selectedStep ? { ...c, next: newId } : c,
                      );
                  }
                });
                if (doc.flow?.start === selectedStep) {
                  onChange({ ...doc, flow: { ...(doc.flow ?? {}), start: newId } });
                }
                onSelectStep(newId);
              }}
              onUpdate={(patch) =>
                mutate((s) => {
                  s[selectedStep] = { ...s[selectedStep], ...patch };
                })
              }
              onDelete={() => onRequestDeleteStep(selectedStep)}
            />
          )}
        </Group>
      </div>
    </div>
  );
}

/** A searchable (plain text-contains) step picker that opens in a dialog so the
 *  full list is easy to browse even though the panel area is narrow. */
function StepSearchSelect({
  stepIds,
  selected,
  onSelect,
}: {
  stepIds: string[];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const close = () => {
    setOpen(false);
    setQuery("");
  };

  const q = query.trim().toLowerCase();
  const matches = q ? stepIds.filter((id) => id.toLowerCase().includes(q)) : stepIds;

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="w-full flex items-center justify-between rounded-lg border border-border bg-bg px-2 h-9 text-sm text-fg hover:bg-elevated"
      >
        <span className={cx("font-mono truncate", !selected && "text-muted")}>
          {selected ?? "Select a step…"}
        </span>
        <ChevronDown />
      </button>

      {open && (
        <div
          className="fixed inset-0 z-50 grid place-items-start justify-center bg-black/40 backdrop-blur-sm p-4 pt-[12vh]"
          onClick={close}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-border bg-surface shadow-xl overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between gap-2 p-3 border-b border-border">
              <span className="text-sm font-medium text-fg">
                Select a step ({stepIds.length})
              </span>
              <button
                onClick={close}
                className="text-sm text-muted hover:text-fg rounded-lg px-2 py-1 hover:bg-elevated"
              >
                Close
              </button>
            </div>
            <div className="p-3 border-b border-border">
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search steps…"
                className="w-full rounded-lg border border-border bg-bg px-3 h-9 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
              />
            </div>
            <div className="max-h-[55vh] overflow-y-auto p-2 space-y-0.5">
              {matches.length === 0 && (
                <p className="px-2 py-2 text-sm text-muted">No matching steps.</p>
              )}
              {matches.map((id) => (
                <button
                  key={id}
                  onClick={() => {
                    onSelect(id);
                    close();
                  }}
                  className={cx(
                    "w-full text-left text-sm rounded-lg px-3 py-2 font-mono truncate",
                    selected === id
                      ? "bg-brand/15 text-fg ring-1 ring-brand/30"
                      : "text-muted hover:text-fg hover:bg-elevated",
                  )}
                >
                  {id}
                </button>
              ))}
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function StepFields({
  id,
  step,
  stepIds,
  isStart,
  onRename,
  onUpdate,
  onDelete,
}: {
  id: string;
  step: WorkflowStep;
  stepIds: string[];
  isStart: boolean;
  onRename: (id: string) => void;
  onUpdate: (patch: Partial<WorkflowStep>) => void;
  onDelete: () => void;
}) {
  const [idDraft, setIdDraft] = useState(id);
  useEffect(() => setIdDraft(id), [id]);
  const conditions = step.conditions ?? [];
  const targets = stepIds.filter((s) => s !== id);

  return (
    <div className="space-y-3">
      <Field label="Step name">
        <input
          value={idDraft}
          onChange={(e) => setIdDraft(e.target.value)}
          onBlur={() => onRename(idDraft.trim())}
          disabled={isStart}
          className={cx(
            "w-full rounded-lg border border-border bg-bg px-2 h-9 text-sm font-mono text-fg focus:outline-none focus:ring-2 focus:ring-brand/40",
            isStart && "opacity-60",
          )}
        />
      </Field>

      {!isStart && (
        <Field label="Action">
          <ExpandableTextarea
            value={step.settings?.action ?? ""}
            onChange={(v) =>
              onUpdate({ settings: { ...(step.settings ?? {}), action: v } })
            }
            rows={4}
            placeholder="Natural-language instruction for the agent"
            dialogTitle={`Action · ${id}`}
          />
        </Field>
      )}

      <Field label="Default next (otherwise)">
        <select
          value={step.next ?? ""}
          onChange={(e) => onUpdate({ next: e.target.value })}
          className="w-full rounded-lg border border-border bg-bg px-2 h-9 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
        >
          <option value="">— terminal —</option>
          {targets.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
      </Field>

      <div>
        <div className="flex items-center justify-between mb-1.5">
          <label className="text-xs font-medium uppercase tracking-wide text-muted">
            Conditions
          </label>
          <button
            onClick={() =>
              onUpdate({ conditions: [...conditions, { condition: "", next: "" }] })
            }
            className="inline-flex items-center gap-1 text-xs text-brand-600 dark:text-brand-400 hover:bg-elevated rounded-md px-1.5 py-1"
          >
            <PlusIcon className="w-3.5 h-3.5" /> Add
          </button>
        </div>
        <div className="space-y-2">
          {conditions.map((c, i) => (
            <div key={i} className="rounded-lg border border-border p-2 space-y-1.5">
              <input
                value={c.condition}
                onChange={(e) => {
                  const next = [...conditions];
                  next[i] = { ...c, condition: e.target.value };
                  onUpdate({ conditions: next });
                }}
                placeholder="natural-language test"
                className="w-full rounded-md border border-border bg-bg px-2 h-8 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
              />
              <div className="flex items-center gap-1.5">
                <span className="text-xs text-muted">→</span>
                <select
                  value={c.next}
                  onChange={(e) => {
                    const next = [...conditions];
                    next[i] = { ...c, next: e.target.value };
                    onUpdate({ conditions: next });
                  }}
                  className="flex-1 rounded-md border border-border bg-bg px-2 h-8 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
                >
                  <option value="">— pick step —</option>
                  {targets.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </select>
                <button
                  onClick={() => onUpdate({ conditions: conditions.filter((_, j) => j !== i) })}
                  className="text-muted hover:text-danger rounded-md p-1"
                  aria-label="Remove condition"
                >
                  <TrashIcon className="w-4 h-4" />
                </button>
              </div>
            </div>
          ))}
          {conditions.length === 0 && (
            <p className="text-xs text-muted">
              No branches — this step always goes to its default next.
            </p>
          )}
        </div>
      </div>

      {!isStart && (
        <button
          onClick={onDelete}
          className="w-full inline-flex items-center justify-center gap-2 h-9 rounded-lg text-sm text-danger border border-danger/30 hover:bg-danger/10"
        >
          <TrashIcon className="w-4 h-4" /> Delete step
        </button>
      )}
    </div>
  );
}

/** A collapsible titled section. */
function Group({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-border bg-surface overflow-hidden">
      <button
        onClick={onToggle}
        className={cx(
          "w-full flex items-center gap-2 px-3 py-2.5 text-left hover:bg-elevated/50",
          open && "border-b border-border",
        )}
      >
        <ChevronDown className={cx("transition-transform", !open && "-rotate-90")} />
        <span className="text-sm font-medium text-fg truncate">{title}</span>
      </button>
      {open && <div className="p-3">{children}</div>}
    </div>
  );
}

function ChevronDown({ className = "" }: { className?: string }) {
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={cx("text-muted shrink-0", className)}
    >
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium uppercase tracking-wide text-muted mb-1">
        {label}
      </label>
      {children}
    </div>
  );
}

/**
 * A textarea with an expand button. The inline editor stays compact; clicking
 * the expand icon opens a large modal editor over the same value so long
 * prompts can be written/read comfortably. Both edit the same `value`.
 */
function ExpandableTextarea({
  value,
  onChange,
  rows = 3,
  placeholder,
  dialogTitle,
}: {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string;
  dialogTitle: string;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="relative">
      <textarea
        value={value}
        onChange={(e) => onChange(e.target.value)}
        rows={rows}
        placeholder={placeholder}
        className="w-full resize-none rounded-lg border border-border bg-bg px-2 py-1.5 pr-8 text-sm text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
      />
      <button
        type="button"
        onClick={() => setExpanded(true)}
        title="Expand"
        aria-label="Expand editor"
        className="absolute top-1.5 right-1.5 inline-flex items-center justify-center rounded-md p-1 text-muted hover:text-fg hover:bg-elevated"
      >
        <ExpandIcon className="w-4 h-4" />
      </button>

      {expanded && (
        <div
          className="fixed inset-0 z-50 grid place-items-center bg-black/40 backdrop-blur-sm p-4"
          onClick={() => setExpanded(false)}
        >
          <div
            className="w-full max-w-2xl rounded-2xl border border-border bg-surface p-5 shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-base font-semibold text-fg truncate">{dialogTitle}</h2>
              <button
                onClick={() => setExpanded(false)}
                className="text-sm text-muted hover:text-fg rounded-lg px-2 py-1 hover:bg-elevated"
              >
                Done
              </button>
            </div>
            <textarea
              autoFocus
              value={value}
              onChange={(e) => onChange(e.target.value)}
              placeholder={placeholder}
              className="w-full h-[60vh] resize-none rounded-lg border border-border bg-bg px-3 py-2 text-sm leading-relaxed text-fg focus:outline-none focus:ring-2 focus:ring-brand/40"
            />
          </div>
        </div>
      )}
    </div>
  );
}

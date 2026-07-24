"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import dagre from "@dagrejs/dagre";
import ReactFlow, {
  BaseEdge,
  Background,
  ConnectionMode,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlowProvider,
  applyNodeChanges,
  getSmoothStepPath,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeChange,
  type NodeProps,
  type OnConnectStartParams,
} from "reactflow";
import "reactflow/dist/style.css";
import { ForkIcon } from "@/components/icons";
import type { WorkflowDoc, WorkflowStep } from "@/lib/api";

/**
 * UI flow editor canvas for a workflow source document.
 *
 * Beyond rendering, the canvas is directly editable:
 *   - inline-edit a step's name (double-click the title) and action (the body)
 *   - inline-edit an edge's condition (the edge label is an input)
 *   - drag from one node to another to create a connection (a branch condition,
 *     or the default `next` when the source has none yet)
 *   - drag from a node and drop on empty canvas to be asked whether to create a
 *     new connected step
 *
 * All mutations flow back through the typed callbacks so the parent `doc` stays
 * the single source of truth and the JSON view stays in sync.
 */

export type EdgeKind =
  | "default"
  | "condition"
  // `parallel`-step edges (never authored via `conditions`/`next` — derived
  // from a step's `branches`/`onError` fields):
  //   parallelFanout: the parallel step -> a branch's first step
  //   branchChain:    consecutive steps within one branch's chain
  //   branchJoin:     a branch's last step -> the parallel step's own `next`
  //   onError:        the parallel step -> its `onError` step
  | "parallelFanout"
  | "branchChain"
  | "branchJoin"
  | "onError";

/** Edge kinds derived from a `parallel` step's `branches`/`onError` — never
 * backed by `conditions[]`, so their label is read-only (branch name / "join"
 * / "on error") rather than an editable condition input. */
const PARALLEL_EDGE_KINDS = new Set<EdgeKind>([
  "parallelFanout",
  "branchChain",
  "branchJoin",
  "onError",
]);

type StepNodeData = {
  label: string;
  kind?: string;
  action?: string;
  /** Name of the `doc.agents` entry this step is pinned to, if any — shown
   * as a small badge so a different-model step stands out on the canvas. */
  agent?: string;
  /** Set when this step is a member of some `parallel` step's branch chain —
   * `{parallelStep, branch}` — so the node can carry a small badge naming
   * which fan-out/branch it belongs to. */
  branchOf?: { parallelStep: string; branch: string };
  selected: boolean;
  isStart: boolean;
  edgeHighlighted?: boolean;
  onSelect: (id: string) => void;
  onRename: (oldId: string, newId: string) => void;
  onAction: (id: string, action: string) => void;
};

function StepNode({ data, id }: NodeProps<StepNodeData>) {
  const stepId = id.replace(/^step:/, "");
  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState(data.label);
  const [actionDraft, setActionDraft] = useState(data.action ?? "");

  useEffect(() => setNameDraft(data.label), [data.label]);
  useEffect(() => setActionDraft(data.action ?? ""), [data.action]);

  const commitName = () => {
    setEditingName(false);
    const next = nameDraft.trim();
    if (next && next !== data.label) data.onRename(data.label, next);
    else setNameDraft(data.label);
  };

  const isParallel = data.kind === "parallel";

  return (
    <div
      onClick={() => data.onSelect(data.label)}
      className={[
        "rounded-xl px-3 py-2 w-[210px] shadow-sm transition-shadow border bg-surface",
        data.edgeHighlighted
          ? "border-2"
          : data.selected
            ? "border-2 border-brand ring-2 ring-brand/40"
            : isParallel
              ? "border-violet-400/60 dark:border-violet-500/50"
              : "border-border",
      ].join(" ")}
      style={
        data.edgeHighlighted
          ? { borderColor: "rgb(96, 165, 250)", boxShadow: "0 0 0 3px rgba(96, 165, 250, 0.3)" }
          : undefined
      }
    >
      {/* With connectionMode="loose" a single handle acts as both source and
          target, so a connection can be drawn from either node's handle. */}
      <Handle type="target" position={Position.Top} className="!bg-muted !w-3 !h-3" />
      <div className="flex items-center gap-1.5">
        {isParallel && <ForkIcon className="w-3 h-3 text-violet-500 dark:text-violet-400 shrink-0" />}
        <span
          className={[
            "text-[11px] uppercase tracking-wide",
            isParallel ? "text-violet-600 dark:text-violet-400 font-medium" : "text-muted",
          ].join(" ")}
        >
          {data.isStart ? "start" : data.kind || "step"}
        </span>
        {data.branchOf && (
          <span
            title={`Branch "${data.branchOf.branch}" of parallel step "${data.branchOf.parallelStep}"`}
            className="inline-flex items-center rounded-full bg-violet-500/15 px-1.5 py-0.5 text-[10px] font-medium text-violet-600 dark:text-violet-400 truncate max-w-[90px]"
          >
            {data.branchOf.branch}
          </span>
        )}
        {data.agent && (
          <span
            title={`Runs on the "${data.agent}" agent`}
            className="ml-auto inline-flex items-center rounded-full bg-brand/15 px-1.5 py-0.5 text-[10px] font-medium text-brand-600 dark:text-brand-400 truncate max-w-[100px]"
          >
            {data.agent}
          </span>
        )}
      </div>

      {editingName ? (
        <input
          autoFocus
          value={nameDraft}
          onChange={(e) => setNameDraft(e.target.value)}
          onFocus={() => data.onSelect(data.label)}
          onBlur={commitName}
          onKeyDown={(e) => {
            if (e.key === "Enter") commitName();
            if (e.key === "Escape") {
              setNameDraft(data.label);
              setEditingName(false);
            }
          }}
          className="nodrag w-full rounded border border-brand/50 bg-bg px-1 py-0.5 text-sm font-medium font-mono text-fg focus:outline-none"
        />
      ) : (
        <div
          className="font-medium text-sm truncate text-fg cursor-text"
          title="Double-click to rename"
          onDoubleClick={(e) => {
            e.stopPropagation();
            if (!data.isStart) setEditingName(true);
          }}
        >
          {data.label}
        </div>
      )}

      {!data.isStart && !isParallel && (
        <textarea
          value={actionDraft}
          onChange={(e) => setActionDraft(e.target.value)}
          onFocus={() => data.onSelect(data.label)}
          onBlur={() => {
            if (actionDraft !== (data.action ?? "")) data.onAction(stepId, actionDraft);
          }}
          onClick={(e) => e.stopPropagation()}
          rows={2}
          placeholder="action prompt…"
          className="nodrag nowheel mt-1 w-full resize-none rounded border border-border bg-bg px-1.5 py-1 text-[11px] leading-snug text-fg placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-brand/40"
        />
      )}
      {isParallel && (
        <p className="mt-1 text-[11px] leading-snug text-muted">
          Branches edited in the side panel — drag from here to add one.
        </p>
      )}

      <Handle type="source" position={Position.Bottom} className="!bg-brand-500 !w-3 !h-3" />
    </div>
  );
}

const nodeTypes = { step: StepNode };

/** Identifies an edge for deletion: its source step, kind, branch index, and
 *  target step (the target is what's cleared/removed). */
export type EdgeRef = { from: string; kind: EdgeKind; condIndex: number; to: string };

export type WorkflowGraphHandlers = {
  onSelectStep: (step: string | null) => void;
  onRenameStep: (oldId: string, newId: string) => void;
  onUpdateAction: (id: string, action: string) => void;
  onUpdateEdgeCondition: (
    from: string,
    kind: EdgeKind,
    condIndex: number,
    condition: string,
  ) => void;
  /** Connect two existing steps. */
  onConnect: (from: string, to: string) => void;
  /** Drag ended on empty canvas: caller asks the user, then maybe creates. */
  onConnectToEmpty: (from: string) => void;
  /** Request deletion of a step (caller confirms, then mutates). */
  onRequestDeleteStep: (id: string) => void;
  /** Request deletion of an edge (caller confirms, then mutates). */
  onRequestDeleteEdge: (ref: EdgeRef) => void;
};

export function WorkflowGraph(props: {
  doc: WorkflowDoc;
  selectedStep: string | null;
} & WorkflowGraphHandlers) {
  return (
    <ReactFlowProvider>
      <WorkflowGraphInner {...props} />
    </ReactFlowProvider>
  );
}

// Blue highlight for a selected edge + its connected nodes (matches TraceGraph).
const EDGE_HL = "rgb(96, 165, 250)";

type CtxMenu =
  | { kind: "node"; id: string; x: number; y: number; isStart: boolean }
  | { kind: "edge"; ref: EdgeRef; x: number; y: number }
  | null;

function WorkflowGraphInner({
  doc,
  selectedStep,
  onSelectStep,
  onRenameStep,
  onUpdateAction,
  onUpdateEdgeCondition,
  onConnect,
  onConnectToEmpty,
  onRequestDeleteStep,
  onRequestDeleteEdge,
}: {
  doc: WorkflowDoc;
  selectedStep: string | null;
} & WorkflowGraphHandlers) {
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [menu, setMenu] = useState<CtxMenu>(null);

  const { nodes: baseNodes, edges } = useMemo(
    () =>
      buildGraph(doc, selectedStep, {
        onSelect: onSelectStep,
        onRename: onRenameStep,
        onAction: onUpdateAction,
        onUpdateEdgeCondition,
      }),
    [doc, selectedStep, onSelectStep, onRenameStep, onUpdateAction, onUpdateEdgeCondition],
  );

  const [currentNodes, setCurrentNodes] = useState<Node[]>(baseNodes);
  useEffect(() => setCurrentNodes(baseNodes), [baseNodes]);

  const handleNodesChange = (changes: NodeChange[]) =>
    setCurrentNodes((nds) => applyNodeChanges(changes, nds));

  // Apply blue highlight + flow animation to the selected edge and mark its
  // source/target nodes so they highlight too. Kept separate from the layout
  // memo so selecting an edge doesn't recompute the Dagre layout.
  const { displayNodes, displayEdges } = useMemo(() => {
    if (!selectedEdgeId) return { displayNodes: currentNodes, displayEdges: edges };
    const sel = edges.find((e) => e.id === selectedEdgeId);
    if (!sel) return { displayNodes: currentNodes, displayEdges: edges };
    const hl = new Set([sel.source, sel.target]);
    const displayNodes = currentNodes.map((n) =>
      hl.has(n.id) ? { ...n, data: { ...n.data, edgeHighlighted: true } } : n,
    );
    const displayEdges = edges.map((e) =>
      e.id === selectedEdgeId
        ? {
            ...e,
            animated: true,
            style: { ...e.style, stroke: EDGE_HL, strokeWidth: 2.5, opacity: 1 },
            markerEnd: {
              type: MarkerType.ArrowClosed,
              ...(typeof e.markerEnd === "object" ? e.markerEnd : {}),
              color: EDGE_HL,
            },
          }
        : e,
    );
    return { displayNodes, displayEdges };
  }, [currentNodes, edges, selectedEdgeId]);

  const closeMenu = useCallback(() => setMenu(null), []);

  // Track where a connection drag started so a drop on empty canvas can create
  // a new step connected from that source.
  const connectFrom = useRef<string | null>(null);
  const onConnectStart = useCallback(
    (_: unknown, params: OnConnectStartParams) => {
      connectFrom.current = params.nodeId ? params.nodeId.replace(/^step:/, "") : null;
    },
    [],
  );

  // Connect by dropping anywhere over the target node's surface — not just on
  // its top handle. We resolve the node under the pointer at drop time and, if
  // found, connect to it; otherwise (empty canvas) raise the create dialog.
  const onConnectEnd = useCallback(
    (event: MouseEvent | TouchEvent) => {
      const from = connectFrom.current;
      connectFrom.current = null;
      if (!from) return;

      const point =
        "changedTouches" in event && event.changedTouches.length
          ? event.changedTouches[0]
          : (event as MouseEvent);
      const el = document.elementFromPoint(point.clientX, point.clientY) as
        | HTMLElement
        | null;
      const nodeEl = el?.closest(".react-flow__node") as HTMLElement | null;
      const toId = nodeEl?.getAttribute("data-id")?.replace(/^step:/, "") ?? null;

      if (toId && toId !== from) {
        onConnect(from, toId);
      } else if (!toId) {
        onConnectToEmpty(from);
      }
    },
    [onConnect, onConnectToEmpty],
  );

  return (
    <div className="relative h-full w-full rounded-2xl border border-border bg-bg overflow-hidden">
      <ReactFlow
        nodes={displayNodes}
        edges={displayEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        connectionMode={ConnectionMode.Loose}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
        onPaneClick={() => {
          onSelectStep(null);
          setSelectedEdgeId(null);
          closeMenu();
        }}
        onNodeClick={(_, node) => {
          setSelectedEdgeId(null);
          closeMenu();
          onSelectStep((node.data as StepNodeData).label);
        }}
        onEdgeClick={(_, edge) => {
          closeMenu();
          setSelectedEdgeId((prev) => (prev === edge.id ? null : edge.id));
        }}
        onNodeContextMenu={(e, node) => {
          e.preventDefault();
          const d = node.data as StepNodeData;
          setMenu({ kind: "node", id: d.label, x: e.clientX, y: e.clientY, isStart: d.isStart });
        }}
        onEdgeContextMenu={(e, edge) => {
          e.preventDefault();
          const d = (edge as Edge<ConditionEdgeData>).data;
          if (!d) return;
          setMenu({
            kind: "edge",
            ref: { from: d.from, kind: d.kind, condIndex: d.condIndex, to: d.to },
            x: e.clientX,
            y: e.clientY,
          });
        }}
        onNodesChange={handleNodesChange}
        onConnectStart={onConnectStart}
        onConnectEnd={onConnectEnd}
        nodesDraggable
        nodesConnectable
      >
        <Background gap={18} size={1} className="!text-border" color="currentColor" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          className="!bg-surface !border !border-border rounded-lg"
          maskColor="rgb(var(--bg) / 0.6)"
          nodeColor={(n) =>
            (n.data as any)?.edgeHighlighted
              ? EDGE_HL
              : (n.data as any)?.selected
                ? "rgb(130 176 21)"
                : "rgb(var(--border))"
          }
          nodeStrokeWidth={2}
        />
      </ReactFlow>

      {menu && (
        <ContextMenu
          menu={menu}
          onClose={closeMenu}
          onDeleteNode={(id) => {
            closeMenu();
            onRequestDeleteStep(id);
          }}
          onDeleteEdge={(ref) => {
            closeMenu();
            setSelectedEdgeId(null);
            onRequestDeleteEdge(ref);
          }}
        />
      )}
    </div>
  );
}

/** Right-click delete menu for a node or edge. Closes on outside click/Escape. */
function ContextMenu({
  menu,
  onClose,
  onDeleteNode,
  onDeleteEdge,
}: {
  menu: NonNullable<CtxMenu>;
  onClose: () => void;
  onDeleteNode: (id: string) => void;
  onDeleteEdge: (ref: EdgeRef) => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onDown = () => onClose();
    window.addEventListener("keydown", onKey);
    window.addEventListener("mousedown", onDown);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("mousedown", onDown);
    };
  }, [onClose]);

  const isStartNode = menu.kind === "node" && menu.isStart;
  // A mid-branch chain link can't be deleted standalone — splicing the chain
  // isn't supported for v1; delete one of the two steps instead (which
  // reconnects the chain automatically — see `confirmDeleteStep`).
  const isUndeletableEdge = menu.kind === "edge" && menu.ref.kind === "branchChain";

  return (
    <div
      className="fixed z-50 min-w-[160px] rounded-lg border border-border bg-surface py-1 shadow-xl"
      style={{ left: menu.x, top: menu.y }}
      onMouseDown={(e) => e.stopPropagation()}
    >
      {menu.kind === "node" ? (
        <button
          disabled={isStartNode}
          onClick={() => onDeleteNode(menu.id)}
          className="w-full text-left px-3 py-1.5 text-sm text-danger hover:bg-danger/10 disabled:opacity-40 disabled:hover:bg-transparent"
          title={isStartNode ? "The start step cannot be deleted" : undefined}
        >
          Delete step
        </button>
      ) : (
        <button
          disabled={isUndeletableEdge}
          onClick={() => onDeleteEdge(menu.ref)}
          className="w-full text-left px-3 py-1.5 text-sm text-danger hover:bg-danger/10 disabled:opacity-40 disabled:hover:bg-transparent"
          title={
            isUndeletableEdge
              ? "Delete one of the two steps instead — a branch chain link can't be removed on its own"
              : undefined
          }
        >
          {menu.ref.kind === "parallelFanout"
            ? "Remove branch"
            : menu.ref.kind === "onError"
              ? "Clear on-error route"
              : "Delete connection"}
        </button>
      )}
    </div>
  );
}

function buildGraph(
  doc: WorkflowDoc,
  selectedStep: string | null,
  cb: {
    onSelect: (id: string) => void;
    onRename: (oldId: string, newId: string) => void;
    onAction: (id: string, action: string) => void;
    onUpdateEdgeCondition: (
      from: string,
      kind: EdgeKind,
      condIndex: number,
      condition: string,
    ) => void;
  },
): { nodes: Node[]; edges: Edge[] } {
  const steps = doc.flow?.steps ?? {};
  const start = doc.flow?.start ?? "start";
  const ids = Object.keys(steps);

  // Every step id that's a member of some `parallel` step's branch chain,
  // mapped to which parallel step + branch name it belongs to — used both to
  // badge the node and to skip it in the plain `next`/`conditions` edge pass
  // below (branch membership draws its OWN edges, not the generic ones).
  const branchMembership = new Map<string, { parallelStep: string; branch: string }>();
  for (const id of ids) {
    const s = steps[id];
    if (s?.type !== "parallel") continue;
    for (const [branch, chain] of Object.entries(s.branches ?? {})) {
      for (const stepId of chain) {
        branchMembership.set(stepId, { parallelStep: id, branch });
      }
    }
  }

  const nodes: Node[] = ids.map((id) => {
    const s: WorkflowStep = steps[id] ?? {};
    return {
      id: `step:${id}`,
      type: "step",
      position: { x: 0, y: 0 },
      data: {
        label: id,
        kind: s.type,
        action: s.settings?.action,
        agent: s.agent,
        branchOf: branchMembership.get(id),
        selected: selectedStep === id,
        isStart: id === start || s.type === "start",
        onSelect: cb.onSelect,
        onRename: cb.onRename,
        onAction: cb.onAction,
      } satisfies StepNodeData,
    };
  });

  const edges: Edge[] = [];
  for (const id of ids) {
    const s = steps[id] ?? {};

    if (s.type === "parallel") {
      for (const [branch, chain] of Object.entries(s.branches ?? {})) {
        const validChain = chain.filter((sid) => steps[sid]);
        if (!validChain.length) continue;
        // Fan-out: the parallel step -> this branch's first step. Labeled
        // with the branch name (read-only — not a `conditions[]` entry).
        edges.push(
          makeEdge(id, validChain[0], branch, "parallelFanout", -1, cb.onUpdateEdgeCondition),
        );
        // Chain edges between consecutive steps within the branch.
        for (let i = 0; i < validChain.length - 1; i++) {
          edges.push(
            makeEdge(validChain[i], validChain[i + 1], "", "branchChain", i, cb.onUpdateEdgeCondition),
          );
        }
        // Join: the branch's last step -> the parallel step's own `next`,
        // once every branch has finished.
        const last = validChain[validChain.length - 1];
        if (s.next && steps[s.next]) {
          edges.push(
            makeEdge(last, s.next, "join", "branchJoin", -1, cb.onUpdateEdgeCondition),
          );
        }
      }
      if (s.onError && steps[s.onError]) {
        edges.push(
          makeEdge(id, s.onError, "on error", "onError", -1, cb.onUpdateEdgeCondition),
        );
      }
      // A parallel step's `next` only takes effect via the per-branch join
      // edges above — it never gets a direct edge of its own.
      continue;
    }

    // Steps that are themselves inside a branch chain get their `next`/
    // `conditions` from the branch-chain/join edges above, not here (a
    // branch step is validated build-time to carry neither anyway).
    if (branchMembership.has(id)) continue;

    (s.conditions ?? []).forEach((c, idx) => {
      if (!c.next || !steps[c.next]) return;
      edges.push(
        makeEdge(id, c.next, c.condition, "condition", idx, cb.onUpdateEdgeCondition),
      );
    });
    if (s.next && steps[s.next]) {
      // The default ("otherwise") edge is also editable: typing a condition
      // into it converts it from the default path into a regular branch (the
      // update handler moves `next` into `conditions` and clears `next`).
      edges.push(
        makeEdge(id, s.next, "", "default", -1, cb.onUpdateEdgeCondition),
      );
    }
  }

  layoutWithDagre(nodes, edges);
  return { nodes, edges };
}

type ConditionEdgeData = {
  from: string;
  to: string;
  kind: EdgeKind;
  condIndex: number;
  condition: string;
  onUpdate: (from: string, kind: EdgeKind, condIndex: number, condition: string) => void;
};

/** Violet accent for every `parallel`-derived edge kind (fan-out, branch
 * chain, join), matching the violet node accent in `StepNode`. `onError`
 * gets the danger color and a dashed stroke — it's the exceptional path. */
const PARALLEL_EDGE_COLOR = "rgb(167, 139, 250)"; // violet-400
const ON_ERROR_EDGE_COLOR = "rgb(248, 113, 113)"; // red-400

function makeEdge(
  from: string,
  to: string,
  condition: string,
  kind: EdgeKind,
  condIndex: number,
  onUpdate: (from: string, kind: EdgeKind, condIndex: number, condition: string) => void,
): Edge<ConditionEdgeData> {
  const isDefault = kind === "default";
  const isOnError = kind === "onError";
  const isBranch = PARALLEL_EDGE_KINDS.has(kind) && !isOnError;
  const color = isOnError
    ? ON_ERROR_EDGE_COLOR
    : isBranch
      ? PARALLEL_EDGE_COLOR
      : "rgb(var(--muted))";
  return {
    id: `e:${from}->${to}:${kind}:${condIndex}`,
    source: `step:${from}`,
    target: `step:${to}`,
    type: "condition",
    markerEnd: { type: MarkerType.ArrowClosed, color },
    style: {
      stroke: color,
      strokeWidth: isBranch || isOnError ? 1.8 : 1.6,
      strokeDasharray: isDefault || isOnError ? "5 4" : undefined,
      opacity: 0.9,
    },
    data: { from, to, kind, condIndex, condition, onUpdate },
  };
}

/** Custom edge: smoothstep path with an interactive HTML label at the midpoint.
 *  Uses EdgeLabelRenderer so the label is real, clickable HTML (the `label`
 *  prop only renders static, non-interactive text). */
function ConditionEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  markerEnd,
  style,
  data,
}: EdgeProps<ConditionEdgeData>) {
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 12,
  });

  const isParallelEdge = data && PARALLEL_EDGE_KINDS.has(data.kind);

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {data && (
        <EdgeLabelRenderer>
          <div
            className="nodrag nopan"
            style={{
              position: "absolute",
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
              pointerEvents: "all",
            }}
          >
            {isParallelEdge ? (
              <ParallelEdgeLabel kind={data.kind} text={data.condition} />
            ) : (
              <EdgeConditionInput
                value={data.condition}
                isDefault={data.kind === "default"}
                onCommit={(v) => data.onUpdate(data.from, data.kind, data.condIndex, v)}
              />
            )}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

/** Static (non-editable) label for a `parallel`-derived edge — a branch
 * name, "join", or "on error". These aren't backed by `conditions[]`, so
 * unlike `EdgeConditionInput` there's nothing to click into and edit here;
 * the branch itself is renamed/removed from the Step settings panel. */
function ParallelEdgeLabel({ kind, text }: { kind: EdgeKind; text: string }) {
  if (kind === "branchChain" || !text) return null;
  const isOnError = kind === "onError";
  return (
    <span
      className={[
        "rounded px-1.5 py-0.5 text-[10px] font-medium border",
        isOnError
          ? "border-danger/40 bg-danger/10 text-danger"
          : "border-violet-400/40 bg-violet-500/10 text-violet-600 dark:text-violet-400",
      ].join(" ")}
    >
      {text}
    </span>
  );
}

const edgeTypes = { condition: ConditionEdge };

/**
 * Inline condition label for a branch edge.
 *
 * When the condition is empty it shows a compact "Add condition" pill; clicking
 * it (or an existing condition's text) opens an inline input to edit the
 * condition prompt. Commits on blur / Enter, cancels on Escape.
 */
function EdgeConditionInput({
  value,
  isDefault = false,
  onCommit,
}: {
  value: string;
  isDefault?: boolean;
  onCommit: (v: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);

  if (!editing) {
    const empty = !value.trim();
    // The default edge with no condition reads as "(default)"; typing
    // a condition into it turns it into a regular branch.
    const emptyLabel = isDefault ? "default" : "+ Add condition";
    return (
      <button
        onClick={() => setEditing(true)}
        className={[
          "nodrag rounded px-1.5 py-0.5 text-[10px] font-medium border",
          empty
            ? isDefault
              ? "border-dashed border-border text-muted bg-elevated hover:bg-surface"
              : "border-dashed border-brand/50 text-brand-600 dark:text-brand-400 bg-brand/10 hover:bg-brand/20"
            : "border-border bg-elevated text-fg hover:bg-surface",
        ].join(" ")}
        title={empty ? "Click to set a condition" : "Edit condition"}
      >
        {empty ? emptyLabel : value}
      </button>
    );
  }

  const commit = () => {
    setEditing(false);
    if (draft !== value) onCommit(draft);
  };

  return (
    <input
      autoFocus
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setDraft(value);
          setEditing(false);
        }
      }}
      placeholder="condition…"
      className="nodrag nowheel rounded border border-brand/50 bg-elevated px-1.5 py-0.5 text-[10px] text-fg placeholder:text-muted focus:outline-none focus:ring-1 focus:ring-brand/40"
      style={{ width: Math.max(80, Math.min(180, (draft.length || 10) * 6)) }}
    />
  );
}

const STEP_W = 210;
const STEP_H = 110;

function layoutWithDagre(nodes: Node[], edges: Edge[]): void {
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: "TB",
    nodesep: 90,
    ranksep: 130,
    edgesep: 30,
    marginx: 30,
    marginy: 30,
    // Default "network-simplex" packs every node as close to its parent's
    // rank as the DAG allows — with a `parallel` step's uneven-depth
    // branches plus an `onError` target, that wedges the error step and a
    // short branch into the SAME rank, so the short branch's edge routes
    // past/through the error node and their labels collide. "longest-path"
    // pushes `onError` down to the join's rank instead — see the identical
    // note in `TraceGraph.tsx`'s `layoutWithDagre`.
    ranker: "longest-path",
  });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: STEP_W, height: STEP_H });
  for (const e of edges) {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }
  dagre.layout(g);
  for (const n of nodes) {
    const dn = g.node(n.id);
    if (!dn) continue;
    n.position = { x: dn.x - STEP_W / 2, y: dn.y - STEP_H / 2 };
  }
}

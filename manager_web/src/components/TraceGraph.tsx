"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import dagre from "@dagrejs/dagre";
import ReactFlow, {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  applyNodeChanges,
  type Edge,
  type Node,
  type NodeChange,
  type NodeProps,
} from "reactflow";
import "reactflow/dist/style.css";
import type { ActionUsage, RunUsage, SessionDoc, TraceEvent } from "@/lib/api";
import { fmtDuration, fmtTokens } from "@/lib/format";

/**
 * Workflow trace graph.
 *
 * The FULL branch topology comes from `workflow.graph` (every step and every
 * conditional edge, including paths this run did not take). The trace then
 * OVERLAYS reality: which steps were entered, per-step durations, and which
 * edge the engine actually followed at each branch. Slot nodes from the memory
 * graph attach to the step that produced them (the "memory flow").
 */

type StepNodeData = {
  label: string;
  kind?: string;
  durationMs: number | null;
  /** Real tokens this step billed, when the runtime reported usage. */
  usage?: ActionUsage | null;
  visited: boolean;
  selected: boolean;
  edgeHighlighted?: boolean;
};
type SlotNodeData = { slots: Record<string, any>; edgeHighlighted?: boolean };

function StepNode({ data }: NodeProps<StepNodeData>) {
  return (
    <div
      className={[
        // Visited steps sit on the elevated surface; unvisited ("not run")
        // steps use a dashed muted border + faint fill so they stay clearly
        // readable against the canvas instead of fading into it.
        // FIXED width so the rendered size matches the size given to Dagre —
        // otherwise long labels grow the node past Dagre's estimate and nodes
        // collide.
        "rounded-xl px-3 py-2 w-[200px] shadow-sm transition-shadow",
        data.edgeHighlighted
          ? "border-2 bg-surface"
          : data.selected
            ? "border-2 border-brand ring-2 ring-brand/40 bg-surface"
            : data.visited
              ? "border border-border bg-surface"
              : "border border-dashed border-muted/60 bg-elevated/40",
      ].join(" ")}
      style={
        data.edgeHighlighted
          ? { borderColor: "rgb(96, 165, 250)", boxShadow: "0 0 0 3px rgba(96, 165, 250, 0.3)" }
          : undefined
      }
    >
      <Handle type="target" position={Position.Top} className="!bg-muted" />
      <div className="flex items-center gap-1.5">
        <span
          className={`text-[11px] uppercase tracking-wide ${
            data.visited ? "text-muted" : "text-muted/80"
          }`}
        >
          {data.kind === "start" ? "start" : "step"}
        </span>
        {data.visited ? (
          <span className="h-1.5 w-1.5 rounded-full bg-brand-500" title="entered" />
        ) : (
          <span className="text-[10px] text-muted">· not run</span>
        )}
      </div>
      <div
        className={`font-medium text-sm truncate ${
          data.visited ? "text-fg" : "text-muted"
        }`}
      >
        {data.label}
      </div>
      {(data.durationMs != null || data.usage) && (
        <div className="flex items-center gap-2 mt-0.5">
          {data.durationMs != null && (
            <span className="text-[11px] text-muted">{fmtDuration(data.durationMs)}</span>
          )}
          {data.usage && (
            <span
              className="text-[10px] font-medium text-brand bg-brand/10 rounded px-1 py-px tabular-nums"
              title={
                `${data.usage.total_tokens} tokens` +
                ` (in ${data.usage.input_tokens} / out ${data.usage.output_tokens}` +
                (data.usage.cache_read_tokens
                  ? ` / cache ${data.usage.cache_read_tokens}`
                  : "") +
                `)`
              }
            >
              {fmtTokens(data.usage.total_tokens)} tok
            </span>
          )}
        </div>
      )}
      <Handle type="source" position={Position.Bottom} className="!bg-muted" />
      <Handle id="slot" type="source" position={Position.Right} className="!bg-brand-500" />
    </div>
  );
}

function SlotNode({ data }: NodeProps<SlotNodeData>) {
  const entries = Object.entries(data.slots || {});
  return (
    <div
      className={[
        "rounded-xl border p-2 flex flex-col transition-shadow shadow-sm max-h-[160px] overflow-y-auto cursor-help",
        data.edgeHighlighted
          ? "border-blue-400 bg-blue-50/90 dark:bg-blue-950/50"
          : "border-brand/40 bg-brand/10 dark:bg-brand-950/20",
      ].join(" ")}
      style={{
        width: "180px",
        borderColor: data.edgeHighlighted ? "rgb(96, 165, 250)" : undefined,
        boxShadow: data.edgeHighlighted ? "0 0 0 3px rgba(96, 165, 250, 0.3)" : undefined,
      }}
    >
      <Handle type="target" position={Position.Left} className="!bg-brand-500" />
      <div className="text-[9px] uppercase tracking-wider text-brand font-semibold mb-1">
        Memory Snapshot
      </div>
      {entries.length === 0 ? (
        <div className="text-[10px] text-muted italic font-mono">— empty —</div>
      ) : (
        <div className="space-y-1 font-mono text-[9px] leading-tight">
          {entries.map(([k, v]) => (
            <div key={k} className="truncate" title={`${k}: ${valuePreview(v)}`}>
              <span className="text-muted mr-1">{k}:</span>
              <span className="text-fg">{valuePreview(v)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function valuePreview(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v.length > 24 ? v.slice(0, 24) + "…" : v;
  try {
    return JSON.stringify(v);
  } catch {
    return String(v);
  }
}

const nodeTypes = { step: StepNode, slot: SlotNode };

export function TraceGraph({
  doc,
  selectedStep,
  onSelectStep,
}: {
  doc: SessionDoc;
  selectedStep: string | null;
  onSelectStep: (step: string | null) => void;
}) {
  const [onlyVisited, setOnlyVisited] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const slotCount = (doc.memory?.nodes ?? []).filter((n) => n.kind === "slot").length;
  const runUsage = useMemo(() => runUsageOf(doc), [doc]);

  const { nodes: baseNodes, edges, hasGraph } = useMemo(
    // "Only path taken" collapses to the steps that ran; "View memory" overlays
    // the slot (memory) nodes each step produced.
    () => buildGraph(doc, selectedStep, { showMemory, onlyVisited }),
    [doc, selectedStep, showMemory, onlyVisited],
  );

  // --- Controlled node state for drag support ------------------------------
  // `baseNodes` holds the Dagre-computed positions; `currentNodes` holds the
  // live positions that update when the user drags. Synced back whenever the
  // base graph changes (toggle, new data, etc.).
  const [currentNodes, setCurrentNodes] = useState<Node[]>(baseNodes);
  const [hasDragged, setHasDragged] = useState(false);

  useEffect(() => {
    setCurrentNodes(baseNodes);
    setHasDragged(false);
  }, [baseNodes]);

  const handleNodesChange = useCallback((changes: NodeChange[]) => {
    setCurrentNodes((nds) => applyNodeChanges(changes, nds));
    if (changes.some((c) => c.type === "position" && c.dragging)) {
      setHasDragged(true);
    }
  }, []);

  const handleResetLayout = useCallback(() => {
    setCurrentNodes(baseNodes);
    setHasDragged(false);
  }, [baseNodes]);

  // Derive display nodes/edges with edge-selection highlighting applied.
  // Kept separate from the layout memo so clicking an edge doesn't recompute
  // the Dagre layout.
  const EDGE_HL = "rgb(96, 165, 250)";
  const { displayNodes, displayEdges } = useMemo(() => {
    if (!selectedEdgeId) return { displayNodes: currentNodes, displayEdges: edges };
    const selEdge = edges.find((e) => e.id === selectedEdgeId);
    if (!selEdge) return { displayNodes: currentNodes, displayEdges: edges };

    const hlIds = new Set([selEdge.source, selEdge.target]);

    const displayNodes = currentNodes.map((n) =>
      hlIds.has(n.id)
        ? { ...n, data: { ...n.data, edgeHighlighted: true } }
        : n,
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
            labelStyle: { ...(e.labelStyle ?? {}), fill: EDGE_HL, fontWeight: 600 },
          }
        : e,
    );
    return { displayNodes, displayEdges };
  }, [currentNodes, edges, selectedEdgeId]);

  return (
    <div className="h-[600px] rounded-2xl border border-border bg-bg overflow-hidden relative">
      {/* controls */}
      <div className="absolute top-2 left-2 z-10 flex flex-wrap gap-1.5">
        {!hasGraph && (
          <span className="text-[11px] text-muted bg-surface/90 rounded px-2 py-1 border border-border">
            Older session — visited steps only.
          </span>
        )}
        {hasGraph && (
          <Toggle on={onlyVisited} onClick={() => setOnlyVisited((v) => !v)}>
            Only path taken
          </Toggle>
        )}
        {slotCount > 0 && (
          <Toggle on={showMemory} onClick={() => setShowMemory((v) => !v)}>
            View memory{showMemory ? "" : ` (${slotCount})`}
          </Toggle>
        )}
        {hasDragged && (
          <button
            onClick={handleResetLayout}
            className="text-[11px] rounded-md px-2 py-1 border bg-surface/90 border-border text-muted hover:text-fg flex items-center gap-1"
          >
            <span className="text-xs">&#x21bb;</span> Reset layout
          </button>
        )}
      </div>

      {/* run token-usage summary */}
      {runUsage && runUsage.total_tokens > 0 && (
        <div
          className="absolute top-2 right-2 z-10 text-[11px] rounded-md px-2 py-1 border bg-surface/90 border-brand/40 text-fg flex items-center gap-1.5 tabular-nums"
          title={
            `Total run tokens: ${runUsage.total_tokens}\n` +
            `input ${runUsage.input_tokens} · output ${runUsage.output_tokens}` +
            (runUsage.cache_read_tokens
              ? ` · cache read ${runUsage.cache_read_tokens}`
              : "") +
            `\nLLM calls: ${runUsage.calls}`
          }
        >
          <span className="text-brand font-semibold">
            {fmtTokens(runUsage.total_tokens)}
          </span>
          <span className="text-muted">tokens</span>
          <span className="text-muted/60">·</span>
          <span className="text-muted">{runUsage.calls} call{runUsage.calls === 1 ? "" : "s"}</span>
        </div>
      )}

      <ReactFlow
        nodes={displayNodes}
        edges={displayEdges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => {
          setSelectedEdgeId(null);
          if (node.type === "step") onSelectStep(node.data.label);
        }}
        onEdgeClick={(_, edge) =>
          setSelectedEdgeId((prev) => (prev === edge.id ? null : edge.id))
        }
        onPaneClick={() => setSelectedEdgeId(null)}
        onNodesChange={handleNodesChange}
        nodesDraggable
        nodesConnectable={false}
      >
        <Background gap={18} size={1} className="!text-border" color="currentColor" />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          className="!bg-surface !border !border-border rounded-lg"
          maskColor="rgb(var(--bg) / 0.6)"
          nodeColor={(n) =>
            n.type === "slot"
              ? "rgb(166 221 31)"
              : (n.data as any)?.visited
                ? "rgb(130 176 21)"
                : "rgb(var(--border))"
          }
          nodeStrokeWidth={2}
        />
      </ReactFlow>
    </div>
  );
}

function Toggle({
  on,
  onClick,
  children,
}: {
  on: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`text-[11px] rounded-md px-2 py-1 border ${
        on
          ? "bg-brand/15 border-brand/40 text-fg"
          : "bg-surface/90 border-border text-muted hover:text-fg"
      }`}
    >
      {children}
    </button>
  );
}

/** Sum two ActionUsage records (per-step accumulation across action calls). */
function mergeUsage(a: ActionUsage | undefined, b: ActionUsage): ActionUsage {
  if (!a) return { ...b };
  return {
    step: a.step || b.step,
    runtime: a.runtime || b.runtime,
    input_tokens: a.input_tokens + b.input_tokens,
    output_tokens: a.output_tokens + b.output_tokens,
    cache_read_tokens: a.cache_read_tokens + b.cache_read_tokens,
    cache_write_tokens: a.cache_write_tokens + b.cache_write_tokens,
    calls: a.calls + b.calls,
    total_tokens: a.total_tokens + b.total_tokens,
  };
}

/** The run's token total. Prefers the authoritative `usage` trace event the
 * engine emits; falls back to summing per-step action usage for older traces. */
function runUsageOf(doc: SessionDoc): RunUsage | null {
  for (let i = doc.trace.length - 1; i >= 0; i--) {
    const ev = doc.trace[i];
    if (ev.type === "usage" && ev.data && typeof ev.data === "object") {
      const d = ev.data as any;
      if (typeof d.total_tokens === "number") return d as RunUsage;
    }
  }
  // Fallback: aggregate from action_after usage payloads.
  let total: RunUsage | null = null;
  for (const ev of doc.trace) {
    if (ev.type !== "action_after") continue;
    const u = (ev.data as any)?.output?.usage as ActionUsage | undefined;
    if (!u) continue;
    total = total ?? {
      total_tokens: 0, input_tokens: 0, output_tokens: 0,
      cache_read_tokens: 0, cache_write_tokens: 0, calls: 0, steps: [],
    };
    total.total_tokens += u.total_tokens;
    total.input_tokens += u.input_tokens;
    total.output_tokens += u.output_tokens;
    total.cache_read_tokens += u.cache_read_tokens;
    total.cache_write_tokens += u.cache_write_tokens;
    total.calls += u.calls;
    total.steps.push(u);
  }
  return total;
}

function buildGraph(
  doc: SessionDoc,
  selectedStep: string | null,
  opts: { showMemory: boolean; onlyVisited: boolean } = {
    showMemory: true,
    onlyVisited: false,
  },
): { nodes: Node[]; edges: Edge[]; hasGraph: boolean } {
  const graph = doc.workflow?.graph;
  const graphSteps = graph?.steps ?? {};
  const allStepIds = Object.keys(graphSteps);
  const hasGraph = allStepIds.length > 0;

  // --- trace-derived overlays ---------------------------------------------
  // A `step_enter` covers a whole SEGMENT, which may bundle several actual
  // steps (e.g. a `question` and the action that precedes it). The event's
  // `step` is just the segment's primary step; `data.steps` lists every step
  // the segment ran. Mark them ALL visited — otherwise a bundled step is never
  // "entered", its node is dropped under "Only path taken", and the edges into
  // and out of it dangle (the lookup_order→not_found-only bug).
  const visited = new Set<string>();
  for (const ev of doc.trace as TraceEvent[]) {
    if (ev.type !== "step_enter") continue;
    // The segment head (e.g. a transparent `start`) plus every bundled step
    // were all entered. The head matters for connectivity: without it the
    // `start → …` edge dangles under "Only path taken".
    const head = (ev.data as any)?.segment;
    if (head) visited.add(head);
    const segSteps = (ev.data as any)?.steps;
    if (Array.isArray(segSteps) && segSteps.length > 0) {
      for (const s of segSteps) if (s) visited.add(s);
    } else if (ev.step) {
      visited.add(ev.step);
    }
  }

  // "Only path taken" collapses the graph to the steps that actually ran.
  const stepIds = opts.onlyVisited
    ? allStepIds.filter((id) => visited.has(id))
    : allStepIds;
  // The engine's branch events tell us which edge was actually taken.
  const takenNextByStep = new Map<string, string | null>();
  for (const ev of doc.trace) {
    if (ev.type === "branch" && ev.step) {
      takenNextByStep.set(ev.step, ((ev.data as any)?.chosen_next ?? null) as string | null);
    }
  }
  // Per-step duration AND per-step token usage: attribute action_after data to
  // the most recent step_enter (action events don't always carry a step id).
  // Usage rides on `data.output.usage` (see runtime.trace_hooks); summed per
  // step so a step driven by several action calls shows its combined cost.
  const durByStep = new Map<string, number>();
  const usageByStep = new Map<string, ActionUsage>();
  {
    let current: string | null = null;
    for (const ev of doc.trace) {
      if (ev.type === "step_enter" && ev.step) current = ev.step;
      if (ev.type !== "action_after") continue;
      const k = ev.step ?? current;
      if (!k) continue;
      if (ev.duration_ms != null) {
        durByStep.set(k, (durByStep.get(k) ?? 0) + ev.duration_ms);
      }
      const u = (ev.data as any)?.output?.usage as ActionUsage | undefined;
      if (u) usageByStep.set(k, mergeUsage(usageByStep.get(k), u));
    }
  }

  if (!hasGraph) {
    // Fallback for old sessions: linear visited-steps chain (previous behavior).
    return fallbackGraph(doc, selectedStep, durByStep);
  }

  // --- step nodes (positions assigned by Dagre below) ---------------------
  const nodes: Node[] = stepIds.map((id) => {
    const s = graphSteps[id];
    return {
      id: `step:${id}`,
      type: "step",
      position: { x: 0, y: 0 },
      data: {
        label: id,
        kind: s?.type,
        durationMs: durByStep.get(id) ?? null,
        usage: usageByStep.get(id) ?? null,
        visited: visited.has(id),
        selected: selectedStep === id,
      },
    };
  });

  // --- edges: every conditional + default, taken ones highlighted ---------
  const edges: Edge[] = [];
  for (const id of stepIds) {
    const s = graphSteps[id];
    const taken = takenNextByStep.get(id);
    const defaultNext = s?.next ?? null;

    const addEdge = (to: string, label: string | undefined, isDefault: boolean) => {
      if (!graphSteps[to]) return;
      const isTaken =
        (taken !== undefined && taken === to) ||
        // No branch event (non-branch step): the static default edge between
        // two visited steps counts as taken.
        (taken === undefined && isDefault && visited.has(id) && visited.has(to));
      edges.push({
        id: `e:${id}->${to}:${label ?? "next"}`,
        source: `step:${id}`,
        target: `step:${to}`,
        type: "smoothstep",
        pathOptions: { borderRadius: 12 } as any,
        label,
        labelShowBg: true,
        animated: isTaken,
        markerEnd: {
          type: MarkerType.ArrowClosed,
          color: isTaken ? "rgb(166 221 31)" : "rgb(var(--muted))",
        },
        style: {
          // Inactive edges use the muted (zinc-500/400) color, not the faint
          // border color, so they stay readable against the canvas.
          stroke: isTaken ? "rgb(166 221 31)" : "rgb(var(--muted))",
          strokeWidth: isTaken ? 2.25 : 1.5,
          strokeDasharray: isDefault && (s?.choices?.length ?? 0) > 0 ? "5 4" : undefined,
          opacity: isTaken ? 1 : 0.85,
        },
        labelStyle: { fill: "rgb(var(--fg))", fontSize: 10, fontWeight: 500 },
        labelBgStyle: {
          fill: "rgb(var(--elevated))",
          fillOpacity: 1,
          stroke: "rgb(var(--border))",
        },
        labelBgPadding: [5, 3],
        labelBgBorderRadius: 4,
      });
    };

    (s?.choices ?? []).forEach((c) => {
      if (c.next) addEdge(c.next, condLabel(c.condition), false);
    });
    if (defaultNext) {
      addEdge(defaultNext, (s?.choices?.length ?? 0) > 0 ? "otherwise" : undefined, true);
    }
  }

  // --- memory slot nodes (added BEFORE layout so Dagre spaces them) --------
  // Reconstruct memory state at each step from trace events and add a single circular-themed snapshot card.
  if (opts.showMemory) {
    const slotsAtStep = new Map<string, Record<string, any>>();
    {
      let currentSlots: Record<string, any> = {};
      if (doc.workflow?.initial_slots) {
        currentSlots = { ...doc.workflow.initial_slots };
      }
      for (const ev of doc.trace as TraceEvent[]) {
        if (ev.slots) {
          currentSlots = { ...currentSlots, ...ev.slots };
        }
        if (ev.step) {
          slotsAtStep.set(ev.step, { ...currentSlots });
        }
      }
    }

    for (const id of stepIds) {
      if (!visited.has(id)) continue;
      const slots = slotsAtStep.get(id);
      if (!slots || Object.keys(slots).length === 0) continue;

      const slotNodeId = `slot:${id}:memory`;
      nodes.push({
        id: slotNodeId,
        type: "slot",
        position: { x: 0, y: 0 },
        data: { slots },
      });
      edges.push({
        id: `m:${id}->${slotNodeId}`,
        source: `step:${id}`,
        sourceHandle: "slot",
        target: slotNodeId,
        type: "smoothstep",
        style: { stroke: "rgb(166 221 31)", strokeWidth: 1.25, strokeDasharray: "4 3" },
      });
    }
  }

  // --- layered layout via Dagre (handles ordering + crossing minimization) -
  layoutWithDagre(nodes, edges);

  return { nodes, edges, hasGraph: true };
}

// Must match the FIXED rendered node sizes (w-[200px] / w-[180px]) so Dagre's
// collision math is accurate. Heights are generous upper bounds (tallest
// variant: tag + label + duration + padding).
const STEP_W = 200;
const STEP_H = 80;
const SLOT_W = 180;
const SLOT_H = 100;

/** Position all nodes with Dagre (top-to-bottom layered DAG). Steps and slot
 *  nodes are sized by type; both step→step and step→slot edges participate so
 *  slots get their own non-overlapping positions. Mutates node positions. */
function layoutWithDagre(nodes: Node[], edges: Edge[]): void {
  const g = new dagre.graphlib.Graph();
  g.setGraph({
    rankdir: "TB",
    nodesep: 90, // horizontal gap between siblings — wide so labels/edges breathe
    ranksep: 130, // vertical gap between ranks — room for edge labels between rows
    edgesep: 30, // gap between parallel edges
    marginx: 30,
    marginy: 30,
  });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of nodes) {
    const isSlot = n.type === "slot";
    g.setNode(n.id, {
      width: isSlot ? SLOT_W : STEP_W,
      height: isSlot ? SLOT_H : STEP_H,
    });
  }
  // Every edge (step→step and step→slot) participates so nothing overlaps.
  for (const e of edges) {
    if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
  }

  dagre.layout(g);

  for (const n of nodes) {
    const dn = g.node(n.id);
    if (!dn) continue;
    const w = n.type === "slot" ? SLOT_W : STEP_W;
    const h = n.type === "slot" ? SLOT_H : STEP_H;
    // Dagre returns node centers; ReactFlow wants top-left.
    n.position = { x: dn.x - w / 2, y: dn.y - h / 2 };
  }
}

function condLabel(cond: string): string {
  const c = (cond || "").trim();
  if (!c) return "if";
  return c.length > 28 ? c.slice(0, 28) + "…" : c;
}

/** Legacy path for sessions captured before workflow.graph existed. */
function fallbackGraph(
  doc: SessionDoc,
  selectedStep: string | null,
  durByStep: Map<string, number>,
): { nodes: Node[]; edges: Edge[]; hasGraph: boolean } {
  const order: string[] = [];
  const seen = new Set<string>();
  for (const ev of doc.trace) {
    if (ev.type === "step_enter" && ev.step && !seen.has(ev.step)) {
      seen.add(ev.step);
      order.push(ev.step);
    }
  }
  const nodes: Node[] = order.map((step) => ({
    id: `step:${step}`,
    type: "step",
    position: { x: 0, y: 0 },
    data: {
      label: step,
      durationMs: durByStep.get(step) ?? null,
      visited: true,
      selected: selectedStep === step,
    },
  }));
  const edges: Edge[] = [];
  for (let i = 0; i < order.length - 1; i++) {
    edges.push({
      id: `e:${order[i]}->${order[i + 1]}`,
      source: `step:${order[i]}`,
      target: `step:${order[i + 1]}`,
      type: "smoothstep",
      markerEnd: { type: MarkerType.ArrowClosed, color: "rgb(166 221 31)" },
      style: { stroke: "rgb(166 221 31)", strokeWidth: 2 },
    });
  }
  layoutWithDagre(nodes, edges);
  return { nodes, edges, hasGraph: false };
}

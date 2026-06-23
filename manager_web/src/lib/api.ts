/**
 * Typed client for the BotCircuits Manager backend.
 *
 * Base URL comes from NEXT_PUBLIC_API_BASE (default localhost:8700). The bearer
 * token is held in localStorage by the auth layer and passed in per call.
 */

export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE ?? "http://127.0.0.1:8700";

export const GITHUB_URL =
  process.env.NEXT_PUBLIC_GITHUB_URL ??
  "https://github.com/botcircuits-ai/botcircuits-agent";

export type SessionSummary = {
  session_id: string;
  workflow: string | null;
  runtime: string | null;
  start: string | null;
  end: string | null;
  status: "running" | "paused" | "done" | "failure" | string;
  event_count: number;
  updated_at: number;
};

export type TraceEvent = {
  seq: number;
  ts: string;
  type: string;
  step: string | null;
  duration_ms: number | null;
  slots: Record<string, unknown>;
  data: Record<string, unknown>;
};

/** Real token usage one action step billed (carried on `action_after`
 * events under `data.output.usage`, and aggregated on the final `usage`
 * event). Present only when the runtime reports usage. */
export type ActionUsage = {
  step?: string;
  runtime?: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  calls: number;
  total_tokens: number;
};

/** Run-level token usage: the session total plus a per-action-step list.
 * Emitted as the `usage` trace event's `data`. */
export type RunUsage = {
  total_tokens: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  calls: number;
  steps: ActionUsage[];
};

export type MemoryNode = {
  id: string;
  kind: string;
  label?: string;
  value?: unknown;
  [k: string]: unknown;
};

export type MemoryEdge = {
  from: string;
  to: string;
  kind?: string;
  [k: string]: unknown;
};

export type FlowChoice = { condition: string; next: string };
export type FlowStep = {
  type?: string;
  action?: string;
  next?: string | null;
  choices?: FlowChoice[];
};
export type FlowGraph = {
  start?: string | null;
  steps?: Record<string, FlowStep>;
};

export type SessionDoc = {
  session_id: string;
  agent: { runtime?: string };
  workflow: {
    name?: string;
    start?: string;
    end?: string | null;
    initial_slots?: Record<string, unknown>;
    graph?: FlowGraph;
  };
  trace: TraceEvent[];
  memory: { nodes: MemoryNode[]; edges: MemoryEdge[] };
};

// --- Workflow authoring types ----------------------------------------------

/** The shared step-type constant. Today only `agentAction` is supported in the
 * UI editor; more step types (question, systemAction, …) come later. */
export const SUPPORTED_STEP_TYPES = ["agentAction"] as const;
export const STEP_TYPE_AGENT_ACTION = "agentAction";

export type WorkflowCondition = { condition: string; next: string };

/** A raw authored step (source format — natural-language conditions). */
export type WorkflowStep = {
  type?: string;
  id?: string;
  next?: string | null;
  settings?: { action?: string; [k: string]: unknown };
  conditions?: WorkflowCondition[];
  [k: string]: unknown;
};

export type WorkflowFlow = {
  start?: string;
  steps?: Record<string, WorkflowStep>;
  [k: string]: unknown;
};

/** A raw, human-authored workflow source document (the `.botcircuits/workflows`
 * file shape — steps nested under `flow`). */
export type WorkflowDoc = {
  name?: string;
  description?: string;
  flow?: WorkflowFlow;
  [k: string]: unknown;
};

export type WorkflowSummary = {
  name: string;
  description: string;
  step_count: number;
  built: boolean;
  updated_at: number;
};

export type BuildResult = {
  ok: boolean;
  returncode: number;
  stdout: string;
  stderr: string;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  opts: { token?: string | null; method?: string; body?: unknown } = {},
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (opts.token) headers["Authorization"] = `Bearer ${opts.token}`;

  let res: Response;
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: opts.method ?? "GET",
      headers,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      cache: "no-store",
    });
  } catch {
    throw new ApiError(0, `Cannot reach the manager backend at ${API_BASE}.`);
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const j = await res.json();
      detail = (j && (j.detail || j.message)) || detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  health: () =>
    request<{ status: string; auth_configured: boolean }>("/api/health"),

  login: (username: string, password: string) =>
    request<{ token: string; expires_in: number }>("/api/auth/login", {
      method: "POST",
      body: { username, password },
    }),

  listSessions: (token: string) =>
    request<SessionSummary[]>("/api/sessions", { token }),

  getSession: (token: string, id: string) =>
    request<SessionDoc>(`/api/sessions/${encodeURIComponent(id)}`, { token }),

  // --- Workflows ---
  listWorkflows: (token: string) =>
    request<WorkflowSummary[]>("/api/workflows", { token }),

  getWorkflow: (token: string, name: string) =>
    request<WorkflowDoc>(`/api/workflows/${encodeURIComponent(name)}`, { token }),

  saveWorkflow: (token: string, name: string, workflow: WorkflowDoc) =>
    request<WorkflowDoc>(`/api/workflows/${encodeURIComponent(name)}`, {
      token,
      method: "PUT",
      body: { workflow },
    }),

  deleteWorkflow: (token: string, name: string) =>
    request<{ deleted: boolean; name: string }>(
      `/api/workflows/${encodeURIComponent(name)}`,
      { token, method: "DELETE" },
    ),

  buildWorkflow: (token: string, name: string) =>
    request<BuildResult>(`/api/workflows/${encodeURIComponent(name)}/build`, {
      token,
      method: "POST",
    }),

  /** URL for the authoring SSE stream (token passed as query — EventSource
   * cannot set an Authorization header). */
  authorStreamUrl: (token: string, name: string, instruction: string) => {
    const q = new URLSearchParams({ name, instruction, token });
    return `${API_BASE}/api/workflows/author/stream?${q.toString()}`;
  },

  /** URL for the run SSE stream. Pass `reply` to resume a paused run with the
   * user's answer. Token passed as query — EventSource can't set headers. */
  runStreamUrl: (token: string, name: string, reply?: string) => {
    const q = new URLSearchParams({ name, token });
    if (reply) q.set("reply", reply);
    return `${API_BASE}/api/workflows/run/stream?${q.toString()}`;
  },
};

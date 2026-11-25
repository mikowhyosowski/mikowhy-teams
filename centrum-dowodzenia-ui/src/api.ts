/**
 * Klient API silnika. Baza: EXPO_PUBLIC_API_URL (web/dev) albo względne /api
 * (web build serwowany razem z backendem / przez ten sam origin).
 */
import type { AgentPipeline, ProcessRun, SponsorView, WorkerHealth } from "./types";

const API_BASE = (process.env.EXPO_PUBLIC_API_URL ?? "").replace(/\/$/, "");

async function j<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: { "Content-Type": "application/json" } });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return (await res.json()) as T;
}

async function post<T>(path: string, body: unknown = {}): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return (await res.json()) as T;
}

export const api = {
  /** Aktywne (żywe) runy procesów — Puls Fabryki. */
  liveRuns: () => j<{ runs: ProcessRun[]; count: number }>("/api/pipeline/runs?status=RUNNING&limit=50"),
  /** Runy z filtrami (task / agent / status). */
  runs: (q: { task_id?: string; agent_role?: string; status?: string; limit?: number }) => {
    const p = new URLSearchParams();
    if (q.task_id) p.set("task_id", q.task_id);
    if (q.agent_role) p.set("agent_role", q.agent_role);
    if (q.status) p.set("status", q.status);
    if (q.limit) p.set("limit", String(q.limit));
    return j<{ runs: ProcessRun[]; count: number }>(`/api/pipeline/runs?${p}`);
  },
  /** Pełny log runu (ANSI-safe plain text). */
  runLog: async (runId: string, tail = 0): Promise<string> => {
    const res = await fetch(`${API_BASE}/api/pipeline/runs/${runId}/log${tail ? `?tail=${tail}` : ""}`);
    if (!res.ok) throw new Error(`log -> HTTP ${res.status}`);
    return res.text();
  },
  /** Zabicie zawieszonego procesu przez Sponsora (cała grupa pgid). */
  killRun: (runId: string) => post<{ ok: boolean; status: string }>(`/api/pipeline/runs/${runId}/kill`),
  /** Agent Execution Pipeline — pełna historia zadań i procesów roli. */
  agentPipeline: (role: string) =>
    j<AgentPipeline>(`/api/agents/${encodeURIComponent(role)}/pipeline`),
  /** Stan workerów (stall detection) — do kafli „Puls Fabryki”. */
  workerHealth: () => j<{ workers: WorkerHealth[]; stalled: string[] }>("/api/workers/health"),
  /** Widok Sponsora — decyzje AWAITING_OWNER. */
  sponsor: () => j<SponsorView>("/api/sponsor"),
  /** Akcje na zadaniu (approve / decline / force_retry). */
  taskAction: (taskId: string, action: string, body: unknown = {}) =>
    post<{ ok: boolean }>(`/api/task/${encodeURIComponent(taskId)}/${action}`, body),
  /** Akcja z flow configa (approve / decline / retry / merge) — walidowana po stronie serwera. */
  flowAction: (taskId: string, action: string, body: unknown = {}) =>
    post<{ ok: boolean; to?: string }>("/api/flow/action", { task_id: taskId, action, ...(body as object) }),
};

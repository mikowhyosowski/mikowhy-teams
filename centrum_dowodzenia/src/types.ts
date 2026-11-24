/** Typy zgodne z modelem danych backendu (process_runs + tasks + sponsor view). */

export type RunStatus =
  | "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "TIMEOUT" | "KILLED";

export interface ProcessRun {
  run_id: string;
  task_id: string | null;
  agent_role: string | null;
  substep: string | null;
  command: string;
  cwd: string | null;
  pid: number | null;
  pgid: number | null;
  status: RunStatus;
  started_at: string | null;   // ISO-8601 UTC
  finished_at: string | null;
  duration_ms: number | null;
  exit_code: number | null;
  timeout_s: number | null;
  log_path: string | null;
  stdout_tail: string;
  stderr_tail: string;
}

export interface Task {
  id: string;
  title: string;
  role: string;
  status: string;
  substep: string | null;
  priority: string;
  claimed_by: string | null;
  updated_at: number;
  runs?: ProcessRun[];
}

export interface AgentPipeline {
  role: string;
  tasks: Task[];
  runs_unassigned: ProcessRun[];
  runs_total: number;
}

export interface WorkerHealth {
  task_id: string;
  substep: string | null;
  claimed_by: string | null;
  idle_s: number;
  stall: boolean;
  llm_job_running: boolean;
}

export interface SponsorView {
  awaiting_owner?: Array<{ id: string; title: string; role: string; substep: string | null }>;
  [key: string]: unknown;
}

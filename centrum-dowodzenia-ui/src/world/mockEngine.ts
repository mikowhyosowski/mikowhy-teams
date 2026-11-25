/**
 * mockEngine — symulator silnika mikowhy-teams zgodny z `flow.md`.
 *
 * Odwzorowuje realne mechanizmy:
 *  - statusy: READY → RUNNING → IN_REVIEW → READY_TO_MERGE → MERGED (+CHANGES_REQUESTED,
 *    COOLING, AWAITING_OWNER, BLOCKED);
 *  - substeps: 1/5_PLAN → 2/5_IMPL → 3/5_TEST → 4/5_QG → 5/5_COMMIT,
 *  - QG (ruff/mypy/pytest) jako brama: zielona = dalej, czerwona = stop;
 *  - kolejka LLM z flockiem (.agents/llm.lock): jedno żądanie naraz (serial) + BATCH;
 *  - COOLING = sleep ~65 min po wyjątku/rc=2 (tu skalowane do sekund);
 *  - priorytety P0–P3 wpływają na rozmiar/kolor zadania i kolejność claimu.
 *
 * Dane są deterministycznie generowane w czasie; interfejs jest "pull + subscribe".
 */

export type Status =
  | "READY" | "RUNNING" | "IN_REVIEW" | "CHANGES_REQUESTED"
  | "READY_TO_MERGE" | "MERGED" | "AWAITING_OWNER" | "BLOCKED" | "COOLING";

export type Substep =
  | "1/5_PLAN" | "2/5_IMPL" | "3/5_TEST" | "4/5_QG" | "5/5_COMMIT" | "done";

export type Priority = "P0" | "P1" | "P2" | "P3";

export interface TaskDTO {
  id: string;
  role: string;
  priority: Priority;
  status: Status;
  substep: Substep;
  progress: number;      // 0..1 wg substep
  age_s: number;         // ile czasu w bieżącym statusie
  worktree: boolean;     // czy ma izolowany boks (RUNNING)
  scope_paths: string[];
}

export interface LlmJob {
  id: string;
  task_id: string;
  model: string;
  state: "QUEUED" | "RUNNING" | "BATCH" | "OK" | "FAILED";
  age_s: number;
  rc?: number;           // rc=3 = race keyring / turn_limit
}

export interface EngineSnapshot {
  ts: number;
  tasks: TaskDTO[];
  llm: { queue: LlmJob[]; running: LlmJob | null; lock: boolean; batch: number };
  events: EngineEvent[];
}

export interface EngineEvent {
  ts: number;
  kind: "spawn" | "qg_ok" | "qg_fail" | "llm" | "llm_spark" | "merge" | "owner" | "cooling" | "review";
  task_id: string;
  text: string;
}

const ROLES = ["backend-dev", "frontend-dev", "devops", "qa-engineer", "data-engineer", "ux-designer"];
const SCHEMAS: Record<Priority, { size: number; ms: number }> = {
  P0: { size: 0.34, ms: 5200 },
  P1: { size: 0.28, ms: 7600 },
  P2: { size: 0.24, ms: 10000 },
  P3: { size: 0.20, ms: 14000 },
};
const SUBSTEP_ORDER: Substep[] = ["1/5_PLAN", "2/5_IMPL", "3/5_TEST", "4/5_QG", "5/5_COMMIT", "done"];
const SUBSTEP_PROGRESS: Record<Substep, number> = {
  "1/5_PLAN": 0.15, "2/5_IMPL": 0.35, "3/5_TEST": 0.6, "4/5_QG": 0.8, "5/5_COMMIT": 0.95, done: 1,
};

let seq = 100;

function pick<T>(arr: T[]): T {
  return arr[Math.floor(Math.random() * arr.length)];
}

function randPriority(): Priority {
  const r = Math.random();
  if (r < 0.15) return "P0";
  if (r < 0.45) return "P1";
  if (r < 0.85) return "P2";
  return "P3";
}

export class MockEngine {
  private tasks = new Map<string, TaskDTO>();
  private llmQueue: LlmJob[] = [];
  private llmRunning: LlmJob | null = null;
  private eventsQueue: EngineEvent[] = [];
  private lastEventTs = 0;

  constructor() {
    for (let i = 0; i < 6; i++) this.spawnTask(randPriority());
    // jeden task od razu w AWAITING_OWNER, żeby wieża decyzji nie była pusta
    this.forceStatus(Array.from(this.tasks.keys())[0], "AWAITING_OWNER");
    this.llmQueue.push({ id: "job-1", task_id: Array.from(this.tasks.keys())[1], model: "vibe-rs/small", state: "QUEUED", age_s: 0 });
  }

  /** + Nowe zadanie (marszczona kolejka). */
  spawnTask(priority?: Priority): TaskDTO {
    const id = `TASK-${++seq}`;
    const p = priority ?? randPriority();
    const t: TaskDTO = {
      id, role: pick(ROLES), priority: p, status: "READY", substep: "1/5_PLAN",
      progress: 0, age_s: 0, worktree: false,
      scope_paths: [`src/${pick(["api", "ui", "engine", "ops"])}/`, `tests/`],
    };
    this.tasks.set(id, t);
    return t;
  }

  private forceStatus(id: string, status: Status) {
    const t = this.tasks.get(id);
    if (!t) return;
    t.status = status;
    t.age_s = 0;
    t.worktree = status === "RUNNING";
    if (status === "READY" || status === "COOLING") t.substep = "1/5_PLAN";
  }

  private emit(kind: EngineEvent["kind"], task_id: string, text: string) {
    this.eventsQueue.push({ ts: Date.now(), kind, task_id, text });
  }

  /** Krok symulacji wywoływany co ~600ms z hooka. */
  tick(dtMs: number) {
    const dt = dtMs / 1000;
    for (const t of this.tasks.values()) {
      t.age_s += dt;
      if (t.status === "RUNNING") {
        const speed = 1 / (SCHEMAS[t.priority].ms / 1000);
        t.progress += dt * speed;
        if (t.progress >= 1) {
          t.progress = 0;
          const i = SUBSTEP_ORDER.indexOf(t.substep);
          const next = SUBSTEP_ORDER[Math.min(i + 1, SUBSTEP_ORDER.length - 1)];
          t.substep = next;
          // żądanie LLM przy krokach wymagających modelu
          if ((next === "2/5_IMPL" || next === "3/5_TEST") && Math.random() < 0.75) this.enqueueLlm(t);
          if (next === "4/5_QG") this.emit("qg_ok", t.id, "QG: ruff/mypy/pytest — brama otwarta");
          if (next === "done") {
            t.status = "IN_REVIEW";
            t.substep = "done";
            this.emit("review", t.id, "oddane do niezależnego Code Review");
          }
        }
        t.progress = Math.min(1, t.progress);
      } else if (t.status === "COOLING") {
        if (t.age_s > 12) { // sleep minął → wraca do kolejki
          this.forceStatus(t.id, "READY");
          this.emit("cooling", t.id, "sleep minął → wraca na taśmę (READY)");
        }
      }
    }

    // Redukcja zbyt długiej listy
    if (this.tasks.size > 26) {
      const merged = Array.from(this.tasks.values()).filter((t) => t.status === "MERGED").slice(0, 3);
      merged.forEach((t) => this.tasks.delete(t.id));
    }

    // LLM: serial — jedno żądanie na raz (flock)
    this.llmQueue = this.llmQueue.filter((j) => j.state === "QUEUED");
    for (const j of this.llmQueue) j.age_s += dt;
    if (!this.llmRunning && this.llmQueue.length) {
      const job = this.llmQueue.shift()!;
      job.state = "RUNNING";
      this.llmRunning = job;
      this.emit("llm", job.task_id, `prompt → ${job.model} (flock .agents/llm.lock)`);
    }
    if (this.llmRunning) {
      this.llmRunning.age_s += dt;
      const done = this.llmRunning.age_s > (2 + Math.random() * 2);
      if (done) {
        const rc3 = Math.random() < 0.12;
        const job = this.llmRunning;
        this.llmRunning = null;
        if (rc3) {
          this.emit("llm_spark", job.task_id, "vibe rc=3 (keyring race) — iskra w reaktorze");
          // rc=3 → wyjątek workera → COOLING
          this.forceStatus(job.task_id, "COOLING");
          this.emit("cooling", job.task_id, "wyjątek workera → COOLING (~65 min w realu)");
        } else {
          this.emit("llm", job.task_id, "odpowiedź z rdzenia → agent");
        }
      }
    }
    // czasem BATCH: 2 joby tego samego modelu razem
    if (this.llmQueue.length >= 2 && Math.random() < 0.05) {
      const [a, b] = this.llmQueue;
      if (a.model === b.model) {
        this.llmQueue = this.llmQueue.filter((j) => j !== a && j !== b);
        this.emit("llm", a.task_id + b.task_id, "BATCH: 2 prompty w jednym wywołaniu CLI");
        this.enqueueLlm(this.tasks.get(a.task_id) || { id: a.task_id } as TaskDTO);
      }
    }

    // Automatyka statusów (jak tick orchestratora)
    for (const t of this.tasks.values()) {
      if (t.status === "READY" && Math.random() < 0.02) {
        t.status = "RUNNING";
        t.substep = "1/5_PLAN";
        t.worktree = true;
        t.age_s = 0;
        this.emit("spawn", t.id, "claim → własny worktree (RUNNING)");
      }
      if (t.status === "IN_REVIEW" && t.age_s > 6 && Math.random() < 0.06) {
        if (Math.random() < 0.65) {
          t.status = "READY_TO_MERGE";
          t.age_s = 0;
          this.emit("qg_ok", t.id, "CR: approve → tor wdrożeniowy");
        } else {
          t.status = "CHANGES_REQUESTED";
          t.age_s = 0;
          this.emit("qg_fail", t.id, "CR: reject → pętla poprawek");
        }
      }
      if (t.status === "CHANGES_REQUESTED" && t.age_s > 4) {
        t.status = "RUNNING"; t.substep = "2/5_IMPL"; t.worktree = true; t.age_s = 0;
        this.emit("spawn", t.id, "poprawki po CR → RUNNING");
      }
      if (t.status === "READY_TO_MERGE" && t.age_s > 5) {
        t.status = "MERGED"; t.worktree = false; t.age_s = 0;
        this.emit("merge", t.id, "rebase + regresja + merge + push → MERGED");
      }
    }

    if (this.lastEventTs < Date.now() - 4000) {
      this.lastEventTs = Date.now();
      if (this.eventsQueue.length > 40) this.eventsQueue = this.eventsQueue.slice(-40);
    }
  }

  private enqueueLlm(t: TaskDTO) {
    this.llmQueue.push({
      id: `job-${Math.random().toString(36).slice(2, 8)}`,
      task_id: t.id,
      model: Math.random() < 0.6 ? "vibe-rs/small" : "vibe-rs/large",
      state: "QUEUED",
      age_s: 0,
    });
  }

  /** Demo controls — ręczne wywołanie zdarzenia. */
  demo(action: "spawn_p0" | "llm" | "qg_fail" | "escalate" | "merge") {
    const running = Array.from(this.tasks.values()).filter((t) => t.status === "RUNNING");
    const anyId = (list: TaskDTO[]) => (list.length ? list[0].id : this.spawnTask().id);
    switch (action) {
      case "spawn_p0": {
        const t = this.spawnTask("P0");
        this.emit("spawn", t.id, "nowe zadanie P0 w kolejce");
        break;
      }
      case "llm": {
        const t = this.tasks.get(anyId(running))!;
        this.enqueueLlm(t);
        this.emit("llm", t.id, "wysłano prompt do rdzenia");
        break;
      }
      case "qg_fail": {
        const t = this.tasks.get(anyId(running.length ? running : Array.from(this.tasks.values()).filter(t => t.status === "READY")))!;
        t.status = "COOLING"; t.age_s = 0; t.worktree = false;
        this.emit("qg_fail", t.id, "QG na czerwono → tor chłodzenia");
        break;
      }
      case "escalate": {
        const t = this.tasks.get(anyId(running.length ? running : Array.from(this.tasks.values())))!;
        t.status = "AWAITING_OWNER"; t.age_s = 0; t.worktree = false;
        this.emit("owner", t.id, "retry>=3 / circuit → wymaga decyzji Sponsora");
        break;
      }
      case "merge": {
        const rtm = Array.from(this.tasks.values()).filter((t) => t.status === "READY_TO_MERGE");
        const t = this.tasks.get(anyId(rtm.length ? rtm : Array.from(this.tasks.values()).filter(t => t.status === "IN_REVIEW")))!;
        t.status = "MERGED"; t.age_s = 0;
        this.emit("merge", t.id, "merge do master (efektowny rozbłysk)");
        break;
      }
    }
  }

  drain(): EngineEvent[] {
    const e = this.eventsQueue;
    this.eventsQueue = [];
    return e;
  }

  snapshot(): EngineSnapshot {
    return {
      ts: Date.now(),
      tasks: Array.from(this.tasks.values()),
      llm: {
        queue: [...this.llmQueue],
        running: this.llmRunning,
        lock: !!this.llmRunning,
        batch: this.llmQueue.length >= 2 ? 1 : 0,
      },
      events: this.drain(),
    };
  }
}

export { SUBSTEP_PROGRESS, SCHEMAS };

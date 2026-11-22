"""Warstwa stanu operacyjnego: SQLite w trybie WAL (.agents/state.db).

Zmiana #8: zastępuje pipeline.json + fcntl. Daje natywne transakcje ACID,
atomowe przejęcia zadań (UPDATE ... WHERE status='READY' RETURNING *) i zero
deadlocków blokad plikowych. Czytelny snapshot pipeline.json jest eksportowany
do repozytorium tylko na koniec kamienia milowego (lub po każdym mergu).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / ".agents" / "state.db"
SNAPSHOT_PATH = ROOT / ".agents" / "pipeline.json"

# --- Statusy zadań w DAG ---------------------------------------------------
READY = "READY"                    # gotowe do przejęcia
RUNNING = "RUNNING"                # przejęte przez workera
IN_REVIEW = "IN_REVIEW"            # czeka na niezależny Code Review
READY_TO_MERGE = "READY_TO_MERGE"  # CR zatwierdził
MERGED = "MERGED"                  # zmergowane do gałęzi milestonu
BLOCKED = "BLOCKED"
FAILED = "FAILED"
ESCALATED = "ESCALATED"            # człowiek (Product Sponsor) musi podjąć decyzję
AWAITING_APPROVAL = "AWAITING_APPROVAL"  # zadania migracyjne czekają na ręczną akceptację Product Sponsora

VALID_TRANSITIONS: dict[str, set[str]] = {
    READY: {RUNNING, BLOCKED, ESCALATED},
    RUNNING: {IN_REVIEW, READY, FAILED, BLOCKED, ESCALATED},
    IN_REVIEW: {READY_TO_MERGE, READY, ESCALATED},
    READY_TO_MERGE: {MERGED, READY, ESCALATED},
    MERGED: set(),
    FAILED: {READY},
    BLOCKED: {READY, ESCALATED, AWAITING_APPROVAL},
    ESCALATED: {READY},
    AWAITING_APPROVAL: {READY, BLOCKED, ESCALATED},  # tylko człowiek (zmiana statusu na READY) odblokowuje
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS milestones (
  id          TEXT PRIMARY KEY,
  title       TEXT NOT NULL,
  goal        TEXT NOT NULL,
  branch      TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN | QG3 | CLOSED
  created_at  REAL NOT NULL,
  merged_at   REAL
);
CREATE TABLE IF NOT EXISTS tasks (
  id                   TEXT PRIMARY KEY,
  milestone            TEXT NOT NULL REFERENCES milestones(id),
  phase                INTEGER NOT NULL DEFAULT 4,  -- 1,2,2.5,3,4,5,6,7
  title                TEXT NOT NULL,
  role                 TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'READY',
  parallel_group       TEXT,
  scope_paths          TEXT NOT NULL DEFAULT '[]',   -- JSON array
  depends_on           TEXT NOT NULL DEFAULT '[]',   -- JSON array task ids
  acceptance_criteria  TEXT NOT NULL DEFAULT '',
  retry_count          INTEGER NOT NULL DEFAULT 0,
  split_depth          INTEGER NOT NULL DEFAULT 0,   -- zmiana #7: max 1
  parent_task          TEXT,
  substep              TEXT,
  review_notes         TEXT,
  branch               TEXT,
  worktree             TEXT,
  claimed_by           TEXT,
  created_at           REAL NOT NULL,
  updated_at           REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_milestone ON tasks(milestone);
CREATE TABLE IF NOT EXISTS metrics (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        REAL NOT NULL,
  task_id   TEXT,
  kind      TEXT NOT NULL,          -- token_usage | step_time | cr_reject | merge | ...
  payload   TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS approvals (
  id          TEXT PRIMARY KEY,
  milestone   TEXT NOT NULL,
  kind        TEXT NOT NULL DEFAULT 'DEPLOY_PROD',  -- DEPLOY_PROD | ROLLBACK
  payload     TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'PENDING',      -- PENDING | APPROVED | DECLINED
  created_at  REAL NOT NULL,
  resolved_at REAL
);
CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


class StateError(RuntimeError):
    pass


class InvalidTransition(StateError):
    pass


class State:
    """Thread-safe (per-thread connection) wrapper nad SQLite WAL."""

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # migracje: pola Telegram + multi-project (project_id, repo)
            for table, col, decl in (
                ("tasks", "telegram_thread_id", "INTEGER"),
                ("tasks", "telegram_summary_msg_id", "INTEGER"),
                ("tasks", "project_id", "TEXT DEFAULT 'mikowhy-teams'"),
                ("tasks", "repo", "TEXT"),  # 'factory' = katalog silnika, NULL = target_repo_path projektu
                ("milestones", "project_id", "TEXT DEFAULT 'mikowhy-teams'"),
            ):
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            conn.execute("""CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                target_repo_path TEXT NOT NULL, read_only INTEGER NOT NULL DEFAULT 0,
                config TEXT NOT NULL DEFAULT '{}')""")

    def set_telegram(self, task_id: str, thread_id: int | None, summary_msg_id: int | None) -> None:
        self._conn().execute(
            "UPDATE tasks SET telegram_thread_id=?, telegram_summary_msg_id=?, updated_at=? WHERE id=?",
            (thread_id, summary_msg_id, time.time(), task_id))

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    # --- milestones ---------------------------------------------------------
    def add_milestone(self, milestone_id: str, goal: str, branch: str, title: str = "",
                      project_id: str = "mikowhy-teams") -> None:
        now = time.time()
        self._conn().execute(
            "INSERT OR IGNORE INTO milestones(id,title,goal,branch,status,created_at,project_id) VALUES(?,?,?,?, 'OPEN', ?,?)",
            (milestone_id, title or milestone_id, goal, branch, now, project_id),
        )

    def get_milestone(self, milestone_id: str) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM milestones WHERE id=?", (milestone_id,)).fetchone()
        return dict(row) if row else None

    def list_milestones(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn().execute("SELECT * FROM milestones ORDER BY created_at")]

    def set_milestone_status(self, milestone_id: str, status: str) -> None:
        extra = ", merged_at=?" if status == "CLOSED" else ""
        params: list[Any] = [status]
        if status == "CLOSED":
            params.append(time.time())
        params.append(milestone_id)
        self._conn().execute(f"UPDATE milestones SET status=?{extra} WHERE id=?", params)

    # --- tasks --------------------------------------------------------------
    def add_task(
        self,
        task_id: str,
        milestone: str,
        phase: float,
        title: str,
        role: str,
        scope_paths: list[str] | None = None,
        depends_on: list[str] | None = None,
        parallel_group: str | None = None,
        acceptance_criteria: str = "",
        split_depth: int = 0,
        parent_task: str | None = None,
        project_id: str = "mikowhy-teams",
        status: str = READY,
        repo: str | None = None,
    ) -> str:
        now = time.time()
        self._conn().execute(
            """INSERT INTO tasks(id,milestone,phase,title,role,status,parallel_group,scope_paths,
               depends_on,acceptance_criteria,retry_count,split_depth,parent_task,created_at,updated_at,project_id,repo)
               VALUES(?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, milestone, phase, title, role, status, parallel_group,
                json.dumps(scope_paths or []), json.dumps(depends_on or []),
                acceptance_criteria, 0, split_depth, parent_task, now, now, project_id, repo,
            ),
        )
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, milestone: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        q, params = "SELECT * FROM tasks", []
        cond = []
        if milestone:
            cond.append("milestone=?"); params.append(milestone)
        if status:
            cond.append("status=?"); params.append(status)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY phase, created_at"
        return [self._row_to_task(r) for r in self._conn().execute(q, params)]

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> dict[str, Any]:
        t = dict(row)
        t["scope_paths"] = json.loads(t["scope_paths"])
        t["depends_on"] = json.loads(t["depends_on"])
        return t

    def ready_tasks(self) -> list[dict[str, Any]]:
        """Zadania READY, których wszystkie zależności są MERGED."""
        tasks = self.list_tasks(status=READY)
        merged = {t["id"] for t in self.list_tasks(status=MERGED)}
        return [t for t in tasks if set(t["depends_on"]) <= merged]

    def claim_task(self, task_id: str, claimed_by: str) -> dict[str, Any] | None:
        """Atomowe przejęcie: tylko jeśli status nadal READY (ACID, bez fcntl)."""
        cur = self._conn().execute(
            """UPDATE tasks SET status='RUNNING', claimed_by=?, updated_at=?
               WHERE id=? AND status='READY'
               RETURNING *""",
            (claimed_by, time.time(), task_id),
        )
        row = cur.fetchone()
        return self._row_to_task(row) if row else None

    def transition(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise StateError(f"Nieznane zadanie: {task_id}")
        current = task["status"]
        if new_status != current and new_status not in VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransition(f"{current} -> {new_status} niedozwolone dla {task_id}")
        sets = ["status=?", "updated_at=?"]
        params: list[Any] = [new_status, time.time()]
        for key, value in fields.items():
            if key not in {"substep", "review_notes", "branch", "worktree", "role", "retry_count", "claimed_by"}:
                raise StateError(f"Nieznane pole: {key}")  # telegram_* ustawia osobno set_telegram
            sets.append(f"{key}=?")
            params.append(value)
        params.append(task_id)
        self._conn().execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
        return self.get_task(task_id)  # type: ignore[return-value]

    def split_task(self, task_id: str, subtask_specs: list[dict[str, Any]], max_split_depth: int = 1) -> list[str]:
        """Zmiana #7: SPLIT_TASK dozwolony tylko do głębokości max_split_depth."""
        task = self.get_task(task_id)
        if task is None:
            raise StateError(task_id)
        if task["split_depth"] >= max_split_depth:
            raise StateError(
                f"Osiągnięto max_split_depth={max_split_depth} dla {task_id} — brak dalszych podziałów"
            )
        ids: list[str] = []
        for i, spec in enumerate(subtask_specs):
            child_id = f"{task_id}{chr(ord('a') + i)}"
            self.add_task(
                child_id,
                milestone=task["milestone"],
                phase=task["phase"],
                title=spec.get("title", f"{task['title']} (część {i + 1})"),
                role=spec.get("role", task["role"]),
                scope_paths=spec.get("scope_paths", task["scope_paths"]),
                depends_on=spec.get("depends_on", []),
                parallel_group=spec.get("parallel_group", task["parallel_group"]),
                acceptance_criteria=spec.get("acceptance_criteria", task["acceptance_criteria"]),
                split_depth=task["split_depth"] + 1,
                parent_task=task_id,
            )
            ids.append(child_id)
        # Oryginał zostaje jako agregator: blokuje się, dzieci mają ten sam milestone.
        self.transition(task_id, BLOCKED, review_notes="SPLIT into: " + ", ".join(ids))
        return ids

    def milestone_tasks(self, milestone: str) -> list[dict[str, Any]]:
        return self.list_tasks(milestone=milestone)

    def milestone_complete(self, milestone: str) -> bool:
        tasks = self.milestone_tasks(milestone)
        return bool(tasks) and all(t["status"] in (MERGED, BLOCKED) for t in tasks)

    # --- metrics / kv / approvals -------------------------------------------
    def record_metric(self, kind: str, payload: dict[str, Any], task_id: str | None = None) -> None:
        self._conn().execute(
            "INSERT INTO metrics(ts,task_id,kind,payload) VALUES(?,?,?,?)",
            (time.time(), task_id, kind, json.dumps(payload, ensure_ascii=False)),
        )

    def set_kv(self, key: str, value: Any) -> None:
        self._conn().execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps(value)))

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self._conn().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def create_approval(self, approval_id: str, milestone: str, payload: dict[str, Any], kind: str = "DEPLOY_PROD") -> None:
        self._conn().execute(
            "INSERT OR REPLACE INTO approvals(id,milestone,kind,payload,status,created_at) VALUES(?,?,?,?,'PENDING',?)",
            (approval_id, milestone, kind, json.dumps(payload, ensure_ascii=False), time.time()),
        )

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn().execute("SELECT * FROM approvals WHERE status='PENDING'")]

    def resolve_approval(self, approval_id: str, status: str) -> None:
        self._conn().execute(
            "UPDATE approvals SET status=?, resolved_at=? WHERE id=?", (status, time.time(), approval_id)
        )

    def merged_count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) c FROM tasks WHERE status='MERGED'").fetchone()["c"]

    # --- snapshot do repo ----------------------------------------------------
    def export_snapshot(self, path: Path | str | None = None, project_id: str | None = None) -> Path:
        """Eksport czytelnego pipeline.json (koniec milestonu / po mergu).
        Domyślnie: snapshot AKTYWNEGO projektu do .agents/projects/<pid>/pipeline.json
        (lub .agents/pipeline.json dla projektu self)."""
        from engine import projects
        pid = project_id or projects.active_project_id()
        path = Path(path) if path else projects.snapshot_path_for(pid)
        path.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {
            "schema": "dark-factory/pipeline-snapshot@2",
            "project_id": pid,
            "target_repo_path": projects.project_config(pid)["target_repo_path"],
            "read_only": projects.project_config(pid).get("read_only", False),
            "generated_at": time.time(),
            "note": "Snapshot wygenerowany z .agents/state.db (SQLite WAL). Stan operacyjny żyje w state.db; ten plik to artefakt czytelny per projekt.",
            "milestones": [m for m in self.list_milestones() if m.get("project_id", "mikowhy-teams") == pid],
            "tasks": [t for t in self.list_tasks() if t.get("project_id", "mikowhy-teams") == pid],
        }
        path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return path

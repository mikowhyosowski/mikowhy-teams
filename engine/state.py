"""Warstwa stanu operacyjnego: SQLite w trybie WAL (.agents/state.db).

Zmiana #8: zastępuje pipeline.json + fcntl. Daje natywne transakcje ACID,
atomowe przejęcia zadań (UPDATE ... WHERE status='READY' RETURNING *) i zero
deadlocków blokad plikowych. Czytelny snapshot pipeline.json jest eksportowany
do repozytorium tylko na koniec kamienia milowego (lub po każdym mergu).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def _detect_root() -> Path:
    """Katalog główny fabryki (v2.7.26).

    Priorytet: DARK_FACTORY_ROOT (env) > główne repo (gdy kod jedzie z worktree
    .agents/worktrees/<...> — stan żyje w repo macierzystym) > katalog modułu.
    Root cause: w worktree Path(__file__) wskazuje na kopię silnika, więc State()
    tworzył ŚWIEŻĄ, pustą .agents/state.db w worktree — testy integracyjne i skrypty
    odpalane z worktree widziały fabrykę „na czysto” i notorycznie walały QG.
    """
    here = Path(__file__).resolve().parent.parent
    env = os.environ.get("DARK_FACTORY_ROOT", "")
    if env:
        return Path(env)
    if "/.agents/worktrees/" in str(here):
        try:
            common = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=here, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if common:
                return Path(common).parent
        except Exception:
            pass  # nie mamy gita — zostań przy katalogu modułu (stare zachowanie)
    return here


ROOT = _detect_root()

DB_PATH = ROOT / ".agents" / "state.db"
SNAPSHOT_PATH = ROOT / ".agents" / "pipeline.json"

# --- Statusy zadań w DAG ---------------------------------------------------
# Docelowy model (samoregulująca się linia produkcyjna, Opiekun = ostatnia instancja):
#   READY -> RUNNING -> IN_REVIEW -> READY_TO_MERGE -> MERGED
#   IN_REVIEW -> CHANGES_REQUESTED -> RUNNING   (pętla poprawek po CR, bez Opiekuna)
#   dowolny etap pracy -> AWAITING_OWNER        (tylko graniczne decyzje / wyczerpane retry)
#   AWAITING_OWNER -> READY (decyzja w kontekście, pełny cykl) | -> BLOCKED (odrzucenie)
#   BLOCKED -> READY tylko po usunięciu przyczyny (pełny cykl od READY)
#   FAILED/ESCALATED zostają jako legacy (auto-retry i migracja w __init__).
READY = "READY"                    # kolejka: kompletne, z priorytetem, gotowe do przejęcia
RUNNING = "RUNNING"                # aktywna praca wykonawcza (implementacja, testy, poprawki)
IN_REVIEW = "IN_REVIEW"            # czeka na niezależny Code Review
CHANGES_REQUESTED = "CHANGES_REQUESTED"  # CR zwrócił uwagi — wraca do RUNNING z listą poprawek
READY_TO_MERGE = "READY_TO_MERGE"  # CR zatwierdził
MERGED = "MERGED"                  # zmergowane do gałęzi milestonu — koniec standardowego obiegu
AWAITING_OWNER = "AWAITING_OWNER"  # czeka na Opiekuna (Sponsora) — TYLKO ostateczność
BLOCKED = "BLOCKED"                # twarda blokada (odrzucenie Opiekuna / brak dostępu / wyczerpane auto-retry)
FAILED = "FAILED"                  # legacy: błąd przejściowy — auto-retry z limitem prób (backoff)
ESCALATED = "ESCALATED"            # legacy: migrowane do AWAITING_OWNER przy starcie
COOLING = "COOLING"                # ❄️ v2.7.23: zamrożone po przejściowym błędzie — sleep bezpieczeństwa, potem orchestrator budzi -> READY
COOLING = "COOLING"                # ❄️ v2.7.23: zamrożone po przejściowym błędzie — bezpieczny sleep, orchestrator budzi -> READY
AWAITING_APPROVAL = AWAITING_OWNER  # alias wsteczny — stare odniesienia w kodzie = AWAITING_OWNER

def substep_progress(substep: str | None) -> float:
    """Postęp zadania 0..1 wyliczony z substepu: '2/5_IMPL' -> 0.4, '4.5_SIMPLIFY' -> 0.9,
    '5/5_COMMIT'/'done' -> 1.0, 'LLM_EXEC' -> 0.5, brak/ASK_OWNER/OWNER_PACKET -> 0.0."""
    s = (substep or "").strip()
    if not s or s in ("ASK_OWNER", "ASK_SPONSOR", "OWNER_PACKET"):
        return 0.0
    if s == "done":
        return 1.0
    if s == "LLM_EXEC":
        return 0.5
    m = re.match(r"(\d+(?:\.\d+)?)\s*/\s*5", s)  # 2/5_IMPL
    if not m:
        m = re.match(r"(\d+(?:\.\d+)?)(?:b|_|$)", s)  # 4.5_SIMPLIFY, 4b_FIX
    if m:
        return max(0.0, min(1.0, float(m.group(1)) / 5.0))
    return 0.0


def estimate_eta_s(title: str = "", scope_paths: list | None = None, read_only: bool = False,
                   mode: str = "pipeline", priority: str = "P2") -> float:
    """v2.7.19: heurystyczny ETA (sekundy) przy tworzeniu zadania — baza + per scope path; agent doprecyzuje w planie."""
    base = 300.0 if (read_only or mode == "llm") else 600.0  # lekkie zadanie 5 min, normalne 10 min
    est = base + 240.0 * len(scope_paths or [])                # +4 min na każdy plik/z katalog w scope
    if priority == "P0":
        est *= 0.7
    return float(min(est, 4 * 3600))


def resume_substep(task: dict | None) -> str:
    """v2.7.9 + v2.7.16 — kontynuacja od NAJDALEJ wypracowanego sub-etapu przy powrocie do READY.
    Bierzemy last_completed_step (checkpoint z bazy), max_substep (rekord postępu z całej historii),
    albo substep (bieżący krok). Priorytet: last_completed_step > max_substep > substep.
    Markery specjalne (ASK_OWNER/OWNER_PACKET/brak) -> 1/5_PLAN; 'done' (po IN_REVIEW) -> 2/5_IMPL (poprawki)."""
    t = task or {}
    # 🔴 NOWE: Sprawdź last_completed_step (checkpoint z bazy SQLite)
    last_completed = t.get("last_completed_step") or ""
    if last_completed and substep_progress(last_completed) > 0:
        return last_completed
    # 🟢 Fallback: max_substep (stare zachowanie)
    cur = str(t.get("substep") or "")
    mx = str(t.get("max_substep") or "")
    if mx and substep_progress(mx) > substep_progress(cur):
        cur = mx  # wracamy do najdalej zapracowanego etapu, nie do bieżącego (cofnietego) 
    if not cur or cur in ("ASK_OWNER", "ASK_SPONSOR", "OWNER_PACKET"):
        return "1/5_PLAN"
    if cur == "done":
        return "2/5_IMPL"
    return cur


VALID_TRANSITIONS: dict[str, set[str]] = {
    READY: {RUNNING, BLOCKED, ESCALATED, AWAITING_OWNER},  # z kolejki też można oddać decyzję Opiekunowi (utknięte zadanie)
    RUNNING: {IN_REVIEW, CHANGES_REQUESTED, READY, FAILED, BLOCKED, ESCALATED, AWAITING_OWNER, COOLING},
    CHANGES_REQUESTED: {RUNNING, READY, FAILED, BLOCKED, ESCALATED, AWAITING_OWNER, COOLING},
    IN_REVIEW: {READY_TO_MERGE, CHANGES_REQUESTED, READY, BLOCKED, ESCALATED, AWAITING_OWNER, COOLING},
    READY_TO_MERGE: {MERGED, READY, BLOCKED, ESCALATED, AWAITING_OWNER},
    MERGED: set(),
    FAILED: {READY, BLOCKED, ESCALATED, AWAITING_OWNER},  # auto-retry -> READY; po max_retries -> AWAITING_OWNER
    BLOCKED: {READY, ESCALATED, AWAITING_OWNER},          # odblokowanie WYŁĄCZNIE do READY (pełny cykl)
    ESCALATED: {READY, AWAITING_OWNER, BLOCKED},          # legacy
    AWAITING_OWNER: {READY, BLOCKED},                     # decyzja Opiekuna -> READY (z decyzją w kontekście); odrzucenie -> BLOCKED
    # ❄️ COOLING — zamrożone po przejściowym błędzie: bezpieczny sleep, orchestrator budzi -> READY (co → na co w notyfikacji)
    COOLING: {READY, BLOCKED, AWAITING_OWNER},
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
-- wątki rozmów Sponsora z agentami (widoczne też na dashboardzie w zakładce Zespół)
CREATE TABLE IF NOT EXISTS threads (
  id         TEXT PRIMARY KEY,
  role       TEXT NOT NULL,
  title      TEXT NOT NULL,
  chat_id    TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  open       INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS thread_messages (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id TEXT NOT NULL,
  ts        REAL NOT NULL,
  author    TEXT NOT NULL,          -- 'sponsor' | rola agenta | 'system'
  text      TEXT NOT NULL,
  kind      TEXT NOT NULL DEFAULT 'msg'  -- msg | reply | sys
);
CREATE INDEX IF NOT EXISTS idx_threads_role ON threads(role, updated_at);
CREATE INDEX IF NOT EXISTS idx_tm_thread ON thread_messages(thread_id, ts);
-- obowiązki cykliczne per rola (zakładka „Obowiązki”); domyślnie niski priorytet P3,
-- żeby zawsze ustępowały bieżącym sprawom (P0..P2) w kolejce
CREATE TABLE IF NOT EXISTS recurring_tasks (
  id          TEXT PRIMARY KEY,
  role        TEXT NOT NULL,
  title       TEXT NOT NULL,
  schedule    TEXT NOT NULL DEFAULT 'daily',   -- hourly | daily | weekly
  priority    TEXT NOT NULL DEFAULT 'P3',
  last_run    REAL,
  created_at  REAL NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_recurring_role ON recurring_tasks(role, active);
-- kolejka wywołań LLM (engine/llm_queue.py) — serializuje dostęp do vibe CLI / keyring
CREATE TABLE IF NOT EXISTS llm_jobs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL DEFAULT 'complete',
  payload     TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'QUEUED',
  attempts    INTEGER NOT NULL DEFAULT 0,
  delay_until REAL NOT NULL DEFAULT 0,
  lease_until REAL NOT NULL DEFAULT 0,
  result      TEXT,
  error       TEXT,
  requester   TEXT,
  step        TEXT,
  created_at  REAL NOT NULL,
  done_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_status ON llm_jobs(status, created_at);
-- załączniki/rezultaty zadania (v2.7.10): pliki wypracowane przez agenta, klikalne w dashboardzie
CREATE TABLE IF NOT EXISTS task_attachments (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id  TEXT,
  path     TEXT NOT NULL,          -- ścieżka względem ROOT repo
  label    TEXT NOT NULL DEFAULT '',
  kind     TEXT NOT NULL DEFAULT '',  -- rozszerzenie: md/png/json/...
  size     INTEGER NOT NULL DEFAULT 0,
  ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_task ON task_attachments(task_id);
-- v2.7.11: skille nadane roli (szkolenia) + historia (kiedy, skąd) — profil pracownika w dashboardzie
CREATE TABLE IF NOT EXISTS role_skills (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  role     TEXT NOT NULL,
  name     TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  source   TEXT NOT NULL DEFAULT 'sponsor',
  ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_role_skills ON role_skills(role, ts);
-- v2.7.15: powiadomienia systemowe dla Opiekuna (dzwonek w dashboardzie, z czasem przy każdej)
CREATE TABLE IF NOT EXISTS notifications (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  kind     TEXT NOT NULL,          -- queue | merge | owner | deploy | circuit | recurring | error | info
  text     TEXT NOT NULL DEFAULT '',
  task_id  TEXT,
  read     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_notif_ts ON notifications(ts);
-- v2.7.23: rejestr operacji — WSZYSTKO co przechodzi przez kolejki/system (llm | git | qg | tg) ma tu jeden spójny cykl:
-- QUEUED (⏳ czeka na wysłanie) -> RUNNING (📤 wysłano, w trakcie) -> DONE (✅) albo FAILED (❌ błąd, próba N; retry wraca do QUEUED)
CREATE TABLE IF NOT EXISTS ops (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  op_id       TEXT UNIQUE,
  kind        TEXT NOT NULL,              -- llm | git | qg | tg
  ref         TEXT,                       -- zewnętrzny id (np. llmj-xxxxxxxxxxxx), unikalny w ramach kind
  task_id     TEXT,
  label       TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'QUEUED',
  detail      TEXT NOT NULL DEFAULT '',
  attempts    INTEGER NOT NULL DEFAULT 0,
  queued_at   REAL NOT NULL,
  started_at  REAL,
  finished_at REAL,
  payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_ops_task ON ops(task_id, queued_at);
CREATE INDEX IF NOT EXISTS idx_ops_ref ON ops(kind, ref);
CREATE INDEX IF NOT EXISTS idx_ops_status ON ops(status, queued_at);
"""


class StateError(RuntimeError):
    pass


class InvalidTransition(StateError):
    pass


_CONNS_LOCAL = threading.local()  # v2.7.28: conn per-wątek (współdzielony conn = flaky IndexError/500 przy DDL)


class State:
    """Wrapper nad SQLite WAL. Połączenie per (wątek, db_path) — ThreadingHTTPServer
    i wątki tła (ask, telegram, orchestrator) NIE dzielą ze sobą sqlite3.Connection,
    więc równoległe żądania nie przeplatają cursorów (root cause sporadycznych
    IndexError 'tuple index out of range' w PRAGMA table_info i 500 na endpointach).
    WAL + busy_timeout=30s → czytelnicy równolegle, piszę czekają grzecznie."""

    def __init__(self, db_path: Path | str = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.executescript(SCHEMA)
            # migracje: pola Telegram + multi-project (project_id, repo)
            for table, col, decl in (
                ("tasks", "telegram_thread_id", "INTEGER"),
                ("tasks", "telegram_summary_msg_id", "INTEGER"),
                ("tasks", "project_id", "TEXT DEFAULT 'mikowhy-teams'"),
                ("tasks", "repo", "TEXT"),  # 'factory' = katalog silnika, NULL = target_repo_path projektu
                ("tasks", "read_only", "INTEGER NOT NULL DEFAULT 0"),  # zadanie nic nie commituje (audyt/decyzja/plan)
                ("tasks", "mode", "TEXT DEFAULT 'pipeline'"),  # 'pipeline' (domyślny) | 'llm' — wykonuje sam LLM (vibe z narzędziami)
                ("tasks", "started_at", "REAL"),  # kiedy worker przejął zadanie (do liczenia czasu pracy)
                ("tasks", "substep_since", "REAL NOT NULL DEFAULT 0"),  # v2.7.11: kiedy zaczął się bieżący sub-etap (live timer na kafelku)
                ("tasks", "max_substep", "TEXT"),  # v2.7.16: NAJDALEJ wypracowany sub-etap w całej historii — przy powrocie do READY wznawiamy od niego
                ("tasks", "eta_s", "REAL NOT NULL DEFAULT 0"),  # v2.7.19: szacowany czas zadania (s) — ETA, raport „za ile będzie”
                ("tasks", "cooldown_until", "REAL NOT NULL DEFAULT 0"),  # v2.7.23: ❄️ COOLING — aż kiedy śpić (epoch)
                ("tasks", "last_activity_at", "REAL NOT NULL DEFAULT 0"),  # v2.8.1: mtime / PTY activity timestamp
                ("llm_jobs", "task_id", "TEXT"),  # v2.7.24: steel guard — 1 aktywny job per (task, kind, step)
                ("tasks", "priority", "TEXT DEFAULT 'P2'"),  # P0=najwyższy … P3=najniższy
                ("tasks", "title_sponsor", "TEXT"),  # tytuł wyświetlany Sponsorowi — krótko, prostymi słowami
                ("milestones", "project_id", "TEXT DEFAULT 'mikowhy-teams'"),
            ):
                cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if col not in cols:
                    try:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                    except sqlite3.OperationalError:
                        pass  # wyścig: inny wątek/proces dodał kolumnę pierwszy (duplicate column)
            conn.execute("""CREATE TABLE IF NOT EXISTS projects (
                project_id TEXT PRIMARY KEY, name TEXT NOT NULL,
                target_repo_path TEXT NOT NULL, read_only INTEGER NOT NULL DEFAULT 0,
                config TEXT NOT NULL DEFAULT '{}')""")
            # migracja statusów do docelowego modelu: AWAITING_APPROVAL/ESCALATED -> AWAITING_OWNER
            # (Opiekun jako ostatnia instancja; stare zadania nie wiszą w martwych statusach)
            conn.execute("UPDATE tasks SET status='AWAITING_OWNER', updated_at=? "
                         "WHERE status IN ('AWAITING_APPROVAL','ESCALATED')", (time.time(),))
        # v2.7.23: podpięcie rejestru operacji (ops) pod wszystkie moduły wykonujące operacje zewnętrzne —
        # każdy git/qg/tg/llm leci przez jeden spójny rejestr ze statusem kolejki (QUEUED->RUNNING->DONE/FAILED)
        try:
            from engine import git_guard, quality_gates, telegram_gate
            rec = self.op_recorder()
            git_guard.set_op_recorder(rec)
            quality_gates.set_op_recorder(rec)
            telegram_gate.set_op_recorder(rec)
        except Exception:
            pass

    def set_telegram(self, task_id: str, thread_id: int | None, summary_msg_id: int | None) -> None:
        self._conn().execute(
            "UPDATE tasks SET telegram_thread_id=?, telegram_summary_msg_id=?, updated_at=? WHERE id=?",
            (thread_id, summary_msg_id, time.time(), task_id))

    def _conn(self) -> sqlite3.Connection:
        """v2.7.28: połączenie per (wątek, db_path) — thread-local. Kursory różnych
        wątków nigdy się nie przeplatają; WAL pozwala czytać równolegle."""
        key = str(self.db_path)
        conns = getattr(_CONNS_LOCAL, "conns", None)
        if conns is None:
            conns = {}
            _CONNS_LOCAL.conns = conns
        conn = conns.get(key)
        if conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            conns[key] = conn
        return conn

    def close(self) -> None:
        """Zamyka thread-localne połączenie dla tego db_path (wywoływane przy wyjściu procesu
        w wątku głównym; conn w innych wątkach znikają razem z nimi)."""
        conns = getattr(_CONNS_LOCAL, "conns", None)
        if conns:
            conn = conns.pop(str(self.db_path), None)
            try:
                conn.close()
            except Exception:
                pass

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
        read_only: bool = False,
        mode: str = "pipeline",
        priority: str = "P2",
        title_sponsor: str | None = None,
        eta_s: float = 0,
    ) -> str:
        now = time.time()
        if not eta_s:
            eta_s = estimate_eta_s(title, scope_paths, read_only, mode, priority)  # v2.7.19: domyślny ETA
        self._conn().execute(
            """INSERT INTO tasks(id,milestone,phase,title,role,status,parallel_group,scope_paths,
               depends_on,acceptance_criteria,retry_count,split_depth,parent_task,created_at,updated_at,project_id,repo,read_only,mode,priority,title_sponsor,eta_s)
               VALUES(?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, milestone, phase, title, role, status, parallel_group,
                json.dumps(scope_paths or []), json.dumps(depends_on or []),
                acceptance_criteria, 0, split_depth, parent_task, now, now, project_id, repo, int(read_only), mode,
                priority, title_sponsor or title, float(eta_s),
            ),
        )
        return task_id

    def set_eta(self, task_id: str, eta_s: float) -> None:
        """v2.7.19: agent doprecyzowuje ETA po planie (skill szacowania)."""
        self._conn().execute("UPDATE tasks SET eta_s=?, updated_at=? WHERE id=?",
                             (max(30.0, float(eta_s)), time.time(), task_id))

    def set_priority(self, task_id: str, priority: str) -> None:
        if priority not in ("P0", "P1", "P2", "P3"):
            raise StateError(f"Zły priorytet: {priority} (dozwolone P0..P3)")
        self._conn().execute("UPDATE tasks SET priority=?, updated_at=? WHERE id=?",
                             (priority, time.time(), task_id))
        self.record_metric("task_event", {"event": "priority", "to": priority}, task_id)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn().execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    # --- obowiązki cykliczne (recurring) per rola --------------------------------
    def add_recurring(self, recurring_id: str, role: str, title: str,
                      schedule: str = "daily", priority: str = "P3") -> str:
        """Zadanie cykliczne. Priorytet domyślnie P3 — niższy niż bieżące sprawy (P0..P2)."""
        if schedule not in ("hourly", "daily", "weekly"):
            raise StateError(f"zły schedule: {schedule} (hourly|daily|weekly)")
        if priority not in ("P0", "P1", "P2", "P3"):
            raise StateError(f"zły priorytet: {priority}")
        now = time.time()
        self._conn().execute(
            "INSERT OR REPLACE INTO recurring_tasks(id, role, title, schedule, priority, last_run, created_at, active) "
            "VALUES(?,?,?,?,?,?,?,1)",
            (recurring_id, role, title, schedule, priority, None, now))
        self.record_metric("recurring_add", {"role": role, "title": title[:120], "schedule": schedule}, None)
        return recurring_id

    def list_recurring(self, role: str | None = None, active_only: bool = True) -> list[dict[str, Any]]:
        q = "SELECT * FROM recurring_tasks"
        cond, params = [], []
        if role:
            cond.append("role=?"); params.append(role)
        if active_only:
            cond.append("active=1")
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY role, created_at"
        return [dict(r) for r in self._conn().execute(q, params).fetchall()]

    def set_recurring_active(self, recurring_id: str, active: bool) -> None:
        self._conn().execute("UPDATE recurring_tasks SET active=? WHERE id=?",
                             (1 if active else 0, recurring_id))

    def recurring_due(self, now: float | None = None) -> list[dict[str, Any]]:
        """Obowiązki, które powinny już wygenerować zadanie (wg schedule od last_run)."""
        now = now or time.time()
        span = {"hourly": 3600, "daily": 86400, "weekly": 604800}
        out = []
        for r in self.list_recurring(active_only=True):
            if r["last_run"] is None or now - r["last_run"] >= span.get(r["schedule"], 86400):
                out.append(r)
        return out

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
        t["progress"] = substep_progress(t.get("substep"))
        return t

    def ready_tasks(self) -> list[dict[str, Any]]:
        """Zadania READY, których wszystkie zależności są MERGED — sortowane po priorytecie (P0 pierwsze)."""
        tasks = self.list_tasks(status=READY)
        merged = {t["id"] for t in self.list_tasks(status=MERGED)}
        ok = [t for t in tasks if set(t["depends_on"]) <= merged]
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        ok.sort(key=lambda t: (order.get(t.get("priority") or "P2", 2), t.get("created_at") or 0))
        return ok

    def claimable_tasks(self) -> list[dict[str, Any]]:
        """READY (z zależnościami) + CHANGES_REQUESTED — oba statusy worker przejmuje do RUNNING.

        CHANGES_REQUESTED = pętla poprawek po Code Review: zadanie wraca do pracy
        bez angażowania Opiekuna (lista poprawek jest w review_notes).
        """
        out = self.ready_tasks()
        for t in self.list_tasks(status=CHANGES_REQUESTED):
            if t["id"] not in {x["id"] for x in out}:
                out.append(t)
        # v2.7.14 — kolejność pobierania przez agentów:
        #   1) wyższy priorytet pierwszy (P0 > P1 > P2 > P3)
        #   2) zadanie po 3+ retry (FAILED) -> KONIEC kolejki w swoim paśmie priorytetu (nie do Opiekuna!)
        #   3) wyższy stopień ukończenia pierwszy (mniej kroków do merge'a = szybciej zmergowane)
        #   4) od najstarszych (created_at rosnąco)
        prio_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

        def _queue_key(t: dict) -> tuple:
            failed3 = 1 if (t.get("retry_count") or 0) >= 3 else 0
            return (prio_order.get(t.get("priority") or "P2", 2),
                    failed3,
                    -substep_progress(t.get("substep")),
                    t.get("created_at") or 0)

        out.sort(key=_queue_key)
        return out

    def claim_task(self, task_id: str, claimed_by: str) -> dict[str, Any] | None:
        """Atomowe przejęcie: tylko jeśli status nadal READY lub CHANGES_REQUESTED (ACID, bez fcntl)."""
        now = time.time()
        cur = self._conn().execute(
            """UPDATE tasks SET status='RUNNING', claimed_by=?, updated_at=?, started_at=?, substep_since=?
               WHERE id=? AND status IN ('READY','CHANGES_REQUESTED')
               RETURNING *""",
            (claimed_by, now, now, now, task_id),  # v2.7.11: przejęcie = start timera bieżącego etapu
        )
        row = cur.fetchone()
        if row:
            from engine import personas as _p
            by = _p.persona_label(row["role"] or "worker") + f" ({claimed_by})"
            self.record_metric("task_event", {"from": "READY", "to": "RUNNING", "by": by,
                                              "changes": {"status": "READY → RUNNING", "claimed_by": claimed_by}}, task_id)
        return self._row_to_task(row) if row else None

    def transition(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task is None:
            raise StateError(f"Nieznane zadanie: {task_id}")
        current = task["status"]
        if new_status != current and new_status not in VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransition(f"{current} -> {new_status} niedozwolone dla {task_id}")
        sets = ["status=?", "updated_at=?"]
        cooldown_s = fields.pop("cooldown_s", None)  # v2.7.23: długość bezp. sleepu dla COOLING (sekundy)
        params: list[Any] = [new_status, time.time()]
        for key, value in fields.items():
            if key not in {"substep", "review_notes", "branch", "worktree", "role", "retry_count", "claimed_by"}:
                raise StateError(f"Nieznane pole: {key}")  # telegram_* ustawia osobno set_telegram
            sets.append(f"{key}=?")
            params.append(value)
        # v2.7.23: COOLING — zamrożenie na bezpieczny sleep; pobudka przez orchestrator (_wake_cooling)
        cd = cooldown_s if cooldown_s is not None else 300
        if new_status == COOLING:
            sets.append("cooldown_until=?")
            params.append(time.time() + float(cd if cd is not None else 300))
        elif "cooldown_until" not in fields:
            sets.append("cooldown_until=0")  # wyjście z mróżki (albo zwykłe przejście) — czyścimy
        params.append(task_id)
        self._conn().execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)
        # v2.7.11: zmiana sub-etapu = start nowego mikro-etapu (timer „teraz: …” na kafelku);
        # przejęcie do RUNNING też startuje timer (nawet gdy sub-etap się nie zmienił — kontynuacja)
        if ("substep" in fields and str(task.get("substep")) != str(fields["substep"])) or (new_status == RUNNING and current != RUNNING):
            self._conn().execute("UPDATE tasks SET substep_since=? WHERE id=?", (time.time(), task_id))
        # v2.7.16: aktualizacja rekordu max_substep — najdalej wypracowany etap w historii zadania
        if "substep" in fields:
            new_sub = str(fields["substep"])
            old_max = str(task.get("max_substep") or task.get("substep") or "")
            if substep_progress(new_sub) > substep_progress(old_max):
                self._conn().execute("UPDATE tasks SET max_substep=? WHERE id=?", (new_sub, task_id))
        # historia zmian / live feed: CO się zmieniło (pole: stare → nowe) + KTO (imię nazwisko rola)
        from engine import personas as _p
        changes: dict[str, str] = {}
        if new_status != current:
            changes["status"] = f"{current} → {new_status}"
        for key, value in fields.items():
            old = task.get(key)
            if str(old) != str(value):
                changes[key] = f"{str(old)[:40]} → {str(value)[:40]}"
        by = _p.persona_label(task.get("role") or "system")
        if fields.get("claimed_by"):
            by += f" ({fields['claimed_by']})"
        self.record_metric("task_event", {"from": current, "to": new_status, "changes": changes, "by": by}, task_id)
        # v2.7.22: powiadomienie o ważnej zmianie statusu — wyraźnie „co → na co” (+ zmiana etapu), dla Opiekuna
        if new_status != current and new_status in ("IN_REVIEW", "READY_TO_MERGE", "MERGED", "FAILED", "AWAITING_OWNER", "BLOCKED"):
            sub_note = ""
            if fields.get("substep") and str(fields["substep"]) != str(task.get("substep")):
                sub_note = f" · etap {task.get('substep') or '—'} → {fields['substep']}"
            self.add_notification("status", f"{task_id}: {current} → {new_status}{sub_note}", task_id)
        # akumulacja czasu pracy per rola (badge „przepracowane” w zakładce Zespół):
        # RUNNING -> cokolwiek: elapsed leci do kv work_s:<role>
        if current == RUNNING and new_status != RUNNING and task.get("started_at"):
            el = max(0.0, time.time() - float(task["started_at"]))
            role = task.get("role") or "worker"
            cur = float(self.get_kv(f"work_s:{role}", 0) or 0)
            self.set_kv(f"work_s:{role}", cur + el)
        # komentarz „co się dzieje” przy każdej zmianie statusu — format 🎯 Cel → ✅ Efekt,
        # krótkie zrozumiałe zdanie dla człowieka (co robimy i co to daje), deterministycznie
        try:
            note = str(fields.get("review_notes") or "")[:120]
            sub = str(fields.get("substep") or "")
            rs = resume_substep(task)  # v2.7.9: powrót do READY = kontynuacja od wypracowanego etapu
            key = (current, new_status)
            SUBSTEP_CEL = {
                "1/5_PLAN": "zaplanować szczegóły zadania",
                "2/5_IMPL": "zaimplementować zmiany",
                "3/5_TEST": "napisać i odpalić testy",
                "4/5_QG": "przejść bramki jakości (lint/testy)",
                "4.5_SIMPLIFY": "uprościć rozwiązanie po nieudanym QG",
                "5/5_COMMIT": "scommitować i wypchnąć gałąź",
                "LLM_EXEC": "wykonać zadanie w trybie LLM",
                "done": "zakończyć zadanie",
            }
            tmpl = {
                ("READY", "RUNNING"): f"🎯 Cel: rozpocząć realizację {task_id} → ✅ Efekt: worker przejął zadanie, praca wystartowała",
                ("RUNNING", "RUNNING"): (f"🔄 W TRAKCIE — etap {sub} ({int(substep_progress(sub)*100)}%): 🎯 Cel: {SUBSTEP_CEL.get(sub, 'kontynuować pracę')} — nad tym teraz pracuję"
                                          if sub else f"🔄 W TRAKCIE: 🎯 Cel: kontynuować {task_id} ({new_status})"),
                ("RUNNING", "IN_REVIEW"): "🎯 Cel: oddać kod do niezależnego review → ✅ Efekt: paczka zmian czeka na code review",
                ("IN_REVIEW", "READY_TO_MERGE"): "🎯 Cel: uzyskać akceptację review → ✅ Efekt: CR OK, zadanie gotowe do merge'a",
                ("IN_REVIEW", "CHANGES_REQUESTED"): "🎯 Cel: poprawić kod wg uwag z review → ✅ Efekt: lista poprawek zapisana, wracam do implementacji",
                ("CHANGES_REQUESTED", "RUNNING"): "🎯 Cel: wprowadzić poprawki po review → ✅ Efekt: pracuję nad uwagami recenzenta",
                ("READY_TO_MERGE", "MERGED"): "🎯 Cel: włączyć zmiany do gałęzi docelowej → ✅ Efekt: zadanie zmergowane, koniec obiegu",
                ("RUNNING", "FAILED"): f"🎯 Cel: dokończyć {task_id} → ✅ Efekt: próba nieudana ({note or 'błąd wykonania'}) — automat robi retry z backoffem",
                ("FAILED", "READY"): f"🎯 Cel: podnieść zadanie po błędzie przejściowym → ✅ Efekt: READY — kontynuacja od etapu {rs} (nie od początku), retry +1",
                ("FAILED", "AWAITING_OWNER"): "🎯 Cel: rozwiązać utknięcie, którego automat nie ogarnia → ✅ Efekt: pakiet A/B/C dla Opiekuna (max_retries wyczerpane)",
                ("RUNNING", "AWAITING_OWNER"): "🎯 Cel: uzyskać decyzję Opiekuna na rozjeździe → ✅ Efekt: czekam na wybór A/B/odrzuć (pakiet w review_notes)",
                ("READY", "AWAITING_OWNER"): "🎯 Cel: uzyskać decyzję Opiekuna na rozjeździe → ✅ Efekt: czekam na wybór A/B/odrzuć (pakiet w review_notes)",
                ("AWAITING_OWNER", "READY"): f"🎯 Cel: wdrożyć decyzję Opiekuna → ✅ Efekt: decyzja zapisana, READY — kontynuacja od etapu {rs}",
                ("AWAITING_OWNER", "BLOCKED"): f"🎯 Cel: zamknąć zadanie odrzucone przez Opiekuna → ✅ Efekt: BLOCKED ({note})",
                ("RUNNING", "BLOCKED"): f"🎯 Cel: zatrzymać pracę (blokada) → ✅ Efekt: BLOCKED ({note or 'zewnętrzna zależność'}) — odblokowanie tylko przez READY",
                ("READY", "BLOCKED"): f"🎯 Cel: zatrzymać zadanie (blokada) → ✅ Efekt: BLOCKED ({note or 'zewnętrzna zależność'})",
                ("BLOCKED", "READY"): f"🎯 Cel: odblokować zadanie po usunięciu przyczyny → ✅ Efekt: READY — kontynuacja od etapu {rs} (statusy pełnym cyklem, nic nie omija testów/CR)",
                ("IN_REVIEW", "READY"): "🎯 Cel: wrócić do poprawek po review → ✅ Efekt: uwagi recenzenta w review_notes",
                ("READY_TO_MERGE", "READY"): "🎯 Cel: cofnąć z kolejki do merge'a → ✅ Efekt: konflikt/poprawka, wracam do READY",
                ("RUNNING", "ESCALATED"): "🎯 Cel: uzyskać decyzję człowieka → ✅ Efekt: eskalacja (legacy → AWAITING_OWNER)",
                ("READY", "ESCALATED"): "🎯 Cel: uzyskać decyzję człowieka → ✅ Efekt: eskalacja (legacy → AWAITING_OWNER)",
            }.get(key)
            if tmpl is None:
                tmpl = f"🎯 Cel: przejść {current} → {new_status} w zadaniu {task_id} → ✅ Efekt: status = {new_status}"
            if new_status == READY and "kontynuacja" not in tmpl:
                tmpl += f" — kontynuacja od etapu {rs}"
            if key == ("READY", "RUNNING") and sub:
                # start pracy od razu z podetapem (np. 1/5_PLAN) = też „w trakcie”, nie „zrobione”
                tmpl = f"🔄 W TRAKCIE — etap {sub} ({int(substep_progress(sub)*100)}%): 🎯 Cel: {SUBSTEP_CEL.get(sub, 'rozpocząć pracę')} — nad tym teraz pracuję"
            # v2.7.24: licznik „który raz” — jeśli zadanie już przechodziło TO SAMO przejście,
            # dopisz do komentarza, że to kolejna pętla (Opiekun od razu widzi powtórki)
            try:
                same = self._conn().execute(
                    "SELECT COUNT(*) FROM metrics WHERE kind='task_event' AND task_id=? "
                    "AND json_extract(payload,'$.to')=?",
                    (task_id, new_status)).fetchone()[0]
                if same > 1:
                    tmpl += f" — właśnie domykam pętlę (raz {same} w tym zadaniu)"
            except Exception:
                pass
            self.add_comment(task_id, by, tmpl)
        except Exception:
            pass
        return self.get_task(task_id)  # type: ignore[return-value]

    MAX_SUBTASKS = 3  # więcej zmian w jednym commicie, mniej podzadań/commitów

    def split_task(self, task_id: str, subtask_specs: list[dict[str, Any]], max_split_depth: int = 1) -> list[str]:
        """Zmiana #7: SPLIT_TASK dozwolony tylko do głębokości max_split_depth.
        Dodatkowo: max MAX_SUBTASKS podzadań (reszta odpada — anty-fragmentacja)."""
        task = self.get_task(task_id)
        if task is None:
            raise StateError(task_id)
        if task["split_depth"] >= max_split_depth:
            raise StateError(
                f"Osiągnięto max_split_depth={max_split_depth} dla {task_id} — brak dalszych podziałów"
            )
        if len(subtask_specs) > self.MAX_SUBTASKS:
            subtask_specs = subtask_specs[: self.MAX_SUBTASKS]
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

    def add_comment(self, task_id: str | None, author: str, text: str) -> None:
        """Komentarz agenta — feed w UI/menu (NIE push).
        ZASADA (Sponsor): każdy komentarz ma być krótkim zrozumiałym zdaniem dla człowieka
        w formacie 🎯 Cel → ✅ Efekt (co ktoś robił i co uzyskał). Jeśli text nie ma tego formatu — opakowujemy automatycznie."""
        text = str(text)
        if "Cel:" not in text:
            # brak formatu Cel/Efekt -> opakuj; samo „Cel:” (bez Efektu) = komunikat W TRAKCIE — zostawiamy
            if "Efekt:" not in text:
                text = f"🎯 Cel: praca nad zadaniem {task_id or '—'} → ✅ Efekt: {text}"
        # anty-duplikat (v2.7.9): identyczny komentarz (task, author, text) w ostatnich 15s -> pomiń
        try:
            _now = time.time()
            _rows = self._conn().execute(
                "SELECT payload, ts FROM metrics WHERE kind='agent_comment' AND task_id IS ? AND ts>? ORDER BY ts DESC LIMIT 30",
                (task_id, _now - 15)).fetchall()
            for _r in _rows:
                try:
                    _p = json.loads(_r[0])
                except Exception:
                    continue
                if _p.get("author") == author and str(_p.get("text")) == text:
                    return  # duplikat — nie zapisujemy drugi raz
        except Exception:
            pass
        self.record_metric("agent_comment", {"author": author, "text": text}, task_id)
        # v2.7.10 — zasada Sponsora: pliki/rezultaty wymienione w komentarzu lądują jako ZAŁĄCZNIKI zadania
        # (klikalny link w dashboardzie), żeby wszystko było w jednym miejscu
        if task_id:
            try:
                _added = 0
                for _m in re.finditer(r'(?<![\w./])((?:[\w.\-]+/)*[\w.\-]+\.(?:md|png|jpe?g|gif|txt|json|csv|log|patch|diff|html))',
                                       text, re.IGNORECASE):
                    if self.add_attachment(task_id, _m.group(1), label="auto: z komentarza") is not None:
                        _added += 1
                    if _added >= 5:
                        break
            except Exception:
                pass

    def add_comment_report(self, task_id: str | None, author: str, cel: str, efekt: str) -> None:
        """Komentarz raportowy: jawny Cel i Efekt — czytelny dla człowieka od razu."""
        self.add_comment(task_id, author, f"🎯 Cel: {cel} → ✅ Efekt: {efekt}")

    # --- załączniki / rezultaty zadania (v2.7.10) ------------------------------
    def add_attachment(self, task_id: str | None, path: str, label: str = "", kind: str = "") -> dict | None:
        """Dodaje załącznik (rezultat) do zadania — plik musi istnieć wewnątrz ROOT repo.
        Dedup po (task_id, path). Zwraca wiersz lub None gdy plik niedostępny."""
        try:
            rel = str(path).strip()
            full = (ROOT / rel).resolve()
            if not str(full).startswith(str(ROOT.resolve())) or not full.is_file():
                return None
            ext = full.suffix.lstrip(".").lower()
            row = self._conn().execute(
                "SELECT id FROM task_attachments WHERE task_id IS ? AND path=?", (task_id, rel)).fetchone()
            if row:
                return {"id": row[0], "task_id": task_id, "path": rel, "label": label, "kind": ext,
                        "size": full.stat().st_size, "ts": 0}
            cur = self._conn().execute(
                "INSERT INTO task_attachments(task_id, path, label, kind, size, ts) VALUES(?,?,?,?,?,?)",
                (task_id, rel, label or full.name, ext, full.stat().st_size, time.time()))
            return {"id": cur.lastrowid, "task_id": task_id, "path": rel, "label": label or full.name,
                    "kind": ext, "size": full.stat().st_size, "ts": time.time()}
        except Exception:
            return None

    def list_attachments(self, task_id: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id, task_id, path, label, kind, size, ts FROM task_attachments WHERE task_id=? ORDER BY ts DESC",
            (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_attachment(self, att_id: int) -> dict | None:
        r = self._conn().execute(
            "SELECT id, task_id, path, label, kind, size, ts FROM task_attachments WHERE id=?", (att_id,)).fetchone()
        return dict(r) if r else None

    # --- skille roli (szkolenia) — v2.7.11 --------------------------------------
    def add_role_skill(self, role: str, name: str, description: str = "", source: str = "sponsor") -> dict:
        cur = self._conn().execute(
            "INSERT INTO role_skills(role, name, description, source, ts) VALUES(?,?,?,?,?)",
            (role, name, description, source, time.time()))
        self.record_metric("skill_granted", {"role": role, "skill": name, "source": source}, None)
        return {"id": cur.lastrowid, "role": role, "name": name, "description": description,
                "source": source, "ts": time.time()}

    def list_role_skills(self, role: str) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id, role, name, description, source, ts FROM role_skills WHERE role=? ORDER BY ts DESC",
            (role,)).fetchall()
        return [dict(r) for r in rows]

    # --- powiadomienia systemowe (v2.7.15) ---------------------------------------
    def add_notification(self, kind: str, text: str, task_id: str | None = None) -> dict | None:
        """Dodaje powiadomienie dla Opiekuna. Dedup: identyczne (kind, task_id, text[:80]) w ostatnich 600s -> skip."""
        try:
            now = time.time()
            row = self._conn().execute(
                "SELECT id FROM notifications WHERE kind=? AND task_id IS ? AND substr(text,1,80)=? AND ts>? ORDER BY ts DESC LIMIT 1",
                (kind, task_id, str(text)[:80], now - 600)).fetchone()
            if row:
                return None
            cur = self._conn().execute(
                "INSERT INTO notifications(ts, kind, text, task_id, read) VALUES(?,?,?,?,0)",
                (now, kind, str(text)[:500], task_id))
            return {"id": cur.lastrowid, "ts": now, "kind": kind, "text": str(text)[:500], "task_id": task_id, "read": 0}
        except Exception:
            return None

    def list_notifications(self, limit: int = 60) -> list[dict]:
        rows = self._conn().execute(
            "SELECT id, ts, kind, text, task_id, read FROM notifications ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def unread_notifications(self) -> int:
        return int(self._conn().execute("SELECT COUNT(*) FROM notifications WHERE read=0").fetchone()[0])

    def mark_notifications_read(self) -> int:
        cur = self._conn().execute("UPDATE notifications SET read=1 WHERE read=0")
        return cur.rowcount or 0

    # --- v2.7.23: rejestr operacji (ops) — spójny status kolejki każdego działania ---
    OP_QUEUED, OP_RUNNING, OP_DONE, OP_FAILED = "QUEUED", "RUNNING", "DONE", "FAILED"

    def op_enqueue(self, kind: str, label: str, task_id: str | None = None, detail: str = "",
                   ref: str | None = None, payload: dict | None = None) -> str:
        """Rejestruje operację w kolejce (status QUEUED = „czeka na wysłanie”). Zwraca op_id ('' przy błędzie).
        v2.7.24: dedup — identyczna operacja (kind+task_id+label) z ostatnich 30 min jest REUŻYWANA
        (attempts+1, queued_at=now), żeby rejestr nie puchł powtórzeniami tego samego kroku."""
        try:
            now = time.time()
            if ref:  # operacja z zewnętrznym ref (np. llmj-xxx) — uzupełnij ref jeśli op już istnieje
                self._conn().execute("UPDATE ops SET ref=? WHERE kind=? AND task_id IS ? AND ref IS NULL AND label=?",
                                     (ref, kind, task_id, str(label)[:160]))
            else:    # bez refa — reuse ostatniej identycznej ops (anty-duplikat, okno 30 min)
                row = self._conn().execute(
                    "SELECT op_id FROM ops WHERE kind=? AND task_id IS ? AND label=? "
                    "AND status IN ('QUEUED','RUNNING') AND queued_at>? ORDER BY id DESC LIMIT 1",
                    (kind, task_id, str(label)[:160], now - 1800)).fetchone()
                if row:
                    self._conn().execute("UPDATE ops SET attempts=attempts+1, queued_at=? WHERE op_id=?",
                                         (now, row[0]))
                    return row[0]
            op_id = "op-" + uuid.uuid4().hex[:12]
            self._conn().execute(
                "INSERT INTO ops(op_id,kind,ref,task_id,label,status,detail,attempts,queued_at,payload) "
                "VALUES(?,?,?,?,?,'QUEUED',?,0,?,?)",
                (op_id, kind, ref, task_id, str(label)[:160], str(detail)[:300], now,
                 json.dumps(payload or {}, ensure_ascii=False)[:500]))
            # okazjonalny prune (co ~50. enqueue): starsze niż 7 dni; przy >6000 wierszy zostawiamy najnowsze 6000
            if uuid.uuid4().int % 50 == 0:
                self._conn().execute("DELETE FROM ops WHERE queued_at < ?", (now - 7 * 86400,))
                n = self._conn().execute("SELECT COUNT(*) FROM ops").fetchone()[0]
                if n > 6000:
                    self._conn().execute("DELETE FROM ops WHERE id IN "
                                         "(SELECT id FROM ops ORDER BY id DESC LIMIT -6000)")
            return op_id
        except Exception:
            return ""

    def _op_set(self, op_id: str, status: str, detail: str | None = None, start: bool = False,
                finish: bool = False, bump_attempts: bool = False) -> None:
        try:
            now = time.time()
            sets = ["status=?"]
            params: list[Any] = [status]
            if detail is not None:
                sets.append("detail=?")
                params.append(str(detail)[:300])
            if start:
                sets.append("started_at=COALESCE(started_at,?)")
                params.append(now)
            if finish:
                sets.append("finished_at=?")
                params.append(now)
            if bump_attempts:
                sets.append("attempts=attempts+1")
            params.append(op_id)
            self._conn().execute(f"UPDATE ops SET {', '.join(sets)} WHERE op_id=?", params)
        except Exception:
            pass

    def op_start(self, op_id: str, detail: str | None = None) -> None:
        """QUEUED -> RUNNING („wysłano, w trakcie”), attempts+1."""
        if op_id:
            self._op_set(op_id, self.OP_RUNNING, detail, start=True, bump_attempts=True)

    def op_done(self, op_id: str, detail: str = "") -> None:
        if op_id:
            self._op_set(op_id, self.OP_DONE, detail or None, finish=True)

    def op_fail(self, op_id: str, detail: str = "") -> None:
        if op_id:
            self._op_set(op_id, self.OP_FAILED, detail, finish=True)

    def op_requeue(self, op_id: str, detail: str = "") -> None:
        """FAILED/RUNNING -> QUEUED (retry z backoffem), attempts+1 — op wraca na koniec kolejki."""
        if op_id:
            self._op_set(op_id, self.OP_QUEUED, detail, bump_attempts=True)

    # warianty po ref (zewnętrzny id, np. llmj-xxx) — używane przez llm_queue
    def _op_id_by_ref(self, kind: str, ref: str) -> str:
        try:
            row = self._conn().execute("SELECT op_id FROM ops WHERE kind=? AND ref=? ORDER BY id DESC LIMIT 1",
                                       (kind, ref)).fetchone()
            return row[0] if row else ""
        except Exception:
            return ""

    def op_start_ref(self, kind: str, ref: str, detail: str | None = None) -> None:
        self.op_start(self._op_id_by_ref(kind, ref), detail)

    def op_done_ref(self, kind: str, ref: str, detail: str = "") -> None:
        self.op_done(self._op_id_by_ref(kind, ref), detail)

    def op_fail_ref(self, kind: str, ref: str, detail: str = "") -> None:
        self.op_fail(self._op_id_by_ref(kind, ref), detail)

    def op_requeue_ref(self, kind: str, ref: str, detail: str = "") -> None:
        self.op_requeue(self._op_id_by_ref(kind, ref), detail)

    def list_ops(self, task_id: str | None = None, limit: int = 80, kind: str | None = None) -> list[dict]:
        q = "SELECT op_id, kind, ref, task_id, label, status, detail, attempts, queued_at, started_at, finished_at FROM ops"
        cond, params = [], []
        if task_id:
            cond.append("task_id=?")
            params.append(task_id)
        if kind:
            cond.append("kind=?")
            params.append(kind)
        if cond:
            q += " WHERE " + " AND ".join(cond)
        q += " ORDER BY queued_at DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._conn().execute(q, params).fetchall()]

    def request_stop(self, task_id: str) -> None:
        """v2.7.26: żądanie „⏹️ Zakończ po etapie" — worker kończy bieżący etap i pauzuje (READY)."""
        self.set_kv(f"stop_req:{task_id}", 1)

    def clear_stop(self, task_id: str) -> None:
        self.set_kv(f"stop_req:{task_id}", 0)

    def stop_requested(self, task_id: str) -> bool:
        return bool(self.get_kv(f"stop_req:{task_id}", 0))

    # --- ❤️ Ulubione zadania (v2.7.27): śledzone przez Sponsora, niezależnie od statusu

    def list_favorites(self) -> list[str]:
        try:
            return json.loads(self.get_kv("favorites:tasks", "[]") or "[]")
        except Exception:
            return []

    def toggle_favorite(self, task_id: str) -> bool:
        """Przełącza ❤️. Zwraca True gdy zadanie JEST teraz ulubione."""
        cur = self.list_favorites()
        if task_id in cur:
            cur.remove(task_id)
            is_fav = False
        else:
            cur.insert(0, task_id)
            is_fav = True
        self.set_kv("favorites:tasks", json.dumps(cur[:200]))
        self.record_metric("favorite_toggle", {"task": task_id, "on": is_fav}, task_id)
        return is_fav

    def wake_cooling(self) -> int:
        """❄️→▶️ v2.7.23: budzi zamrożone zadania (status COOLING), których sleep bezpieczeństwa minął -> READY."""
        cur = self._conn().execute(
            "UPDATE tasks SET status='READY', updated_at=?, cooldown_until=0 "
            "WHERE status='COOLING' AND (cooldown_until IS NULL OR cooldown_until<=?)",
            (time.time(), time.time()))
        return cur.rowcount or 0

    def recent_ops(self, limit: int = 100) -> list[dict]:
        return [dict(r) for r in self._conn().execute(
            "SELECT op_id, kind, ref, task_id, label, status, detail, attempts, queued_at, started_at, finished_at "
            "FROM ops ORDER BY queued_at DESC LIMIT ?", (limit,)).fetchall()]

    def ops_stats(self) -> dict:
        rows = self._conn().execute("SELECT status, COUNT(*) FROM ops GROUP BY status").fetchall()
        d = {r[0]: r[1] for r in rows}
        return {"queued": d.get("QUEUED", 0), "running": d.get("RUNNING", 0),
                "done": d.get("DONE", 0), "failed": d.get("FAILED", 0)}

    def op_recorder(self):
        """v2.7.23: fabryka recorder'a podpinana pod moduły wykonujące operacje (git_guard, quality_gates, telegram).
        Każda operacja przechodząca przez system kolejek ląduje w tabeli ops ze spójnym statusem."""
        st = self

        class _Rec:
            def __call__(self, kind: str, label: str, task_id: str | None = None, detail: str = "",
                         ref: str | None = None) -> str:
                return st.op_enqueue(kind, label, task_id=task_id, detail=detail, ref=ref)

            def start(self, op_id: str, detail: str | None = None) -> None:
                st.op_start(op_id, detail)

            def done(self, op_id: str, detail: str = "") -> None:
                st.op_done(op_id, detail)

            def fail(self, op_id: str, detail: str = "") -> None:
                st.op_fail(op_id, detail)

        return _Rec()

    def recent_comments(self, limit: int = 40, task_id: str | None = None) -> list[dict]:
        q = "SELECT ts, task_id, payload FROM metrics WHERE kind='agent_comment'"
        params: list = []
        if task_id:
            q += " AND task_id=?"
            params.append(task_id)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(q, params).fetchall()
        return [{"ts": r[0], "task_id": r[1], **json.loads(r[2])} for r in rows]

    def recent_events(self, limit: int = 40, task_id: str | None = None) -> list[dict]:
        """Ostatnie zmiany statusów/zadań (task_event) — live feed + historia na dashboardzie."""
        q = "SELECT ts, task_id, payload FROM metrics WHERE kind='task_event'"
        params: list = []
        if task_id:
            q += " AND task_id=?"
            params.append(task_id)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = self._conn().execute(q, params).fetchall()
        return [{"ts": r[0], "task_id": r[1], **json.loads(r[2])} for r in rows]

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

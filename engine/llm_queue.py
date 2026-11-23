"""LLM QUEUE — wszystkie krótkie wywołania LLM (complete) przechodzą przez kolejkę w SQLite.

Problem: wiele workerów równolegle odpala `vibe` CLI → współbieżny odczyt keyring/keys
(rc=3 "Reading API keys from the system keyring") i brak odpowiedzi. Rozwiązanie: kolejka
z lease + retry/backoff + rate limit; jeden dispatcher (tick orchestratora) wykonuje
wywołania SERYJNIE — keyring dotyka tylko jeden proces.

llm_jobs: id, kind(complete|agent), payload(JSON), status(QUEUED|RUNNING|DONE|FAILED),
attempts, delay_until, lease_until, result, error, requester, step, created_at, done_at.

Użycie:
  llm_queue.enqueue(state, kind="complete", payload={...}, requester="...", step="...") -> job_id
  llm_queue.wait_result(state, job_id, timeout=600) -> str (result) lub rzuca
  llm_queue.dispatch_once(state)  — wołane w ticku orchestratora (max_jobs=1, rate limit)
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from engine.state import State

QUEUE_TABLE = """
CREATE TABLE IF NOT EXISTS llm_jobs (
  id          TEXT PRIMARY KEY,
  kind        TEXT NOT NULL DEFAULT 'complete',
  payload     TEXT NOT NULL DEFAULT '{}',
  status      TEXT NOT NULL DEFAULT 'QUEUED',   -- QUEUED | RUNNING | DONE | FAILED
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
"""

MAX_ATTEMPTS = 5
LEASE_S = 240          # lease na RUNNING job (complete trwa < ~2 min)
MIN_INTERVAL_S = 0.8   # rate limit: min odstęp między startami kolejnych jobów (cross-process)
BACKOFF_BASE_S = 8     # backoff: delay = BASE * 2^(attempts-1)


def ensure_table(s: State) -> None:
    s._conn().executescript(QUEUE_TABLE)


def enabled(s: State | None = None) -> bool:
    """Czy kolejka LLM aktywna. Env LLM_QUEUE=0 wyłącza; kv llm_queue default 1."""
    import os
    if os.environ.get("LLM_QUEUE", "1") == "0":
        return False
    try:
        s = s or State()
        return int(s.get_kv("llm_queue", 1) or 0) == 1
    except Exception:
        return True


def enqueue(s: State, kind: str, payload: dict[str, Any], requester: str = "", step: str = "") -> str:
    ensure_table(s)
    jid = "llmj-" + uuid.uuid4().hex[:12]
    now = time.time()
    s._conn().execute(
        "INSERT INTO llm_jobs(id, kind, payload, status, attempts, delay_until, lease_until, requester, step, created_at) "
        "VALUES(?,?,?,'QUEUED',0,0,0,?,?,?)",
        (jid, kind, json.dumps(payload, ensure_ascii=False), requester, step, now))
    s.record_metric("llm_job_enqueued", {"job": jid, "kind": kind, "step": step, "requester": requester}, None)
    return jid


def claim_next(s: State, lease_s: int = LEASE_S) -> dict | None:
    """Atomowy claim: QUEUED (z delay_until) lub RUNNING z wygasłym lease (crash recovery)."""
    ensure_table(s)
    now = time.time()
    cur = s._conn().execute(
        """UPDATE llm_jobs SET status='RUNNING', lease_until=?, attempts=attempts+1
           WHERE id = (SELECT id FROM llm_jobs
                       WHERE (status='QUEUED' AND delay_until<=?)
                          OR (status='RUNNING' AND lease_until<=?)
                       ORDER BY created_at LIMIT 1)
           RETURNING *""",
        (now + lease_s, now, now))
    row = cur.fetchone()
    return dict(row) if row else None


def finish(s: State, job_id: str, result: str) -> None:
    ensure_table(s)
    s._conn().execute("UPDATE llm_jobs SET status='DONE', result=?, done_at=?, lease_until=0 WHERE id=?",
                      (result[:20000], time.time(), job_id))
    s.record_metric("llm_job_done", {"job": job_id}, None)


def fail(s: State, job_id: str, error: str, max_attempts: int = MAX_ATTEMPTS) -> None:
    """Retry z backoff: attempts < max -> QUEUED z delay_until = now + BASE*2^(attempts-1); inaczej FAILED."""
    ensure_table(s)
    row = s._conn().execute("SELECT attempts FROM llm_jobs WHERE id=?", (job_id,)).fetchone()
    attempts = (row[0] if row else 1)
    now = time.time()
    if attempts < max_attempts:
        delay = BACKOFF_BASE_S * (2 ** (attempts - 1))
        s._conn().execute(
            "UPDATE llm_jobs SET status='QUEUED', error=?, delay_until=?, lease_until=0 WHERE id=?",
            (error[:1000], now + delay, job_id))
        s.record_metric("llm_job_retry", {"job": job_id, "attempts": attempts, "delay_s": round(delay)}, None)
    else:
        s._conn().execute("UPDATE llm_jobs SET status='FAILED', error=?, done_at=?, lease_until=0 WHERE id=?",
                          (error[:1000], now, job_id))
        s.record_metric("llm_job_failed", {"job": job_id, "attempts": attempts, "error": error[:200]}, None)


def wait_result(s: State, job_id: str, timeout: float = 600.0, poll_s: float = 0.5) -> str:
    """Czeka na wynik joba (synchronicznie dla wołającego; wykonanie robi dispatcher)."""
    ensure_table(s)
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = s._conn().execute("SELECT status, result, error, attempts FROM llm_jobs WHERE id=?",
                                (job_id,)).fetchone()
        if not row:
            raise RuntimeError(f"llm job {job_id} zaginiony")
        st, res, err, att = row[0], row[1], row[2], row[3]
        if st == "DONE":
            return res or ""
        if st == "FAILED":
            raise RuntimeError(f"llm job {job_id} FAILED po {att} próbach: {err}")
        time.sleep(poll_s)
    raise TimeoutError(f"llm job {job_id} timeout {timeout}s (ostatni status: {st})")


def dispatch_once(s: State, max_jobs: int = 1, min_interval_s: float = MIN_INTERVAL_S) -> int:
    """Wykonuje do max_jobs jobów seryjnie z rate limitem. Zwraca ile wykonano.
    Wołane w ticku orchestratora — stąd tylko TEN proces dotyka keyring/vibe CLI."""
    ensure_table(s)
    from engine.llm_gateway import get_gateway
    did = 0
    for _ in range(max_jobs):
        # rate limit cross-process: plik z ts ostatniego startu
        import os
        last_file = s.db_path.parent / "llm_last_start"
        now = time.time()
        try:
            last = float(last_file.read_text().strip() or 0)
        except OSError:
            last = 0.0
        wait = min_interval_s - (now - last)
        if wait > 0:
            time.sleep(wait)
        job = claim_next(s)
        if not job:
            break
        try:
            last_file.write_text(str(time.time()))
        except OSError:
            pass
        payload = json.loads(job["payload"] or "{}")
        try:
            gw = get_gateway()
            if job["kind"] == "agent":
                from pathlib import Path
                proc = gw.agent(system=payload.get("system", ""), user=payload.get("user", ""),
                                cwd=Path(payload.get("cwd", str(s.db_path.parent.parent))), step=job["step"] or "agent",
                                model=payload.get("model", "strong-model"))
                finish(s, job["id"], f"rc={proc.returncode}\n{proc.stdout[-8000:]}")
            else:
                out = gw.complete(system=payload.get("system", ""), user=payload.get("user", ""),
                                  step=job["step"] or payload.get("step", "llm"), model=payload.get("model", "strong-model"))
                finish(s, job["id"], out)
            did += 1
        except Exception as e:
            fail(s, job["id"], repr(e))
            did += 1
    return did


def stats(s: State) -> dict:
    ensure_table(s)
    rows = s._conn().execute(
        "SELECT status, COUNT(*) n, SUM(attempts) att FROM llm_jobs GROUP BY status").fetchall()
    out = {r[0]: {"count": r[1], "attempts": r[2] or 0} for r in rows}
    return out


def recent_jobs(s: State, limit: int = 30) -> list[dict]:
    ensure_table(s)
    rows = s._conn().execute(
        "SELECT id, kind, status, attempts, step, requester, error, created_at, done_at FROM llm_jobs "
        "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

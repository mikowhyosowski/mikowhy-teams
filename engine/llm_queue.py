"""LLM QUEUE — wszystkie krótkie wywołania LLM (complete) przechodzą przez kolejkę w SQLite.

Problem: wiele workerów równolegle odpala `vibe` CLI → współbieżny odczyt keyring/keys
(rc=3 "Reading API keys from the system keyring") i brak odpowiedzi. Rozwiązanie: kolejka
z lease + retry/backoff + rate limit + priorytetami + deduplikacją; dispatcher wykonuje
wywołania SERYJNIE (keyring dotyka tylko jeden proces naraz — flock w llm_gateway).

ZOPTYMALIZOWANE (v2):
  - dispatch w OSOBNYM WĄTKU orchestratora (dispatch_loop) — tick nie blokuje się na czas joba LLM
  - LEASE_S > timeout subprocessu — brak re-claim/duplikatów RUNNING jobów
  - priorytety jobów (P0..P3) — claim_next bierze najpierw P0 (kolejka priorytetowa)
  - dedup: ten sam payload (dedup_key) w QUEUED/RUNNING -> jeden job zamiast wielu
  - per-job timeout_s (fast/strong) — szybciej failuje -> backoff zamiast wiszenia
  - MAX_ATTEMPTS=3, backoff 4s*2^n — szybciej do FAILED i dalej (worker robi auto-retry zadania)
  - housekeeping: DONE/FAILED starsze niż 24h są usuwane (mniejsza baza, szybsze zapytania)
  - indeks claim: (status, delay_until, priority, created_at)

llm_jobs: id, kind(complete|agent), payload(JSON), status(QUEUED|RUNNING|DONE|FAILED),
attempts, delay_until, lease_until, result, error, requester, step, priority, dedup_key,
created_at, done_at.

Użycie:
  llm_queue.enqueue(state, kind="complete", payload={...}, requester="...", step="...", priority="P2") -> job_id
  llm_queue.wait_result(state, job_id, timeout=...) -> str (result) lub rzuca
  llm_queue.dispatch_once(state)  — pojedynczy krok dispatchera (max_jobs, rate limit)
  llm_queue.dispatch_loop(state)  — pętla do osobnego wątku/procesu (orchestrator)
"""
from __future__ import annotations

import hashlib
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
  priority    TEXT NOT NULL DEFAULT 'P2',       -- P0..P3 (v2: kolejka priorytetowa)
  dedup_key   TEXT,                             -- v2: hash payloadu — anty-duplikat
  model       TEXT,                             -- v3: model z payloadu (grupowanie batchy)
  created_at  REAL NOT NULL,
  done_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_status ON llm_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_claim ON llm_jobs(status, delay_until, priority, created_at);
"""

MAX_ATTEMPTS = 3      # v2: 3 próby z backoffem, potem FAILED (worker robi własny auto-retry zadania)
LEASE_S = 480         # v2: lease > timeout subprocessu (fast 180s / strong 360s) — bez re-claim duplikatów
MIN_INTERVAL_S = 0.3  # v2: rate limit między startami (było 0.8)
BACKOFF_BASE_S = 4    # v2: backoff 4s * 2^(attempts-1) (było 8s)
CLEANUP_AFTER_S = 24 * 3600  # v2: housekeeping — DONE/FAILED starsze niż 24h
CACHE_TTL_S = 3600    # v3: cache odpowiedzi LLM — identyczny payload (dedup_key) z DONE w TTL nie robi nowego requestu
BATCH_MIN = 2         # v3: batch tylko gdy tyle+ jobów tego samego modelu czeka (1 request = wiele pytań)
BATCH_DEFAULT = 3     # v3: domyślny rozmiar batcha (nadpisuje kv llm_batch_size)

PRIO_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# per-model timeout joba (v2): fast = szybki model, strong = wolniejszy
MODEL_TIMEOUT_S = {"fast": 120, "strong": 300}


def ensure_table(s: State) -> None:
    # najpierw sama tabela (bez indeksu claim), potem migracja kolumn v2, potem indeksy
    s._conn().executescript("""
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
  priority    TEXT NOT NULL DEFAULT 'P2',
  dedup_key   TEXT,
  created_at  REAL NOT NULL,
  done_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_llm_jobs_status ON llm_jobs(status, created_at);
""")
    # migracja v2: kolumny priority/dedup_key dla starszych baz
    cols = {r[1] for r in s._conn().execute("PRAGMA table_info(llm_jobs)")}
    if "priority" not in cols:
        try:
            s._conn().execute("ALTER TABLE llm_jobs ADD COLUMN priority TEXT NOT NULL DEFAULT 'P2'")
        except Exception:
            pass
    if "dedup_key" not in cols:
        try:
            s._conn().execute("ALTER TABLE llm_jobs ADD COLUMN dedup_key TEXT")
        except Exception:
            pass
    if "model" not in cols:
        try:
            s._conn().execute("ALTER TABLE llm_jobs ADD COLUMN model TEXT")
        except Exception:
            pass
    s._conn().execute("CREATE INDEX IF NOT EXISTS idx_llm_jobs_claim ON llm_jobs(status, delay_until, priority, created_at)")
    s._conn().execute("CREATE INDEX IF NOT EXISTS idx_llm_jobs_batch ON llm_jobs(kind, model, status, attempts)")


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


def model_timeout_s(model: str) -> int:
    """Timeout joba per rodzaj modelu (fast/strong) — v2."""
    m = (model or "").lower()
    if "fast" in m or "flash" in m:
        return MODEL_TIMEOUT_S["fast"]
    return MODEL_TIMEOUT_S["strong"]


def _dedup_key(kind: str, payload: dict[str, Any]) -> str:
    raw = json.dumps({"k": kind, "p": payload}, ensure_ascii=False, sort_keys=True)
    return "dd-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def enqueue(s: State, kind: str, payload: dict[str, Any], requester: str = "", step: str = "",
            priority: str = "P2") -> str:
    """Dodaje job do kolejki.
    v2 dedup: job z tym samym payloadem już czeka/pracuje -> jego id (bez dublowania LLM).
    v3 cache: job z tym samym payloadem zakończony DONE w ostatniej godzinie -> jego id
    (wait_result od razu zwróci wynik z cache — zero nowych requestów LLM na powtórki)."""
    ensure_table(s)
    now = time.time()
    prio = priority if priority in PRIO_ORDER else "P2"
    dk = _dedup_key(kind, payload)
    row = s._conn().execute(
        "SELECT id FROM llm_jobs WHERE dedup_key=? AND status IN ('QUEUED','RUNNING') ORDER BY created_at LIMIT 1",
        (dk,)).fetchone()
    if row:
        s.record_metric("llm_job_dedup", {"job": row[0], "step": step, "requester": requester}, None)
        return row[0]
    # v3: cache odpowiedzi (DONE w TTL)
    row = s._conn().execute(
        "SELECT id FROM llm_jobs WHERE dedup_key=? AND status='DONE' AND done_at>=? ORDER BY done_at DESC LIMIT 1",
        (dk, now - CACHE_TTL_S)).fetchone()
    if row:
        s.record_metric("llm_cache_hit", {"job": row[0], "step": step, "requester": requester}, None)
        return row[0]
    jid = "llmj-" + uuid.uuid4().hex[:12]
    s._conn().execute(
        "INSERT INTO llm_jobs(id, kind, payload, status, attempts, delay_until, lease_until, requester, step, priority, dedup_key, model, created_at) "
        "VALUES(?,?,?,'QUEUED',0,0,0,?,?,?,?,?,?)",
        (jid, kind, json.dumps(payload, ensure_ascii=False), requester, step, prio, dk,
         str(payload.get("model", "")), now))
    s.record_metric("llm_job_enqueued", {"job": jid, "kind": kind, "step": step, "requester": requester, "priority": prio}, None)
    return jid


def claim_next(s: State, lease_s: int = LEASE_S) -> dict | None:
    """Atomowy claim: QUEUED (z delay_until) lub RUNNING z wygasłym lease (crash recovery).
    v2: kolejka priorytetowa — P0 pierwsze, potem najstarsze."""
    ensure_table(s)
    now = time.time()
    cur = s._conn().execute(
        """UPDATE llm_jobs SET status='RUNNING', lease_until=?, attempts=attempts+1
           WHERE id = (SELECT id FROM llm_jobs
                       WHERE (status='QUEUED' AND delay_until<=?)
                          OR (status='RUNNING' AND lease_until<=?)
                       ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                                created_at
                       LIMIT 1)
           RETURNING *""",
        (now + lease_s, now, now))
    row = cur.fetchone()
    return dict(row) if row else None


def claim_batch(s: State, model: str, n: int, lease_s: int = LEASE_S) -> list[dict]:
    """v3: atomowy claim do `n` jobów (kind=complete, dany model, tylko pierwsza próba attempts=0)
    — grupa do jednego requestu batchowego. Retry (attempts>0) idą pojedynczo, żeby nie zapętlić batcha."""
    ensure_table(s)
    now = time.time()
    cur = s._conn().execute(
        """UPDATE llm_jobs SET status='RUNNING', lease_until=?, attempts=attempts+1
           WHERE id IN (SELECT id FROM llm_jobs
                        WHERE kind='complete' AND model=? AND status='QUEUED' AND delay_until<=? AND attempts=0
                        ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                                 created_at
                        LIMIT ?)
           RETURNING *""",
        (now + lease_s, model, now, n))
    return [dict(r) for r in cur.fetchall()]


BATCH_MAX_CHARS = 20000  # v4: batch tylko gdy suma promptów < 20k znaków (inaczej pojedynczo — parse safety)


def _parse_batch(raw: str, n: int) -> dict[int, str]:
    """v4: parsowanie odpowiedzi batchowej po separatorach `###ODP-<i>###` (odporne na zagnieżdżony JSON).
    Zwraca {i: tekst}. Rzuca ValueError przy niepełnej liczbie odpowiedzi."""
    import re as _re
    parts = _re.split(r"###ODP-(\d+)###", raw)
    answers: dict[int, str] = {}
    for i in range(1, len(parts) - 1, 2):
        try:
            idx = int(parts[i])
        except ValueError:
            continue
        answers[idx] = parts[i + 1].strip()
    if set(answers) != set(range(1, n + 1)):
        raise ValueError(f"batch answers mismatch: {sorted(answers)} != 1..{n}")
    return answers


def _run_batch(s: State, jobs: list[dict]) -> int:
    """v4: wykonuje grupę jobów JEDNYM requestem LLM (batch), ale odpowiedź w formacie SEPARATOROWYM
    (`###ODP-1###\\n…\\n###ODP-2###…`), nie JSON — v3 padało na zagnieżdżonym JSON w odpowiedziach
    (llm_batch_fail ~30%). Przy zbyt dużych promptach (>BATCH_MAX_CHARS) wykonuje joby pojedynczo.
    Błąd parsowania -> fail każdego joba osobno (backoff; retrye pójdą pojedynczo)."""
    from engine.llm_gateway import get_gateway
    model = str(jobs[0].get("model") or "strong-model")
    payloads = []
    timeout_s = 0
    total_chars = 0
    for j in jobs:
        p = json.loads(j["payload"] or "{}")
        payloads.append({"step": j.get("step") or p.get("step") or "", "system": p.get("system", ""),
                         "user": p.get("user", "")})
        total_chars += len(payloads[-1]["system"]) + len(payloads[-1]["user"])
        try:
            timeout_s = max(timeout_s, int(p.get("timeout_s") or 0))
        except Exception:
            pass
    timeout_s = timeout_s or model_timeout_s(model)
    if total_chars > BATCH_MAX_CHARS:
        # za duży batch — pojedynczo (bezpieczniej dla parsera i kontekstu)
        did = 0
        for j in jobs:
            try:
                _run_job(s, j)
            except Exception as e:
                fail(s, j["id"], repr(e))
            did += 1
        s.record_metric("llm_batch_skip_big", {"n": len(jobs), "chars": total_chars}, None)
        return did
    gw = get_gateway()
    if hasattr(gw, "timeout"):
        gw.timeout = timeout_s
    sys_prompt = (
        "Odpowiadasz na WIELE osobnych zapytań JEDNĄ odpowiedzią (batch). "
        "Dostaniesz JSON: {\"items\": [{\"step\": ..., \"system\": ..., \"user\": ...}, ...]}. "
        "Każdy item to osobne zadanie. Odpowiedz WYŁĄCZNIE w formacie separatorowym, bez żadnego innego tekstu:\n"
        "###ODP-1###\n<pełna odpowiedź na items[0] — czysty tekst, może zawierać JSON jako tekst>\n"
        "###ODP-2###\n<items[1]>\n... aż do ostatniego. Dokładnie tyle sekcji ile items, w tej samej kolejności."
    )
    user_payload = json.dumps({"items": payloads}, ensure_ascii=False)
    try:
        raw = gw.complete(system=sys_prompt, user=user_payload, step="BATCH×" + str(len(jobs)),
                          model=model, _direct=True)
        answers = _parse_batch(raw, len(jobs))
    except Exception as e:
        for j in jobs:
            fail(s, j["id"], f"batch parse error: {e!r}"[:900])
        s.record_metric("llm_batch_fail", {"n": len(jobs), "model": model, "error": repr(e)[:200]}, None)
        return len(jobs)
    for i, j in enumerate(jobs, start=1):
        finish(s, j["id"], answers[i][:20000])
    s.record_metric("llm_batch_done", {"n": len(jobs), "model": model, "step": jobs[0].get("step", "")}, None)
    return len(jobs)


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


def wait_result(s: State, job_id: str, timeout: float = 900.0, poll_s: float = 0.25) -> str:
    """Czeka na wynik joba (synchronicznie dla wołającego; wykonanie robi dispatcher).
    v2: poll 0.25s (było 0.5s) — szybciej reaguje na DONE."""
    ensure_table(s)
    deadline = time.time() + timeout
    st = "?"
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


def cleanup(s: State, older_than_s: float = CLEANUP_AFTER_S) -> int:
    """v2 housekeeping: usuwa DONE/FAILED starsze niż `older_than_s` — baza nie puchnie."""
    ensure_table(s)
    cur = s._conn().execute(
        "DELETE FROM llm_jobs WHERE status IN ('DONE','FAILED') AND done_at < ?",
        (time.time() - older_than_s,))
    return cur.rowcount or 0


def _run_job(s: State, job: dict) -> None:
    """Wykonuje pojedynczy job (complete lub agent) z per-job timeoutem."""
    from engine.llm_gateway import get_gateway
    payload = json.loads(job["payload"] or "{}")
    gw = get_gateway()
    # v2: per-job timeout — job szybciej failuje i idzie w backoff zamiast wisieć
    t_s = int(payload.get("timeout_s") or 0)
    if t_s and hasattr(gw, "timeout"):
        gw.timeout = t_s
    if job["kind"] == "agent":
        from pathlib import Path
        proc = gw.agent(system=payload.get("system", ""), user=payload.get("user", ""),
                        cwd=Path(payload.get("cwd", str(s.db_path.parent.parent))), step=job["step"] or "agent",
                        model=payload.get("model", "strong-model"))
        finish(s, job["id"], f"rc={proc.returncode}\n{proc.stdout[-8000:]}")
    else:
        # _direct=True: dispatcher wykonuje OD RAZU (bez kolejki — inaczej deadlock)
        out = gw.complete(system=payload.get("system", ""), user=payload.get("user", ""),
                          step=job["step"] or payload.get("step", "llm"), model=payload.get("model", "strong-model"),
                          _direct=True)
        finish(s, job["id"], out)


def _rate_limit(s: State, min_interval_s: float) -> None:
    """Rate limit cross-process: min odstęp między startami kolejnych requestów LLM."""
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
    try:
        last_file.write_text(str(time.time()))
    except OSError:
        pass


def _busy_model(s: State) -> tuple[str, int] | None:
    """v3: model z największą liczbą czekających jobów (pierwsze próby) — kandydat do batcha."""
    now = time.time()
    row = s._conn().execute(
        """SELECT model, COUNT(*) n FROM llm_jobs
           WHERE kind='complete' AND status='QUEUED' AND delay_until<=? AND attempts=0 AND model IS NOT NULL AND model!=''
           GROUP BY model ORDER BY n DESC LIMIT 1""",
        (now,)).fetchone()
    if row and row[1] >= BATCH_MIN:
        return row[0], row[1]
    return None


def _reclaim_expired(s: State) -> int:
    """v4: RECLAIM wygasłych lease'ów — dispatcher jest seryjny (1 wątek), więc każdy job RUNNING
    z lease_until <= now to ZAWIESZONY job (crash/stuck dispatchera) i blokuje kolejkę na minuty.
    Przepychamy go od razu do QUEUED z backoffem (lub FAILED gdy attempts >= MAX)."""
    ensure_table(s)
    now = time.time()
    rows = s._conn().execute(
        "SELECT id, attempts FROM llm_jobs WHERE status='RUNNING' AND lease_until<=?", (now,)).fetchall()
    for r in rows:
        if (r["attempts"] or 0) >= MAX_ATTEMPTS:
            fail(s, r["id"], "lease expired (dispatcher stuck/crash)", MAX_ATTEMPTS)
        else:
            s._conn().execute(
                "UPDATE llm_jobs SET status='QUEUED', lease_until=0, delay_until=?, error=? WHERE id=?",
                (now + BACKOFF_BASE_S, "lease expired — reclaim", r["id"]))
            s.record_metric("llm_lease_reclaim", {"job": r["id"], "attempts": r["attempts"]}, None)
    return len(rows)


def dispatch_once(s: State, max_jobs: int = 4, min_interval_s: float = MIN_INTERVAL_S) -> int:
    """Wykonuje joby seryjnie z rate limitem. Zwraca ile jobów wykonano (batch liczy się jako tyle jobów,
    ile pytań w jednym requeście).
    v3: jeśli ≥ BATCH_MIN jobów tego samego modelu czeka -> grupuje je w JEDEN request LLM (batch),
    co zmniejsza liczbę wywołań CLI. Pojedyncze joby (agent / retry / solo) wykonuje osobno.
    keyring/vibe CLI dotyka tylko ten proces (flock w gateway serializuje)."""
    ensure_table(s)
    try:
        batch_size = max(BATCH_MIN, int(s.get_kv("llm_batch_size", BATCH_DEFAULT) or BATCH_DEFAULT))
    except Exception:
        batch_size = BATCH_DEFAULT
    _reclaim_expired(s)  # v4: najpierw odzyskaj zawieszone joby (lease wygasły), potem normalny dispatch
    did = 0
    while did < max_jobs:
        _rate_limit(s, min_interval_s)
        # v3: najpierw spróbuj batch (model z największą kolejką)
        busy = _busy_model(s)
        if busy and batch_size >= BATCH_MIN:
            model, n_waiting = busy
            n = min(batch_size, n_waiting, max_jobs - did)
            if n >= BATCH_MIN:
                jobs = claim_batch(s, model, n)
                if len(jobs) >= BATCH_MIN:
                    try:
                        did += _run_batch(s, jobs)
                    except Exception as e:
                        for j in jobs:
                            fail(s, j["id"], repr(e))
                        did += len(jobs)
                    continue
                elif jobs:
                    # claim zwrócił mniej niż BATCH_MIN (wyścig) — wykonaj pojedynczo
                    for j in jobs:
                        try:
                            _run_job(s, j)
                        except Exception as e:
                            fail(s, j["id"], repr(e))
                        did += 1
                    continue
        job = claim_next(s)
        if not job:
            break
        try:
            _run_job(s, job)
            did += 1
        except Exception as e:
            fail(s, job["id"], repr(e))
            did += 1
    return did


def dispatch_loop(s: State, poll_s: float = 0.3, max_jobs: int = 2) -> None:
    """v2: pętla dispatchera do OSOBNEGO WĄTKU — tick orchestratora nie czeka na LLM.
    Co jakiś czas robi housekeeping (cleanup starych jobów)."""
    ensure_table(s)
    idle = 0
    while True:
        try:
            did = dispatch_once(s, max_jobs=max_jobs)
            if did == 0:
                idle += 1
                if idle % 40 == 0:  # co ~12s przy poll 0.3s gdy pusto
                    try:
                        cleanup(s)
                    except Exception:
                        pass
                time.sleep(poll_s)
            else:
                idle = 0
        except Exception as e:
            try:
                s.record_metric("llm_dispatch_error", {"error": repr(e)}, None)
            except Exception:
                pass
            time.sleep(1.0)


def stats(s: State) -> dict:
    ensure_table(s)
    rows = s._conn().execute(
        "SELECT status, COUNT(*) n, SUM(attempts) att FROM llm_jobs GROUP BY status").fetchall()
    out = {r[0]: {"count": r[1], "attempts": r[2] or 0} for r in rows}
    return out


def recent_jobs(s: State, limit: int = 30) -> list[dict]:
    ensure_table(s)
    rows = s._conn().execute(
        "SELECT id, kind, status, attempts, step, requester, priority, error, created_at, done_at FROM llm_jobs "
        "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]

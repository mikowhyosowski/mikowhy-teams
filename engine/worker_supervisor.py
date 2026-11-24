"""Jeden nadzorca workerów.

Prawda o procesie to para (pid, start_time) z /proc, nie sam numer PID.
Dashboard woła inspect() i niczego nie ubija. Sprząta wyłącznie reconcile().
"""
from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

PROC = Path("/proc")
GRACE_S = 15.0
KILL_AFTER_S = 10.0
KEEP_DONE_S = 3600.0

_MODULES = {
    "engine.worker_lifecycle": "worker",
    "engine.code_review": "review",
}


def ensure_table(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS workers (
             pid INTEGER NOT NULL,
             start_time TEXT NOT NULL,
             pgid INTEGER,
             task_id TEXT,
             module TEXT,
             cwd TEXT,
             step TEXT,
             phase TEXT,
             cmd TEXT,
             rc INTEGER,
             state TEXT NOT NULL,
             started_at REAL,
             last_beat REAL,
             PRIMARY KEY (pid, start_time))""")


def _stat(pid: int) -> tuple[str, str, int] | None:
    """Stan z /proc, czas startu i grupa. Sam pid nie identyfikuje procesu."""
    try:
        raw = (PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    cut = raw.rfind(")")
    if cut < 0:
        return None
    fields = raw[cut + 2:].split()
    if len(fields) < 20:
        return None
    try:
        pgid = int(fields[2])
    except ValueError:
        return None
    return fields[0], fields[19], pgid


def _cmdline(pid: int) -> str:
    try:
        return (PROC / str(pid) / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def scan(proc_root: Path | None = None) -> list[dict[str, Any]]:
    """Żywe procesy worker_lifecycle i code_review."""
    root = proc_root or PROC
    found: list[dict[str, Any]] = []
    if not root.is_dir():
        return found
    for entry in root.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        cmd = _cmdline(pid) if proc_root is None else _read_fake_cmd(entry)
        module = next((name for needle, name in _MODULES.items() if needle in cmd), "")
        if not module:
            continue
        parts = cmd.split()
        task_id = parts[-1] if parts else ""
        if not task_id or "/" in task_id or task_id.endswith(".py"):
            continue
        if proc_root is None:
            st = _stat(pid)
            if st is None:
                continue
            state, start_time, pgid = st
        else:
            state, start_time, pgid = _read_fake_stat(entry, pid)
        found.append({
            "pid": pid,
            "start_time": start_time,
            "pgid": pgid,
            "task_id": task_id,
            "module": module,
            "state_os": state,
            "cmd": cmd[:300],
        })
    return found


def _read_fake_cmd(entry: Path) -> str:
    try:
        return (entry / "cmdline").read_text()
    except OSError:
        return ""


def _read_fake_stat(entry: Path, pid: int) -> tuple[str, str, int]:
    try:
        raw = (entry / "stat").read_text().split()
        return raw[0], raw[1], int(raw[2])
    except (OSError, IndexError, ValueError):
        return "R", f"fake-{pid}", pid


def note_spawn(state, pid: int, task_id: str, module: str, cwd: str = "") -> None:
    """Zapis tuż po Popen. start_time bierze z /proc, nie z zegara ściennego."""
    conn = state._conn()
    ensure_table(conn)
    st = _stat(pid)
    if st is None:
        return
    state_os, start_time, pgid = st
    now = time.time()
    phase = "ZYJE" if state_os != "Z" else "OSIEROCONY"
    conn.execute(
        """INSERT INTO workers(pid,start_time,pgid,task_id,module,cwd,step,phase,cmd,rc,state,started_at,last_beat)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(pid, start_time) DO UPDATE SET
             task_id=excluded.task_id, module=excluded.module, pgid=excluded.pgid,
             state=excluded.state, last_beat=excluded.last_beat""",
        (pid, start_time, pgid, task_id, module, cwd, "", phase, "", None, phase, now, now))


def beat(state, task_id: str, *, phase: str | None = None, step: str | None = None,
         cmd: str | None = None, rc: int | None = None) -> None:
    """Worker dopisuje fazę do swojego żywego wiersza."""
    conn = state._conn()
    ensure_table(conn)
    now = time.time()
    sets = ["last_beat=?"]
    params: list[Any] = [now]
    for col, val in (("phase", phase), ("step", step), ("rc", rc)):
        if val is not None:
            sets.append(f"{col}=?")
            params.append(val)
    if cmd is not None:
        sets.append("cmd=?")
        params.append(cmd[:300])
    params.append(task_id)
    conn.execute(
        f"""UPDATE workers SET {', '.join(sets)}
            WHERE task_id=? AND state IN ('ZYJE','KOMENDA','CZEKA_NA_LLM','KONCZY')""",
        params)


def inspect(state, procs: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Odczyt na odświeżenie. Nie zmienia zadań i nie wysyła sygnałów."""
    conn = state._conn()
    ensure_table(conn)
    live = procs if procs is not None else scan()
    rows = [dict(r) for r in conn.execute("SELECT * FROM workers").fetchall()]
    by_key = {(r["pid"], r["start_time"]): r for r in rows}
    out: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for proc in live:
        key = (proc["pid"], proc["start_time"])
        seen.add(key)
        row = by_key.get(key, {})
        alive = proc["state_os"] != "Z"
        out.append({
            "pid": proc["pid"],
            "start_time": proc["start_time"],
            "pgid": proc["pgid"],
            "task_id": proc["task_id"],
            "module": proc["module"],
            "step": row.get("step") or "",
            "phase": row.get("phase") or ("OSIEROCONY" if not alive else "ZYJE"),
            "cmd": row.get("cmd") or proc.get("cmd") or "",
            "rc": row.get("rc"),
            "state": "OSIEROCONY" if not alive else (row.get("state") or "ZYJE"),
            "alive": alive,
            "started_at": row.get("started_at"),
            "last_beat": row.get("last_beat"),
        })
    for row in rows:
        key = (row["pid"], row["start_time"])
        if key in seen:
            continue
        out.append({**row, "alive": False})
    out.sort(key=lambda r: (not r.get("alive"), r.get("task_id") or "", -(r.get("started_at") or 0)))
    return out


def _signal(pid: int, sig: int, pgid: int | None = None) -> None:
    try:
        if sig == signal.SIGKILL and pgid and pgid > 1:
            os.killpg(pgid, sig)
        else:
            os.kill(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def reconcile(state, procs: list[dict[str, Any]] | None = None, *, kill: bool = True) -> dict[str, Any]:
    """Jedno miejsce sprzątania. Zwraca co zrobił, żeby test i log widziały to samo."""
    conn = state._conn()
    ensure_table(conn)
    now = time.time()
    live = procs if procs is not None else scan()
    signals: list[dict[str, Any]] = []
    recovered: list[str] = []

    rows = [dict(r) for r in conn.execute("SELECT * FROM workers").fetchall()]
    by_key = {(r["pid"], r["start_time"]): r for r in rows}
    live_keys = set()

    for proc in live:
        key = (proc["pid"], proc["start_time"])
        live_keys.add(key)
        prev = by_key.get(key)
        if proc["state_os"] == "Z":
            phase, state_name = "OSIEROCONY", "OSIEROCONY"
        elif prev and prev.get("state") == "KONCZY":
            phase, state_name = "KONCZY", "KONCZY"
        else:
            phase = (prev or {}).get("phase") or "ZYJE"
            state_name = (prev or {}).get("state") or "ZYJE"
        if prev is None:
            conn.execute(
                """INSERT INTO workers(pid,start_time,pgid,task_id,module,cwd,step,phase,cmd,rc,state,started_at,last_beat)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (proc["pid"], proc["start_time"], proc["pgid"], proc["task_id"], proc["module"],
                 "", "", phase, proc.get("cmd") or "", None, state_name, now, now))
            by_key[key] = {"pid": proc["pid"], "start_time": proc["start_time"], "state": state_name,
                           "phase": phase, "started_at": now, "last_beat": now, "module": proc["module"],
                           "task_id": proc["task_id"]}
        elif state_name == "KONCZY":
            conn.execute(
                """UPDATE workers SET pgid=?, task_id=?, module=?
                   WHERE pid=? AND start_time=?""",
                (proc["pgid"], proc["task_id"], proc["module"], proc["pid"], proc["start_time"]))
        else:
            conn.execute(
                """UPDATE workers SET pgid=?, task_id=?, module=?, state=?, phase=?, last_beat=?
                   WHERE pid=? AND start_time=?""",
                (proc["pgid"], proc["task_id"], proc["module"], state_name, phase, now,
                 proc["pid"], proc["start_time"]))

    # duplikaty: zostaje najnowszy żywy, reszta dostaje SIGTERM
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for proc in live:
        if proc["state_os"] == "Z":
            continue
        grouped.setdefault((proc["module"], proc["task_id"]), []).append(proc)
    for (_module, task_id), procs_g in grouped.items():
        if len(procs_g) < 2:
            continue
        procs_g.sort(key=lambda p: p["start_time"])
        keep = procs_g[-1]
        for proc in procs_g:
            if proc is keep:
                continue
            _mark(conn, proc, "KONCZY", now)
            signals.append({"pid": proc["pid"], "sig": "TERM", "why": "duplikat", "task": task_id})
            if kill:
                _signal(proc["pid"], signal.SIGTERM, proc["pgid"])

    # zły status albo KONCZY, które wisi
    for proc in live:
        if proc["state_os"] == "Z":
            continue
        row = by_key.get((proc["pid"], proc["start_time"])) or {}
        started = row.get("started_at") or now
        task = state.get_task(proc["task_id"])
        status = (task or {}).get("status") or ""
        wanted = "RUNNING" if proc["module"] == "worker" else "IN_REVIEW"
        if status != wanted and (now - started) > GRACE_S and row.get("state") != "KONCZY":
            _mark(conn, proc, "KONCZY", now)
            signals.append({"pid": proc["pid"], "sig": "TERM", "why": f"status {status}", "task": proc["task_id"]})
            if kill:
                _signal(proc["pid"], signal.SIGTERM, proc["pgid"])
            continue
        if row.get("state") == "KONCZY" and (now - (row.get("last_beat") or started)) > KILL_AFTER_S:
            signals.append({"pid": proc["pid"], "sig": "KILL", "why": "konczy", "task": proc["task_id"]})
            if kill:
                _signal(proc["pid"], signal.SIGKILL, proc["pgid"])

    # wiersze bez procesu
    for row in rows:
        if (row["pid"], row["start_time"]) in live_keys:
            continue
        if row.get("state") == "SKONCZYL":
            continue
        task = state.get_task(row["task_id"]) if row.get("task_id") else None
        presence = (task or {}).get("presence") or "AKTYWNE"
        if task and task.get("status") == "RUNNING" and presence == "AKTYWNE" and row.get("module") == "worker":
            try:
                state.transition(
                    task["id"], "READY", claimed_by="",
                    review_notes=f"{(task.get('review_notes') or '')} | nadzorca: proces {row['pid']} zniknął, etap zostaje do wznowienia")
            except Exception:
                conn.execute(
                    "UPDATE tasks SET status='READY', claimed_by='', updated_at=? WHERE id=? AND status='RUNNING'",
                    (now, task["id"]))
            recovered.append(task["id"])
        conn.execute(
            "UPDATE workers SET state='SKONCZYL', phase='SKONCZYL', last_beat=? WHERE pid=? AND start_time=?",
            (now, row["pid"], row["start_time"]))

    conn.execute(
        "DELETE FROM workers WHERE state='SKONCZYL' AND COALESCE(last_beat, started_at, 0) < ?",
        (now - KEEP_DONE_S,))
    return {"signals": signals, "recovered": recovered, "workers": inspect(state, procs=live)}


def _mark(conn, proc: dict, state_name: str, now: float) -> None:
    conn.execute(
        "UPDATE workers SET state=?, phase=?, last_beat=? WHERE pid=? AND start_time=?",
        (state_name, state_name, now, proc["pid"], proc["start_time"]))

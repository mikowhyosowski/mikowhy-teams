"""
WebSocket gateway — live push procesów (runy pipeline) i logów runów.

Zamiast pollingu REST co 2 s, klient łączy się przez ws://<host>:<ws_port>/ i dostaje:
  - "pulse"  — broadcast co ~1 s (tylko gdy stan się zmienił): żywe runy, ostatnie runy,
               health workerów + lista zacięć (stall);
  - "log"    — stream nowych linii logu subskrybowanego runu (subscribe_log / unsubscribe_log);
  - "hello" / "pong" — handshake i keepalive.

Uruchamiany w wątku daemon z engine.dashboard.main() (--ws-port, --no-ws).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_factory_root = os.environ.get("DARK_FACTORY_ROOT")
if _factory_root and os.path.isdir(_factory_root):
    ROOT = Path(_factory_root).resolve()
else:
    ROOT = Path(__file__).resolve().parent.parent

import sys
sys.path.insert(0, str(ROOT))

from engine.state import State

PULSE_INTERVAL_S = 1.0


def _worker_health() -> dict:
    """Taki sam payload jak REST /api/workers/health."""
    from engine import console_streamer as cs
    s = State()
    now = time.time()
    try:
        threshold_s = float(s.get_kv("stall_threshold_s", 600) or 600)
    except Exception:
        threshold_s = 600.0
    workers_out = []
    stalled_out = []
    for t in s.list_tasks(status="RUNNING"):
        tid = t["id"]
        la = cs.last_activity_at(tid)
        if la == 0.0:
            log_p = ROOT / ".agents" / "logs" / f"worker_lifecycle.{tid}.log"
            try:
                la = log_p.stat().st_mtime
            except FileNotFoundError:
                la = t.get("updated_at") or now
        idle_s = max(0.0, now - la)
        llm_busy = bool(s._conn().execute(
            "SELECT 1 FROM llm_jobs WHERE task_id=? AND status='RUNNING' LIMIT 1",
            (tid,)).fetchone())
        is_stalled = (idle_s >= threshold_s) and not llm_busy
        if is_stalled:
            stalled_out.append(tid)
        workers_out.append({
            "task_id": tid,
            "substep": t.get("substep"),
            "claimed_by": t.get("claimed_by"),
            "last_activity_at": la,
            "idle_s": round(idle_s, 1),
            "llm_job_running": llm_busy,
            "stall": is_stalled,
        })
    return {"workers": workers_out, "stalled": stalled_out, "stall_threshold_s": threshold_s}


def _pulse_payload() -> dict:
    s = State()
    live = s.active_process_runs()
    recent = s.list_process_runs(limit=20)
    health = _worker_health()
    return {
        "type": "pulse",
        "ts": time.time(),
        "live": live,
        "recent": recent,
        "workers": health["workers"],
        "stalled": health["stalled"],
    }


def _resolve_log_file(run_id: str) -> Path | None:
    """Bezpieczna ścieżka logu runu (tak jak REST /api/pipeline/runs/<id>/log)."""
    s = State()
    run = s.get_process_run(run_id)
    if not run:
        return None
    log_rel = run.get("log_path") or f".agents/logs/runs/{run_id}.log"
    f = (ROOT / log_rel).resolve()
    if not str(f).startswith(str(ROOT.resolve())):
        return None
    return f if f.is_file() else None


def _log_fallback_text(run_id: str) -> str:
    run = State().get_process_run(run_id) or {}
    return (run.get("stdout_tail") or "") + "\n--- stderr ---\n" + (run.get("stderr_tail") or "")


class WsGateway:
    """Serwer WebSocket (websockets.sync) z broadcast loopem — jeden wątek na klienta."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8767):
        self.host = host
        self.port = port
        self._clients: set = set()          # aktywne połączenia
        self._subs: dict = {}               # ws -> {run_id: offset_bytes}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._srv = None
        self._thread: threading.Thread | None = None

    # --- broadcast ---
    def _broadcaster(self) -> None:
        last_hash = None
        while not self._stop.is_set():
            try:
                payload = _pulse_payload()
                blob = json.dumps(payload, ensure_ascii=False, default=str)
                h = hash(blob)
                if h != last_hash:
                    last_hash = h
                    self._broadcast(blob)
                self._pump_logs()
            except Exception:
                pass
            self._stop.wait(PULSE_INTERVAL_S)

    def _broadcast(self, blob: str) -> None:
        with self._lock:
            clients = list(self._clients)
        dead = []
        for ws in clients:
            try:
                ws.send(blob)
            except Exception:
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                    self._subs.pop(ws, None)

    def _pump_logs(self) -> None:
        with self._lock:
            subs = {ws: dict(d) for ws, d in self._subs.items() if d}
        for ws, run_ids in subs.items():
            for run_id in list(run_ids):
                f = _resolve_log_file(run_id)
                if f is None:
                    continue
                try:
                    size = f.stat().st_size
                    offset = run_ids[run_id]
                    if size < offset:  # log obrócony/ucięty — od nowa
                        offset = 0
                    if size == offset:
                        continue
                    with open(f, "rb") as fh:
                        fh.seek(offset)
                        chunk = fh.read().decode("utf-8", errors="replace")
                    offset = size
                except Exception:
                    continue
                with self._lock:
                    if ws in self._subs and run_id in self._subs[ws]:
                        self._subs[ws][run_id] = offset
                try:
                    ws.send(json.dumps({
                        "type": "log", "run_id": run_id, "text": chunk, "offset": offset,
                    }, ensure_ascii=False))
                except Exception:
                    with self._lock:
                        self._clients.discard(ws)
                        self._subs.pop(ws, None)
                    break

    # --- connection handler (jeden wątek na klienta) ---
    def _handle(self, ws) -> None:
        with self._lock:
            self._clients.add(ws)
            self._subs[ws] = {}
        try:
            ws.send(json.dumps({"type": "hello", "ts": time.time(),
                                "pulse_interval_s": PULSE_INTERVAL_S}))
            for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mtype = msg.get("type")
                if mtype == "ping":
                    ws.send(json.dumps({"type": "pong", "ts": time.time()}))
                elif mtype == "subscribe_log":
                    run_id = str(msg.get("run_id") or "")
                    if not run_id:
                        continue
                    tail = int(msg.get("tail") or 400)
                    f = _resolve_log_file(run_id)
                    text = ""
                    offset = 0
                    if f is not None:
                        data = f.read_text(encoding="utf-8", errors="replace")
                        lines = data.splitlines()
                        text = "\n".join(lines[-tail:]) if tail > 0 else data
                        offset = f.stat().st_size
                    else:
                        text = _log_fallback_text(run_id)
                    with self._lock:
                        self._subs.setdefault(ws, {})[run_id] = offset
                    ws.send(json.dumps({
                        "type": "log", "run_id": run_id, "text": text, "offset": offset,
                        "complete": True,
                    }, ensure_ascii=False))
                elif mtype == "unsubscribe_log":
                    run_id = str(msg.get("run_id") or "")
                    with self._lock:
                        self._subs.get(ws, {}).pop(run_id, None)
        except Exception:
            pass
        finally:
            with self._lock:
                self._clients.discard(ws)
                self._subs.pop(ws, None)

    # --- lifecycle ---
    def start(self) -> int:
        from websockets.sync.server import serve
        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                self._srv = serve(self._handle, self.host, self.port)
                self._thread = threading.Thread(target=self._broadcaster, daemon=True)
                self._thread.start()
                threading.Thread(target=self._srv.serve_forever, daemon=True).start()
                return self.port
            except OSError as e:
                if e.errno == 98 and attempt < max_attempts - 1:
                    self.port += 1
                    continue
                raise
        raise OSError(f"Could not bind to any port after {max_attempts} attempts")

    def stop(self) -> None:
        self._stop.set()
        try:
            self._srv.shutdown()
        except Exception:
            pass

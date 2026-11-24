"""Testy jednostkowe i integracyjne dla Worker Console Observability & Health Watchdog.

Pokrycie:
1. Zapis i odczyt wpisów .jsonl z poprawnymi timestampami i polami cmd/data/rc/stream/step
2. Detekcja ACTIVE (< 600s) vs STALLED (>= 600s) oraz brak duplikacji powiadomień
3. Ochrona przed fałszywym alarmem, gdy aktywny jest job LLM (llm_busy)
4. Endpointy /api/task/<id>/console, /api/workers/health oraz /api/task/<id>/kill_and_retry
"""
import json
import os
import tempfile
import time
from pathlib import Path
import pytest

from engine import console_streamer as cs
from engine.state import State, RUNNING, COOLING, READY
from engine.orchestrator import Orchestrator
import engine.dashboard as dashboard_mod


@pytest.fixture
def tmp_state(tmp_path):
    """Tworzy czystą instancję State w katalogu tymczasowym."""
    db_file = tmp_path / "test_state.db"
    st = State(db_path=db_file)
    return st


@pytest.fixture
def isolated_console_dir(tmp_path, monkeypatch):
    """Izoluje katalog logów konsoli do katalogu tymczasowego."""
    cdir = tmp_path / "console"
    cdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cs, "CONSOLE_DIR", cdir)
    return cdir


def test_console_streamer_write_and_read(tmp_state, isolated_console_dir):
    """Weryfikuje poprawny zapis i odczyt wpisów .jsonl ze strukturą pól i timestampami."""
    tid = "TASK-TEST-001"
    streamer = cs.ConsoleStreamer(tid, state=tmp_state)

    t0 = time.time()
    streamer.log_meta("init", "start workera", rc=None)
    streamer.log_cmd("1/5_PLAN", "vibe --model fast-model")
    streamer.log_data("stdout", "1/5_PLAN", "Generowanie planu...\n")
    streamer.log_data("stderr", "1/5_PLAN", "Ostrzeżenie: deprecation warning\n")
    streamer.log_meta("1/5_PLAN", "exit", rc=0)
    streamer.close()

    assert cs.count_lines(tid) == 5
    la = cs.last_activity_at(tid)
    assert la >= t0 - 1.0

    # Odczyt ogólny
    events = cs.read_tail(tid, tail=10)
    assert len(events) == 5

    # Weryfikacja struktury pól pierwszego i kolejnych zdarzeń
    ev_meta = events[0]
    assert ev_meta["stream"] == "meta"
    assert ev_meta["step"] == "init"
    assert ev_meta["data"] == "start workera"
    assert "ts" in ev_meta and ev_meta["ts"] >= t0

    ev_cmd = events[1]
    assert ev_cmd["stream"] == "cmd"
    assert ev_cmd["step"] == "1/5_PLAN"
    assert ev_cmd["cmd"] == "vibe --model fast-model"

    ev_out = events[2]
    assert ev_out["stream"] == "stdout"
    assert "Generowanie planu" in ev_out["data"]

    ev_err = events[3]
    assert ev_err["stream"] == "stderr"
    assert "Ostrzeżenie" in ev_err["data"]

    ev_exit = events[4]
    assert ev_exit["stream"] == "meta"
    assert ev_exit["rc"] == 0

    # Filtrowanie po stream
    cmd_only = cs.read_tail(tid, stream_filter="cmd")
    assert len(cmd_only) == 1
    assert cmd_only[0]["cmd"] == "vibe --model fast-model"

    stderr_only = cs.read_tail(tid, stream_filter="stderr")
    assert len(stderr_only) == 1
    assert "Ostrzeżenie" in stderr_only[0]["data"]


def test_stalled_detection_threshold(isolated_console_dir):
    """Weryfikuje logikę detekcji ACTIVE (< 600s) vs STALLED (>= 600s)."""
    tid = "TASK-TEST-STALL"
    streamer = cs.ConsoleStreamer(tid)
    streamer.log_meta("init", "start")
    streamer.close()

    p = cs.console_path(tid)
    now = time.time()

    # 1. Świeży plik (< 600s) -> ACTIVE
    os.utime(p, (now - 120, now - 120))
    assert not cs.is_stalled(tid, threshold_s=600.0)

    # 2. Plik stary (650s) -> STALLED
    os.utime(p, (now - 650, now - 650))
    assert cs.is_stalled(tid, threshold_s=600.0)


def test_orchestrator_stall_watchdog_and_no_duplicate_notifications(tmp_state, isolated_console_dir, monkeypatch):
    """Weryfikuje działanie watchdog w orchestratorze:
    - wykrycie STALLED dla zadania RUNNING
    - brak duplikowania powiadomień w tej samej minucie ciszy
    - brak fałszywego alertu gdy zadanie ma aktywny job LLM (llm_busy)
    """
    tmp_state.add_milestone("M1", "Test Milestone", "branch-m1")
    tid1 = "TASK-STALL-ALERT"
    tid2 = "TASK-LLM-BUSY"
    tmp_state.add_task(tid1, "M1", 4, "Task 1", "developer", status=RUNNING)
    tmp_state.add_task(tid2, "M1", 4, "Task 2", "developer", status=RUNNING)

    now = time.time()
    # Task 1: plik konsoli stary (700s temu = ~11 min)
    s1 = cs.ConsoleStreamer(tid1)
    s1.log_meta("step", "stary log")
    s1.close()
    p1 = cs.console_path(tid1)
    os.utime(p1, (now - 700, now - 700))

    # Task 2: plik konsoli stary (700s temu), ALE ma aktywny job w llm_jobs (status RUNNING)
    s2 = cs.ConsoleStreamer(tid2)
    s2.log_meta("step", "stary log")
    s2.close()
    p2 = cs.console_path(tid2)
    os.utime(p2, (now - 700, now - 700))

    tmp_state._conn().execute(
        "INSERT INTO llm_jobs (id, kind, status, task_id, created_at) VALUES (?, 'complete', 'RUNNING', ?, ?)",
        ("llmj-test-1", tid2, now - 100)
    )

    orch = Orchestrator(poll=1)
    orch.state = tmp_state

    # 1. Pierwszy przebieg watchdoga
    orch._stall_watchdog()

    notifs = tmp_state.list_notifications()
    # Task 1 powinien dostać alert stall
    task1_notifs = [n for n in notifs if n.get("task_id") == tid1 and n.get("kind") == "stall"]
    assert len(task1_notifs) == 1
    assert "bez outputu na konsoli" in task1_notifs[0]["text"]

    # Task 2 nie powinien dostać alertu (bo llm_busy)
    task2_notifs = [n for n in notifs if n.get("task_id") == tid2 and n.get("kind") == "stall"]
    assert len(task2_notifs) == 0

    # 2. Drugi przebieg w tym samym przedziale minutowym - brak duplikacji alertu
    orch._stall_watchdog()
    notifs_after = tmp_state.list_notifications()
    task1_notifs_after = [n for n in notifs_after if n.get("task_id") == tid1 and n.get("kind") == "stall"]
    assert len(task1_notifs_after) == 1, "Watchdog nie powinien duplikować powiadomienia w tej samej minucie"


def test_dashboard_api_console_and_health(tmp_state, isolated_console_dir, monkeypatch):
    """Test integracyjny endpointów dashboardu:
    - GET /api/task/<id>/console
    - GET /api/workers/health
    - POST /api/task/<id>/kill_and_retry
    """
    monkeypatch.setattr(dashboard_mod, "State", lambda *a, **k: tmp_state)

    tid = "TASK-DASH-OBS"
    tmp_state.add_milestone("M1", "Test Milestone", "branch-m1")
    tmp_state.add_task(tid, "M1", 4, "Task Dash", "developer", status=RUNNING)

    streamer = cs.ConsoleStreamer(tid, state=tmp_state)
    streamer.log_cmd("2/5_IMPL", "python test.py")
    streamer.log_data("stdout", "2/5_IMPL", "All tests passed\n")
    streamer.log_meta("2/5_IMPL", "exit", rc=0)
    streamer.close()

    # Symulacja obsługi żądań przez Handler
    class FakeServer:
        server_address = ("127.0.0.1", 8765)

    class DummyHandler(dashboard_mod.Handler):
        def __init__(self, path, method="GET", body=b"{}"):
            self.path = path
            self.command = method
            self.headers = {"Host": "127.0.0.1:8765", "Content-Length": str(len(body))}
            self.server = FakeServer()
            import io
            self.rfile = io.BytesIO(body)
            self.wfile = io.BytesIO()
            self.status_code = None
            self.response_headers = {}
            self.response_body = b""

        def send_response(self, code):
            self.status_code = code

        def send_header(self, k, v):
            self.response_headers[k] = v

        def end_headers(self):
            pass

        def _send(self, code, body, content_type="text/plain"):
            self.status_code = code
            self.response_body = body
            self.response_headers["Content-Type"] = content_type

    # 1. Test GET /api/task/<id>/console
    h_console = DummyHandler(f"/api/task/{tid}/console?tail=50")
    h_console._do_GET()
    assert h_console.status_code == 200
    res_console = json.loads(h_console.response_body.decode("utf-8"))
    assert res_console["task_id"] == tid
    assert res_console["total_lines"] == 3
    assert len(res_console["events"]) == 3
    assert res_console["events"][0]["cmd"] == "python test.py"
    assert res_console["events"][1]["data"] == "All tests passed\n"
    assert res_console["events"][2]["rc"] == 0

    # 2. Test GET /api/workers/health
    h_health = DummyHandler("/api/workers/health")
    h_health._do_GET()
    assert h_health.status_code == 200
    res_health = json.loads(h_health.response_body.decode("utf-8"))
    assert "workers" in res_health
    workers = res_health["workers"]
    matching = [w for w in workers if w["task_id"] == tid]
    assert len(matching) == 1
    w = matching[0]
    assert w["status"] == RUNNING
    assert w["console_lines"] == 3
    assert not w["stall"]

    # 3. Test POST /api/task/<id>/kill_and_retry
    h_kill = DummyHandler(f"/api/task/{tid}/kill_and_retry", method="POST",
                          body=json.dumps({"reason": "test kill"}).encode())
    h_kill._do_POST()
    assert h_kill.status_code == 200
    res_kill = json.loads(h_kill.response_body.decode("utf-8"))
    assert res_kill["ok"] is True
    t_after = tmp_state.get_task(tid)
    assert t_after["status"] == COOLING
    assert "kill_and_retry" in (t_after.get("review_notes") or "")

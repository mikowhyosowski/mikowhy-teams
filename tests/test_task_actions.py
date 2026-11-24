"""Przyciski w szczegółach zadania: klik → status zmienia się TYLKO na właściwy.

Każdy test przechodzi przez prawdziwy handler HTTP dashboardu (POST /api/task/<id>/<akcja>)
na tymczasowej fabryce, więc sprawdza to, co realnie robi przycisk w UI: kod odpowiedzi,
nowy status zadania i to, czy postęp (substep) nie został skasowany.
"""
import json
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from engine import dashboard as D
from engine import orchestrator as O
from engine.flow import flow as F
from engine.state import State


@pytest.fixture()
def factory(monkeypatch):
    db = Path(tempfile.mkdtemp()) / ".agents" / "state.db"

    def _state(*_a, **_kw):
        return State(db_path=db)

    # handler + cmd_* tworzą State() same — podmieniamy fabrykę, żeby wszystko szło do tmp DB
    monkeypatch.setattr(D, "State", _state)
    monkeypatch.setattr(O, "State", _state)
    monkeypatch.setattr(F, "State", _state)

    s = State(db_path=db)
    s.add_milestone("MS-T", "goal", "branch", title="test")
    cases = {
        "T-OWNER": "AWAITING_OWNER",
        "T-RUN": "RUNNING",
        "T-READY": "READY",
        "T-BLOCKED": "BLOCKED",
        "T-MERGED": "MERGED",
        "T-REVIEW": "IN_REVIEW",
    }
    for tid, st in cases.items():
        s.add_task(tid, "MS-T", 4, f"{tid} title", "backend-dev", status=st)
    s.transition("T-OWNER", "AWAITING_OWNER", substep="4/5_QG", retry_count=1)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), D.Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield s, f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown()


def _post(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(),
                                 method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:  # 4xx to też odpowiedź — nie może wisieć bez treści
        return e.code, json.loads(e.read().decode() or "{}")


# --- ✅ Zaakceptuj decyzję ---------------------------------------------------

def test_approve_z_awaiting_owner_daje_ready_i_zachowuje_postep(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-OWNER/approve")
    assert code == 200 and j["ok"] is True and j["to"] == "READY"
    task = s.get_task("T-OWNER")
    assert task["status"] == "READY"
    assert task["substep"] == "4/5_QG", "akceptacja nie może kasować wypracowanego etapu"


def test_approve_spoza_awaiting_owner_nie_rusza_statusu(factory):
    s, base = factory
    for tid in ("T-RUN", "T-READY", "T-BLOCKED", "T-MERGED", "T-REVIEW"):
        before = s.get_task(tid)["status"]
        code, j = _post(base, f"/api/task/{tid}/approve")
        assert code == 409 and j["ok"] is False and "AWAITING_OWNER" in j["error"]
        assert s.get_task(tid)["status"] == before, f"approve z {before} nie może zmieniać statusu"


# --- 🛑 Odrzuć ---------------------------------------------------------------

def test_decline_daje_blocked_i_substep_owner_packet(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-OWNER/decline", {"reason": "poza zakresem"})
    assert code == 200 and j["to"] == "BLOCKED"
    task = s.get_task("T-OWNER")
    assert task["status"] == "BLOCKED" and task["substep"] == "OWNER_PACKET"


def test_decline_spoza_awaiting_owner_nie_rusza_statusu(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-RUN/decline", {"reason": "x"})
    assert code == 409 and j["ok"] is False
    assert s.get_task("T-RUN")["status"] == "RUNNING"


# --- ▶️ aktywacja / ⚡ boost -------------------------------------------------

def test_activate_wstawia_do_kolejki_z_running(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-RUN/activate")
    assert code == 200 and j["from"] == "RUNNING" and j["to"] == "READY"
    assert s.get_task("T-RUN")["status"] == "READY"


def test_activate_nie_rusza_merged_ani_ready(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-MERGED/activate")
    assert code == 409 and j["ok"] is False
    assert s.get_task("T-MERGED")["status"] == "MERGED"
    code, j = _post(base, "/api/task/T-READY/activate")
    assert code == 200 and j["to"] == "READY" and s.get_task("T-READY")["status"] == "READY"


def test_boost_daje_ready_i_p0_ale_nie_wskrzesza_merged(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-BLOCKED/boost")
    assert code == 200 and j["to"] == "READY"
    task = s.get_task("T-BLOCKED")
    assert task["status"] == "READY" and task["priority"] == "P0"
    code, j = _post(base, "/api/task/T-MERGED/boost")
    assert code == 409 and s.get_task("T-MERGED")["status"] == "MERGED"


# --- ⏹️ stop / 💬 komentarz — bez zmiany statusu -----------------------------

def test_stop_zostawia_running_do_granicy_etapu(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-RUN/stop")
    assert code == 200 and j["to"] == "RUNNING"
    assert s.get_task("T-RUN")["status"] == "RUNNING"
    assert s.stop_requested("T-RUN") is True


def test_comment_nie_zmienia_statusu(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-REVIEW/comment", {"text": "sprawdźcie testy"})
    assert code == 200
    assert s.get_task("T-REVIEW")["status"] == "IN_REVIEW"


# --- ♻️ force_retry / 🔄 kill_and_retry -------------------------------------

def test_force_retry_tylko_z_running(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-RUN/force_retry")
    assert code == 200 and j["to"] == "READY"
    assert s.get_task("T-RUN")["status"] == "READY"
    code, j = _post(base, "/api/task/T-BLOCKED/force_retry")
    assert code == 409 and j["ok"] is False
    assert s.get_task("T-BLOCKED")["status"] == "BLOCKED"


def test_kill_and_retry_usypia_bez_zmiany_statusu(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-RUN/kill_and_retry", {"reason": "test"})
    assert code == 200 and j["to"] == "RUNNING" and j["presence"] == "USPIONE"
    assert s.get_task("T-RUN")["status"] == "RUNNING"
    code, j = _post(base, "/api/task/T-MERGED/kill_and_retry", {"reason": "test"})
    assert code == 409 and s.get_task("T-MERGED")["status"] == "MERGED"


# --- 🌊 akcje z configu flow (przyciski w karcie i na diagramie) -------------

def test_flow_approve_zachowuje_substep(factory):
    s, base = factory
    code, j = _post(base, "/api/flow/action", {"task_id": "T-OWNER", "action": "approve"})
    assert code == 200 and j["ok"] is True and j["to"] == "READY"
    task = s.get_task("T-OWNER")
    assert task["status"] == "READY" and task["substep"] == "4/5_QG"


def test_flow_decline_ustawia_owner_packet(factory):
    s, base = factory
    code, j = _post(base, "/api/flow/action",
                    {"task_id": "T-OWNER", "action": "decline", "reason": "nie teraz"})
    assert code == 200 and j["to"] == "BLOCKED"
    task = s.get_task("T-OWNER")
    assert task["status"] == "BLOCKED" and task["substep"] == "OWNER_PACKET"


def test_flow_retry_odblokowuje_tylko_blocked(factory):
    s, base = factory
    code, j = _post(base, "/api/flow/action", {"task_id": "T-BLOCKED", "action": "retry"})
    assert code == 200 and j["to"] == "READY"
    assert s.get_task("T-BLOCKED")["status"] == "READY"
    code, j = _post(base, "/api/flow/action", {"task_id": "T-RUN", "action": "retry"})
    assert code == 400 and j["ok"] is False
    assert s.get_task("T-RUN")["status"] == "RUNNING"


# --- endpointy diagnostyczne wołane POST-em z UI -----------------------------

def test_kill_orphans_i_process_check_dzialaja_postem(factory):
    s, base = factory
    code, j = _post(base, "/api/task/T-RUN/kill-orphans")
    assert code == 200 and isinstance(j.get("killed"), list)
    code, j = _post(base, "/api/task/T-RUN/process-check")
    assert code == 200 and "process_check" in j
    assert s.get_task("T-RUN")["status"] == "RUNNING", "diagnostyka nie może zmieniać statusu"

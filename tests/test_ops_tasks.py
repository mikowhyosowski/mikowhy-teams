"""OPS: zadania pilne (LLM root-cause), rola workload-guardian (zatrudnienie/People Ops),
obowiązki cykliczne (dzienny audyt FAILED->READY, strażnik bezczynności) i ich egzekucja w orchestratorze."""
import tempfile
from pathlib import Path

from engine.state import State


def _tmp_state():
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    s.add_milestone("FACTORY-OPS", "Operacje i naprawy fabryki (mikowhy-teams)",
                    "milestone/factory-ops", title="FACTORY-OPS")
    return s


def test_workload_guardian_role_registered():
    """„Zatrudnienie”: rola ma wpis w rejestrze, skill i węzeł w org chart pod Sponsorem."""
    from engine import skill_compiler
    from engine.flow import org as O
    reg = skill_compiler.load_registry()
    assert "workload-guardian" in reg["roles"]
    assert reg["roles"]["workload-guardian"]["status"] == "ACTIVE"
    assert (Path(".agents/skills/roles/workload-guardian.md")).exists()
    cfg = O.load_config()
    assert O.validate_config(cfg) == []
    m = O.build_org({"tasks": []})
    g = next(n for n in m["nodes"] if n["id"] == "workload-guardian")
    assert g["role"] == "workload-guardian"
    assert g["reports_to"] == "sponsor"          # People Ops raportuje bezpośrednio Opiekunowi
    assert g["name"] and g["position"]            # persona przypisana deterministycznie


def test_ops_tasks_in_live_state():
    """Zadania P0/P1 są w kolejce READY z właściwymi rolami i priorytetami."""
    s = State()
    t1 = s.get_task("OPS-LLM-ROOTCAUSE")
    # zamknięte przez Sponsora (BLOCKED) albo w pracy — zależnie od decyzji; priorytet i rola stałe
    assert t1 and t1["status"] in ("READY", "RUNNING", "IN_REVIEW", "BLOCKED") and t1["priority"] == "P0"
    assert t1["role"] == "principal-debugger"
    t2 = s.get_task("OPS-GUARDIAN-SETUP")
    assert t2 and t2["status"] in ("READY", "RUNNING", "IN_REVIEW") and t2["priority"] == "P1"
    assert t2["role"] == "workload-guardian"


def test_recurring_registered():
    """Obowiązki cykliczne: dzienny audyt FAILED->READY (health-monitor) i strażnik bezczynności (workload-guardian)."""
    s = State()
    rec = {r["id"]: r for r in s.list_recurring(active_only=False)}
    a = rec["AUDIT-FAILED-2-READY"]
    assert a["role"] == "health-monitor" and a["schedule"] == "daily" and a["active"] == 1
    g = rec["GUARDIAN-IDLE-CHECK"]
    assert g["role"] == "workload-guardian" and g["schedule"] == "hourly" and g["active"] == 1


def test_launch_recurring_idempotent():
    """Orchestrator generuje zadanie z obowiązku cyklicznego tylko raz (max jedno otwarte na recurring)."""
    from engine.orchestrator import Orchestrator
    s = _tmp_state()
    s.add_recurring("TEST-REC", "health-monitor", "Testowy obowiązek", schedule="daily", priority="P2")
    o = Orchestrator.__new__(Orchestrator)  # bez locka — tylko self.state
    o.state = s
    o._launch_recurring()
    spawned = [t for t in s.list_tasks() if t["id"].startswith("REC-TEST-REC-")]
    assert len(spawned) == 1 and spawned[0]["status"] == "READY" and spawned[0]["role"] == "health-monitor"
    assert spawned[0]["priority"] == "P2"
    # drugie wywołanie w tym samym dniu nie dubluje zadania
    o._launch_recurring()
    spawned2 = [t for t in s.list_tasks() if t["id"].startswith("REC-TEST-REC-")]
    assert len(spawned2) == 1
    # last_run ustawione -> nie due od razu
    assert s.recurring_due() == [] or all(r["id"] != "TEST-REC" for r in s.recurring_due())


def test_escalate_stuck_tasks_phase():
    """Faza orchestratora: zadania utknięte (retry>=2) w READY/RUNNING/FAILED -> AWAITING_OWNER,
    nie przechodzą całego flow w kółko. BLOCKED/MERGED/AWAITING_OWNER nie są ruszane."""
    from engine.orchestrator import Orchestrator, STUCK_RETRY
    from engine.state import AWAITING_OWNER
    s = _tmp_state()
    s.add_task("T-STUCK", "FACTORY-OPS", 9, "utknęło", "backend-dev", status="READY")
    s._conn().execute("UPDATE tasks SET retry_count=? WHERE id=?", (STUCK_RETRY, "T-STUCK"))
    s.add_task("T-OK", "FACTORY-OPS", 9, "jedna próba", "backend-dev", status="READY")  # retry=0
    s.add_task("T-BLK", "FACTORY-OPS", 9, "już zamknięte", "backend-dev", status="BLOCKED")
    s._conn().execute("UPDATE tasks SET retry_count=5 WHERE id='T-BLK'")
    o = Orchestrator.__new__(Orchestrator)
    o.state = s
    o._escalate_stuck_tasks()
    assert s.get_task("T-STUCK")["status"] == AWAITING_OWNER
    assert s.get_task("T-STUCK")["substep"] == "OWNER_PACKET"
    assert "AWAITING_OWNER" in (s.get_task("T-STUCK")["review_notes"] or "")
    assert s.get_task("T-OK")["status"] == "READY"          # retry=0 — nie ruszamy
    assert s.get_task("T-BLK")["status"] == "BLOCKED"        # zamknięte — nie ruszamy
    # READY -> AWAITING_OWNER dozwolone w modelu
    from engine.state import VALID_TRANSITIONS
    assert "AWAITING_OWNER" in VALID_TRANSITIONS["READY"]

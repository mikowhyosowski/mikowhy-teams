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
    assert t2 and t2["status"] in ("READY", "RUNNING", "IN_REVIEW", "AWAITING_OWNER", "BLOCKED") and t2["priority"] == "P1"
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


def test_scope_autoextend_autoheal():
    """Zasada „Opiekunowi łatwiej”: plik spoza scope_paths nie eskaluje do Opiekuna —
    worker sam poszerza scope (limit, sekrety blokowane)."""
    from engine.worker_lifecycle import _scope_autoextend, SCOPE_AUTOEXTEND_MAX
    s = _tmp_state()
    s.add_task("T-SCOPE", "FACTORY-OPS", 9, "test scope", "backend-dev", scope_paths=["engine/"])
    t = s.get_task("T-SCOPE")
    assert _scope_autoextend(s, t, "docs/nowy.md") is True
    assert "docs/nowy.md" in s.get_task("T-SCOPE")["scope_paths"]
    # sekrety nigdy automatycznie
    assert _scope_autoextend(s, s.get_task("T-SCOPE"), ".env") is False
    assert _scope_autoextend(s, s.get_task("T-SCOPE"), "keys/api_key.pem") is False
    # limit poszerzeń
    for i in range(SCOPE_AUTOEXTEND_MAX + 2):
        ok = _scope_autoextend(s, s.get_task("T-SCOPE"), f"docs/p{i}.md")
    assert ok is False
    # już w scope -> True bez zmiany
    assert _scope_autoextend(s, s.get_task("T-SCOPE"), "engine/") is True


def test_owner_packet_has_error_history():
    """Pakiet OWNER_PACKET zawiera ostatnie błędy z metryk — Opiekun widzi co automat próbował."""
    from engine.orchestrator import Orchestrator, STUCK_RETRY
    from engine.state import AWAITING_OWNER
    s = _tmp_state()
    s.add_task("T-PKT", "FACTORY-OPS", 9, "pakiet", "backend-dev", status="READY")
    s._conn().execute("UPDATE tasks SET retry_count=? WHERE id=?", (STUCK_RETRY, "T-PKT"))
    s.record_metric("agent_comment", {"author": "worker", "text": "❌ próba nieudana (PermissionError: plik spoza scope)"}, "T-PKT")
    o = Orchestrator.__new__(Orchestrator)
    o.state = s
    o._escalate_stuck_tasks()
    t = s.get_task("T-PKT")
    assert t["status"] == AWAITING_OWNER
    assert "PermissionError" in (t["review_notes"] or "")  # historia błędów w pakiecie


def test_substep_progress():
    """Postęp zadania z substepu: 2/5_IMPL -> 0.4, done -> 1.0, LLM_EXEC -> 0.5, ASK_OWNER -> 0."""
    from engine.state import substep_progress as p
    assert p("1/5_PLAN") == 0.2
    assert p("2/5_IMPL") == 0.4
    assert p("4/5_QG") == 0.8
    assert p("4.5_SIMPLIFY") == 0.9
    assert p("5/5_COMMIT") == 1.0
    assert p("4b_FIX") == 0.8
    assert p("done") == 1.0
    assert p("LLM_EXEC") == 0.5
    assert p("ASK_OWNER") == 0.0 and p(None) == 0.0 and p("") == 0.0
    # progress leci też w API (wiersz taska ma pole progress)
    from engine.state import State
    s = State(db_path=Path(tempfile.mkdtemp()) / "t.db")
    s.add_milestone("MS", "g", "b", title="t")
    s.add_task("T-P", "MS", 4, "t", "backend-dev", status="RUNNING")
    s.transition("T-P", "RUNNING", substep="2/5_IMPL")
    assert s.get_task("T-P")["progress"] == 0.4


def test_comments_have_cel_efekt_format():
    """Każdy komentarz ma format 🎯 Cel → ✅ Efekt (czytelny dla człowieka)."""
    s = _tmp_state()
    s.add_task("T-C", "FACTORY-OPS", 9, "komentarze", "backend-dev", status="READY")
    # zwykły add_comment bez formatu -> auto-opakowanie
    s.add_comment("T-C", "worker(x)", "napisałem testy")
    # jawny raport
    s.add_comment_report("T-C", "worker(x)", "zaimplementować X", "zapisano 3 pliki")
    # transition generuje komentarz Cel/Efekt
    s.transition("T-C", "RUNNING", substep="1/5_PLAN")
    rows = s._conn().execute(
        "SELECT payload FROM metrics WHERE kind='agent_comment' AND task_id='T-C' ORDER BY ts").fetchall()
    import json as _json
    texts = [_json.loads(r[0])["text"] for r in rows]
    assert len(texts) == 3
    for t in texts:
        assert "Cel:" in t, f"brak Cel w: {t[:80]}"
        # „🔄 W TRAKCIE” = komunikat w trakcie (Cel bez Efektu — efekt dopiero będzie);
        # zakończone komunikaty muszą mieć oba pola
        if not t.startswith("🔄"):
            assert "Efekt:" in t, f"brak formatu Cel/Efekt w: {t[:80]}"
    assert "napisałem testy" in texts[0]  # oryginalny tekst zachowany w Efekt
    assert texts[2].startswith("🔄 W TRAKCIE"), texts[2]  # start etapu = w trakcie, z celem etapu


def test_resume_substep_on_return_to_ready():
    """v2.7.9: powrót do READY NIE resetuje sub-etapu — kontynuacja od wypracowanego."""
    from engine.state import resume_substep
    s = _tmp_state()
    s.add_task("T-R", "FACTORY-OPS", 9, "resume", "backend-dev", status="READY")
    s.claim_task("T-R", "w1")
    s.transition("T-R", "RUNNING", substep="3/5_TEST")
    s.transition("T-R", "FAILED", review_notes="timeout")
    s.transition("T-R", "READY", retry_count=1)  # bez substep -> NIE resetuje
    t = s.get_task("T-R")
    assert t["status"] == "READY"
    assert t["substep"] == "3/5_TEST", t["substep"]
    assert resume_substep(t) == "3/5_TEST"
    # markery specjalne -> 1/5_PLAN, done -> 2/5_IMPL
    assert resume_substep({"substep": "OWNER_PACKET"}) == "1/5_PLAN"
    assert resume_substep({"substep": "done"}) == "2/5_IMPL"
    assert resume_substep({"substep": None}) == "1/5_PLAN"
    # komentarz przy FAILED->READY mówi o kontynuacji
    import json as _json
    texts = [_json.loads(r[0])["text"] for r in s._conn().execute(
        "SELECT payload FROM metrics WHERE kind='agent_comment' AND task_id='T-R' ORDER BY ts").fetchall()]
    assert any("kontynuacja od etapu 3/5_TEST" in t for t in texts), texts


def test_comment_dedup_no_duplicates():
    """v2.7.9: identyczny komentarz (task, author, text) w ciągu 15s = duplikat -> jeden wpis."""
    s = _tmp_state()
    s.add_task("T-D", "FACTORY-OPS", 9, "dedup", "backend-dev", status="READY")
    s.add_comment("T-D", "worker(x)", "🔄 W TRAKCIE — etap 1/5_PLAN: 🎯 Cel: plan — nad tym pracuję")
    s.add_comment("T-D", "worker(x)", "🔄 W TRAKCIE — etap 1/5_PLAN: 🎯 Cel: plan — nad tym pracuję")
    s.add_comment("T-D", "worker(x)", "inny tekst")
    rows = s._conn().execute(
        "SELECT payload FROM metrics WHERE kind='agent_comment' AND task_id='T-D'").fetchall()
    import json as _json
    texts = [_json.loads(r[0])["text"] for r in rows]
    assert texts.count("🔄 W TRAKCIE — etap 1/5_PLAN: 🎯 Cel: plan — nad tym pracuję") == 1, texts
    assert len(texts) == 2

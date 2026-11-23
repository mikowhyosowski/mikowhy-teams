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
    """v2.7.14: zadania utknięte (retry>=2) NIE idą do AWAITING_OWNER — zostają w READY na końcu
    kolejki (z dopisaną notatką), BLOCKED/MERGED/AWAITING_OWNER/RUNNING nie są ruszane."""
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
    t = s.get_task("T-STUCK")
    assert t["status"] == "READY", t["status"]               # NIE AWAITING_OWNER
    assert "koniec kolejki" in (t["review_notes"] or "")
    assert t["substep"] != "OWNER_PACKET"
    assert s.get_task("T-OK")["status"] == "READY"           # retry=0 — nie ruszamy
    assert s.get_task("T-BLK")["status"] == "BLOCKED"        # zamknięte — nie ruszamy
    # AWAITING_OWNER nadal dozwolone w modelu (tylko ręcznie / ASK_OWNER workera)
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


def test_stuck_requeued_with_error_history_not_owner():
    """v2.7.14: utknięte zadanie po retry dostaje historię błędów w review_notes, ale zostaje w READY
    (koniec kolejki) — NIE AWAITING_OWNER."""
    from engine.orchestrator import Orchestrator, STUCK_RETRY
    s = _tmp_state()
    s.add_task("T-PKT", "FACTORY-OPS", 9, "pakiet", "backend-dev", status="READY")
    s._conn().execute("UPDATE tasks SET retry_count=? WHERE id=?", (STUCK_RETRY, "T-PKT"))
    s.record_metric("agent_comment", {"author": "worker", "text": "❌ próba nieudana (PermissionError: plik spoza scope)"}, "T-PKT")
    o = Orchestrator.__new__(Orchestrator)
    o.state = s
    o._escalate_stuck_tasks()
    t = s.get_task("T-PKT")
    assert t["status"] == "READY"
    assert "koniec kolejki" in (t["review_notes"] or "")
    assert "PermissionError" in (t["review_notes"] or "")  # historia błędów nadal w notatce


def test_queue_sort_priority_progress_retry_age():
    """v2.7.14: kolejność claimable_tasks = priorytet → (3+ retry na koniec pasma) → wyższy postęp → najstarsze."""
    import time as _t
    s = _tmp_state()
    now = _t.time()
    s.add_task("Q-P2-OLD", "FACTORY-OPS", 9, "stare P2", "backend-dev", status="READY", priority="P2")
    s.add_task("Q-P0", "FACTORY-OPS", 9, "P0 świeże", "backend-dev", status="READY", priority="P0")
    s.add_task("Q-P2-PROG", "FACTORY-OPS", 9, "P2 dalej w postępie", "backend-dev", status="READY", priority="P2")
    s.add_task("Q-P2-RETRY3", "FACTORY-OPS", 9, "P2 po 3 retry", "backend-dev", status="READY", priority="P2")
    s._conn().execute("UPDATE tasks SET substep='4/5_QG' WHERE id='Q-P2-PROG'")       # progress 0.8
    s._conn().execute("UPDATE tasks SET retry_count=3 WHERE id='Q-P2-RETRY3'")         # koniec kolejki
    s._conn().execute("UPDATE tasks SET created_at=? WHERE id='Q-P2-OLD'", (now - 500,))
    s._conn().execute("UPDATE tasks SET created_at=? WHERE id='Q-P2-PROG'", (now - 100,))
    s._conn().execute("UPDATE tasks SET created_at=? WHERE id='Q-P2-RETRY3'", (now - 50,))
    ids = [t["id"] for t in s.claimable_tasks()]
    assert ids[0] == "Q-P0"                 # priorytet rządzi
    assert ids.index("Q-P2-PROG") < ids.index("Q-P2-OLD")   # wyższy postęp przed starszym
    assert ids[-1] == "Q-P2-RETRY3"         # 3+ retry na koniec (mimo najnowszego)
    assert ids == ["Q-P0", "Q-P2-PROG", "Q-P2-OLD", "Q-P2-RETRY3"]


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


def test_attachments_add_autodetect_and_dedup():
    """v2.7.10: załączniki zadania — add_attachment + auto-wykrywanie ścieżek w komentarzu + dedup."""
    from engine.state import ROOT
    s = _tmp_state()
    s.add_task("T-A", "FACTORY-OPS", 9, "atts", "backend-dev", status="READY")
    p = ROOT / ".agents" / "runtime" / "test-att-xyz.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# rezultat testowy\n", encoding="utf-8")
    try:
        rel = ".agents/runtime/test-att-xyz.md"
        a1 = s.add_attachment("T-A", rel, "📋 testowy rezultat", "md")
        assert a1 and a1["path"] == rel
        a2 = s.add_attachment("T-A", rel, "inny label")  # dedup po (task, path)
        assert a2["id"] == a1["id"]
        assert len(s.list_attachments("T-A")) == 1
        # auto-wykrywanie ścieżki w komentarzu
        s.add_comment("T-A", "worker(x)", "🎯 Cel: zapisać plan → ✅ Efekt: plan w .agents/runtime/test-att-xyz.md gotowy")
        atts = s.list_attachments("T-A")
        assert len(atts) == 1 and atts[0]["path"] == rel
        # ścieżka spoza ROOT odrzucona
        assert s.add_attachment("T-A", "../../etc/passwd") is None
        assert s.get_attachment(a1["id"])["path"] == rel
    finally:
        p.unlink(missing_ok=True)


def test_flow_merge_action():
    """v2.7.10: akcja flow 'merge' (🚀 Wdróż) — flaga sponsor_merge + komentarz, bez bezpośredniego przejścia."""
    from engine.flow import flow as flowmod
    s = _tmp_state()
    s.add_task("T-M", "FACTORY-OPS", 9, "merge", "backend-dev", status="READY")
    s.claim_task("T-M", "w1")
    s.transition("T-M", "RUNNING")
    s.transition("T-M", "IN_REVIEW", substep="done")
    s.transition("T-M", "READY_TO_MERGE")
    cfg = flowmod.load_config()
    assert not flowmod.validate_config(cfg), flowmod.validate_config(cfg)
    assert "merge" in [a for st in cfg["stages"] if st["id"] == "merge" for a in st.get("actions", [])]
    res = flowmod.apply_action("T-M", "merge", {}, state=s)
    assert res["ok"] and res["to"] == "READY_TO_MERGE"  # orchestrator zrobi realny merge
    assert s.get_kv("sponsor_merge:T-M")
    import json as _json
    texts = [_json.loads(r[0])["text"] for r in s._conn().execute(
        "SELECT payload FROM metrics WHERE kind='agent_comment' AND task_id='T-M'").fetchall()]
    assert any(t.startswith("🚀") for t in texts)
    # merge niedozwolony z RUNNING
    s.transition("T-M", "READY")
    try:
        flowmod.apply_action("T-M", "merge", {}, state=s)
        assert False, "merge z READY powinien rzucić"
    except ValueError:
        pass


def test_substep_since_timer():
    """v2.7.11: zmiana sub-etapu ustawia substep_since (live timer na kafelku 'teraz: …')."""
    s = _tmp_state()
    s.add_task("T-S", "FACTORY-OPS", 9, "timer", "backend-dev", status="READY")
    s.claim_task("T-S", "w1")
    s.transition("T-S", "RUNNING", substep="1/5_PLAN")
    t1 = s.get_task("T-S")
    assert t1["substep_since"] > 0
    import time as _t
    _t.sleep(0.05)
    s.transition("T-S", "RUNNING", substep="2/5_IMPL")
    t2 = s.get_task("T-S")
    assert t2["substep"] == "2/5_IMPL" and t2["substep_since"] >= t1["substep_since"]
    # ten sam substep drugi raz -> bez zmiany czasu
    s.transition("T-S", "RUNNING", substep="2/5_IMPL")
    assert s.get_task("T-S")["substep_since"] == t2["substep_since"]


def test_role_skills_history():
    """v2.7.11: szkolenie — add_role_skill ląduje w historii roli (ts, source), lista sortowana od najnowszego."""
    s = _tmp_state()
    a = s.add_role_skill("backend-dev", "testy E2E", "Playwright: smoki + pełne flow", source="sponsor/dashboard")
    b = s.add_role_skill("backend-dev", "SQL tuning", "indeksy, EXPLAIN", source="sponsor")
    lst = s.list_role_skills("backend-dev")
    assert [x["name"] for x in lst] == [b["name"], a["name"]]  # najnowszy pierwszy
    assert lst[0]["ts"] >= lst[1]["ts"]
    import json as _json
    rows = s._conn().execute("SELECT payload FROM metrics WHERE kind='skill_granted'").fetchall()
    assert len(rows) == 2 and _json.loads(rows[0][0])["role"] == "backend-dev"


def test_substep_since_on_claim_even_if_same_substep():
    """v2.7.11: przejęcie do RUNNING (nawet przy kontynuacji tego samego sub-etapu) startuje timer."""
    s = _tmp_state()
    s.add_task("T-S2", "FACTORY-OPS", 9, "timer2", "backend-dev", status="READY")
    s.claim_task("T-S2", "w1")
    s.transition("T-S2", "RUNNING", substep="2/5_IMPL")   # claim -> RUNNING: timer start
    t1 = s.get_task("T-S2")
    assert t1["substep_since"] > 0
    s.transition("T-S2", "READY")                            # wraca do kolejki (substep zostaje)
    s.claim_task("T-S2", "w1")
    s.transition("T-S2", "RUNNING", substep="2/5_IMPL")      # kontynuacja tego samego etapu: timer też startuje
    t2 = s.get_task("T-S2")
    assert t2["substep_since"] >= t1["substep_since"]


def test_task_detail_context_actions_per_status():
    """v2.7.12: szczegóły zadania mają available_actions tylko dla bieżącego statusu (kontekstowe buttony)."""
    from engine.dashboard import _task_detail
    s = _tmp_state()
    s.add_task("T-CA", "FACTORY-OPS", 9, "ctx", "backend-dev", status="READY")
    d = _task_detail(s, "T-CA")
    assert d["stage"] == "queue" and "set_priority" in d["available_actions"] and "approve" not in d["available_actions"]
    s.claim_task("T-CA", "w1")
    s.transition("T-CA", "RUNNING")
    s.transition("T-CA", "IN_REVIEW", substep="done")
    s.transition("T-CA", "READY_TO_MERGE")
    d = _task_detail(s, "T-CA")
    assert d["stage"] == "merge" and "merge" in d["available_actions"], d["available_actions"]
    s2 = _tmp_state()
    s2.add_task("T-CB", "FACTORY-OPS", 9, "ctx2", "backend-dev", status="READY")
    s2.claim_task("T-CB", "w1")
    s2.transition("T-CB", "RUNNING")
    s2.transition("T-CB", "AWAITING_OWNER", substep="OWNER_PACKET", review_notes="x")
    d2 = _task_detail(s2, "T-CB")
    assert d2["stage"] == "owner" and set(d2["available_actions"]) >= {"approve", "decline"}


def test_notifications_dedup_and_unread():
    """v2.7.15: powiadomienia systemowe — dedup w 600s, unread count, mark read."""
    s = _tmp_state()
    a = s.add_notification("queue", "🔚 T-X po 3 próbach → koniec kolejki", "T-X")
    b = s.add_notification("queue", "🔚 T-X po 3 próbach → koniec kolejki", "T-X")  # duplikat w oknie
    assert a and b is None
    s.add_notification("merge", "✅ T-Y zmergowane", "T-Y")
    assert s.unread_notifications() == 2
    lst = s.list_notifications(10)
    assert lst[0]["kind"] in ("merge", "queue") and "ts" in lst[0]
    n = s.mark_notifications_read()
    assert n == 2 and s.unread_notifications() == 0


def test_snapshot_has_queue_logs_notifications():
    """v2.7.15: /api/state (snapshot) zawiera queue (kolejność pobierania), applogs i notifications."""
    from engine.dashboard import _snapshot
    s = _tmp_state()
    s.add_task("T-Q", "FACTORY-OPS", 9, "w kolejce", "backend-dev", status="READY", priority="P1")
    s.add_notification("info", "test powiadomienia")
    d = _snapshot(s)
    assert any(q["id"] == "T-Q" for q in d["queue"])
    assert d["queue"][0]["pos"] == 1
    assert isinstance(d["applogs"], list) and isinstance(d["notifications"], list)
    assert d["notif_unread"] >= 1


def test_resume_uses_furthest_max_substep():
    """v2.7.16: przy powrocie do READY wznawiamy od NAJDALEJ wypracowanego etapu (max_substep),
    nawet gdy bieżący substep cofnął się do tyłu."""
    from engine.state import resume_substep
    s = _tmp_state()
    s.add_task("T-MX", "FACTORY-OPS", 9, "max", "backend-dev", status="READY")
    s.claim_task("T-MX", "w1")
    s.transition("T-MX", "RUNNING", substep="1/5_PLAN")
    s.transition("T-MX", "RUNNING", substep="4/5_QG")     # najdalej
    assert s.get_task("T-MX")["max_substep"] == "4/5_QG"
    s.transition("T-MX", "RUNNING", substep="2/5_IMPL")    # cofnięcie — max zostaje
    assert s.get_task("T-MX")["max_substep"] == "4/5_QG"
    s.transition("T-MX", "FAILED", review_notes="błąd")
    s.transition("T-MX", "READY", retry_count=1)
    t = s.get_task("T-MX")
    assert t["status"] == "READY" and t["substep"] == "2/5_IMPL"   # bieżący został cofnięty
    assert resume_substep(t) == "4/5_QG"                          # ale wznawiamy od najdalej
    import json as _json
    texts = [_json.loads(r[0])["text"] for r in s._conn().execute(
        "SELECT payload FROM metrics WHERE kind='agent_comment' AND task_id='T-MX'").fetchall()]
    assert any("kontynuacja od etapu 4/5_QG" in x for x in texts), texts


def test_failed_parked_after_max_retries_for_team_audit():
    """v2.7.17: FAILED po max_retries zostaje ZAPARKOWANE w FAILED (czeka na audyt AUDIT-FAILED-ROOTCAUSE),
    nie wraca samo do READY ani do Opiekuna; próby 1..2 idą do READY (koniec kolejki)."""
    from engine.orchestrator import Orchestrator, CIRCUIT_BREAKER_RETRIES
    from engine.state import FAILED, READY
    s = _tmp_state()
    s.add_task("T-F1", "FACTORY-OPS", 9, "jedna próba", "backend-dev", status="FAILED")
    s._conn().execute("UPDATE tasks SET retry_count=1 WHERE id='T-F1'")
    s.add_task("T-F3", "FACTORY-OPS", 9, "po 3 próbach", "backend-dev", status="FAILED")
    s._conn().execute("UPDATE tasks SET retry_count=? WHERE id='T-F3'", (CIRCUIT_BREAKER_RETRIES,))
    o = Orchestrator.__new__(Orchestrator)
    o.state = s
    o._auto_retry_failed()
    assert s.get_task("T-F1")["status"] == READY and s.get_task("T-F1")["retry_count"] == 2  # auto-retry dalej
    t3 = s.get_task("T-F3")
    assert t3["status"] == FAILED, t3["status"]                                          # zaparkowane
    assert "ZAPARKOWANE" in (t3["review_notes"] or "")
    # obowiązek cykliczny audytu FAILED — seedowany przez _ensure_ops_recurring dla systems-auditor
    o._ensure_ops_recurring()
    rec = [r for r in s.list_recurring(active_only=False) if r["id"] == "AUDIT-FAILED-ROOTCAUSE"]
    assert rec and rec[0]["role"] == "systems-auditor" and rec[0]["schedule"] == "daily"


def test_eta_estimate_and_snapshot():
    """v2.7.19: ETA zadania — heurystyka przy add_task, set_eta z planu, snapshot ma eta_left/eta_at dla RUNNING."""
    from engine.state import estimate_eta_s
    s = _tmp_state()
    s.add_task("T-E1", "FACTORY-OPS", 9, "małe", "backend-dev", status="READY", scope_paths=["engine/x.py"])
    s.add_task("T-E2", "FACTORY-OPS", 9, "read_only raport", "health-monitor", status="READY", read_only=True, mode="llm")
    assert s.get_task("T-E1")["eta_s"] == estimate_eta_s("małe", ["engine/x.py"], False, "pipeline", "P2")
    assert s.get_task("T-E1")["eta_s"] > 0 and s.get_task("T-E2")["eta_s"] < s.get_task("T-E1")["eta_s"]
    s.set_eta("T-E1", 1234)
    assert s.get_task("T-E1")["eta_s"] == 1234
    s.claim_task("T-E1", "w1")
    s.transition("T-E1", "RUNNING", substep="2/5_IMPL")
    from engine.dashboard import _snapshot
    d = _snapshot(s)
    t = [x for x in d["tasks"] if x["id"] == "T-E1"][0]
    assert t["eta_s"] == 1234 and t.get("eta_left_s") is not None and t.get("eta_at")
    assert d["stats"]["eta_queue_s"] >= 0
    assert any(q["id"] == "T-E2" and "eta_s" in q for q in d["queue"])

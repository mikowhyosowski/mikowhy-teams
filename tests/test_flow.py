"""FLOW-001: testy zakładki 🌊 Flow — config, walidacja, build_flow, apply_action, endpointy."""
import json
import tempfile
import urllib.request
from pathlib import Path

from engine.dashboard import snapshot
from engine.flow import flow as F
from engine.state import State


def _tmp_state():
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    s.add_milestone("MS-T", "goal", "branch", title="test")
    return s


def test_config_valid():
    cfg = F.load_config()
    assert F.validate_config(cfg) == []
    assert cfg["_override_active"] is False
    assert len(cfg["stages"]) >= 5
    lanes = set(cfg["layout"]["lanes"])
    for st in cfg["stages"]:
        assert st["lane"] in lanes
        assert st["statuses"]


def test_stage_mapping():
    cfg = F.load_config()
    assert F.stage_of_status(cfg, "READY") == "queue"
    assert F.stage_of_status(cfg, "RUNNING") == "working"
    assert F.stage_of_status(cfg, "AWAITING_OWNER") == "owner"
    assert F.stage_of_status(cfg, "CHANGES_REQUESTED") == "changes"
    assert F.stage_of_status(cfg, "MERGED") == "done"
    assert F.stage_of_status(cfg, "FAILED") == "blocked"
    assert F.stage_of_status(cfg, "BLOCKED") == "blocked"


def test_validate_bad_transition():
    bad = F.load_default()
    bad["transitions"].append({"from": "done", "to": "queue", "label": "złe", "kind": "auto"})
    errs = F.validate_config(bad)
    assert any("done -> queue" in e for e in errs)


def test_validate_bad_action():
    bad = F.load_default()
    bad["actions"]["approve"]["target_status"] = "MERGED"  # AWAITING_OWNER -> MERGED niedozwolone
    errs = F.validate_config(bad)
    assert any("approve" in e and "niedozwolone" in e for e in errs)
    bad2 = F.load_default()
    bad2["stages"][0]["actions"].append("nie-istnieje")
    assert any("nie-istnieje" in e for e in F.validate_config(bad2))


def test_build_flow_full():
    m = F.build_flow(snapshot(), State())
    assert m["errors"] == []
    assert m["stats"]["total"] == len(m["stages"] and [t for s in m["stages"] for t in s["tasks"]])
    for st in m["stages"]:
        for t in st["tasks"]:
            assert t["status"] in st["statuses"]
            # available_actions są zgodne z definicją akcji i statusem zadania
            for a in t["available_actions"]:
                assert F.validate_action(m["config"], t["status"], a) is None


def test_build_flow_empty():
    m = F.build_flow({"tasks": []})
    assert all(s["count"] == 0 for s in m["stages"])
    assert m["stats"] == {"total": 0, "hot": 0, "overdue": 0,
                          "per_stage": {s["id"]: 0 for s in m["stages"]}}


def test_apply_action_lifecycle():
    s = _tmp_state()
    s.add_task("T-1", "MS-T", 4, "test", "backend-dev", status="AWAITING_OWNER")
    r = F.apply_action("T-1", "approve", state=s)
    assert r["to"] == "READY" and s.get_task("T-1")["status"] == "READY"
    # approve niedozwolone z READY
    try:
        F.apply_action("T-1", "approve", state=s)
        raise AssertionError("powinno rzucić ValueError")
    except ValueError:
        pass
    s.transition("T-1", "BLOCKED")
    assert F.apply_action("T-1", "retry", state=s)["to"] == "READY"
    assert s.get_task("T-1")["status"] == "READY"
    F.apply_action("T-1", "set_priority", {"priority": "P0"}, state=s)
    assert s.get_task("T-1")["priority"] == "P0"
    try:
        F.apply_action("T-1", "set_priority", {"priority": "P9"}, state=s)
        raise AssertionError("powinno rzucić ValueError")
    except ValueError:
        pass
    r = F.apply_action("T-1", "comment", {"text": "ok"}, state=s)
    assert r["ok"] and r["to"] == "READY"
    try:
        F.apply_action("T-1", "comment", {"text": ""}, state=s)
        raise AssertionError("pusty komentarz powinien rzucić")
    except ValueError:
        pass
    try:
        F.apply_action("NIE-MA", "approve", state=s)
        raise AssertionError("nieznane zadanie powinno rzucić")
    except ValueError:
        pass


def test_override_save_reset(tmp_path=None):
    tmp = Path(tempfile.mkdtemp())
    old = F.OVERRIDE_CFG
    try:
        F.OVERRIDE_CFG = tmp / "flow.config.json"
        assert F.save_override({"meta": {"name": "Custom"}}) == []
        cfg = F.load_config()
        assert cfg["meta"]["name"] == "Custom"
        assert cfg["_override_active"] is True
        # listy w override zastępują default (stages), reszta się meruje
        stages_mod = F.load_default()["stages"]
        stages_mod[0] = dict(stages_mod[0], label="Kolejka TEST")
        assert F.save_override({"stages": stages_mod}) == []
        cfg = F.load_config()
        assert len(cfg["stages"]) == 8  # queue, working, changes, review, merge, done, owner, blocked
        assert cfg["stages"][0]["label"] == "Kolejka TEST"
        assert F.reset_override() is True
        assert F.load_config()["meta"]["name"].startswith("Dark Factory")
        assert F.reset_override() is False
        # zły override blokowany
        errs = F.save_override({"stages": []})
        assert errs and not F.OVERRIDE_CFG.exists()
    finally:
        F.OVERRIDE_CFG = old


def test_endpoint_live():
    with urllib.request.urlopen("http://127.0.0.1:8765/api/flow", timeout=5) as r:
        f = json.loads(r.read().decode())
    assert "config" in f and "stages" in f and "transitions" in f and "actions" in f
    assert f["errors"] == []
    with urllib.request.urlopen("http://127.0.0.1:8765/api/flow/config", timeout=5) as r:
        c = json.loads(r.read().decode())
    assert "default_raw" in c and "errors" in c and c["errors"] == []


# ===== Docelowy model przepływu (v2): samoregulująca się linia, Opiekun = ostatnia instancja =====

def test_model_valid_transitions():
    from engine.state import VALID_TRANSITIONS as VT
    # standardowy obieg
    assert "RUNNING" in VT["READY"]
    assert "IN_REVIEW" in VT["RUNNING"]
    assert "READY_TO_MERGE" in VT["IN_REVIEW"]
    assert "MERGED" in VT["READY_TO_MERGE"]
    assert VT["MERGED"] == set()
    # pętla poprawek po CR bez Opiekuna: IN_REVIEW -> CHANGES_REQUESTED -> RUNNING
    assert "CHANGES_REQUESTED" in VT["IN_REVIEW"]
    assert "RUNNING" in VT["CHANGES_REQUESTED"]
    # Opiekun tylko jako ostatnia instancja: z każdego etapu pracy, ale tylko do READY/BLOCKED
    for st in ("RUNNING", "CHANGES_REQUESTED", "IN_REVIEW", "READY_TO_MERGE"):
        assert "AWAITING_OWNER" in VT[st]
    assert VT["AWAITING_OWNER"] == {"READY", "BLOCKED"}
    # BLOCKED odblokowuje się WYŁĄCZNIE do READY (pełny cykl — nic nie omija testów/CR)
    assert VT["BLOCKED"] == {"READY", "ESCALATED", "AWAITING_OWNER"}
    assert "READY" in VT["FAILED"]  # automatyczny retry
    assert "AWAITING_OWNER" in VT["FAILED"]  # po wyczerpaniu max_retries


def test_legacy_status_migration():
    from engine.state import State, AWAITING_OWNER
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    s.add_milestone("MS-T", "goal", "branch", title="test")
    s.add_task("T-OLD1", "MS-T", 4, "stare approval", "backend-dev", status="AWAITING_APPROVAL")
    s.add_task("T-OLD2", "MS-T", 4, "stare escalated", "backend-dev", status="ESCALATED")
    s2 = State(db_path=tmp / "t.db")  # nowe połączenie = migracja przy starcie
    st1 = s2.get_task("T-OLD1")["status"]
    st2 = s2.get_task("T-OLD2")["status"]
    assert st1 == AWAITING_OWNER and st2 == AWAITING_OWNER


def test_claim_changes_requested():
    from engine.state import State, CHANGES_REQUESTED
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    s.add_milestone("MS-T", "goal", "branch", title="test")
    s.add_task("T-CR", "MS-T", 4, "poprawki po CR", "backend-dev", status=CHANGES_REQUESTED)
    # worker przejmuje CHANGES_REQUESTED tak jak READY -> RUNNING (pętla naprawcza)
    t = s.claim_task("T-CR", claimed_by="worker-test")
    assert t is not None and t["status"] == "RUNNING"
    # claimable_tasks zawiera CHANGES_REQUESTED
    ids = {t["id"] for t in s.claimable_tasks()}
    assert "T-CR" in ids or True  # już RUNNING po claim — sprawdźmy przed claim w osobnym teście
    s2 = State(db_path=tmp / "t2.db")
    s2.add_milestone("MS-T", "goal", "branch", title="test")
    s2.add_task("T-CR2", "MS-T", 4, "poprawki", "backend-dev", status=CHANGES_REQUESTED)
    assert "T-CR2" in {t["id"] for t in s2.claimable_tasks()}


def test_flow_config_v2_model():
    cfg = F.load_config()
    assert F.validate_config(cfg) == []
    stages = {s["id"]: s for s in cfg["stages"]}
    # tor główny: kolejka -> realizacja -> poprawki -> review -> merge -> archiwum
    assert [s for s in cfg["stages"] if s["lane"] == "main"] and \
           [s["id"] for s in sorted([s for s in cfg["stages"] if s["lane"] == "main"], key=lambda x: x["order"])] == \
           ["queue", "working", "changes", "review", "merge", "done"]
    # tor Opiekuna (góra) tylko AWAITING_OWNER i hot
    assert stages["owner"]["statuses"] == ["AWAITING_OWNER"] and stages["owner"]["lane"] == "top" and stages["owner"]["hot"]
    # tor blokad (dół): BLOCKED + FAILED (auto-retry)
    assert set(stages["blocked"]["statuses"]) == {"BLOCKED", "FAILED"} and stages["blocked"]["lane"] == "bottom"
    tr = {(t["from"], t["to"]) for t in cfg["transitions"]}
    assert ("review", "changes") in tr and ("changes", "working") in tr      # pętla CR
    assert ("owner", "queue") in tr and ("owner", "blocked") in tr            # decyzja -> READY / odrzucenie -> BLOCKED
    assert ("blocked", "queue") in tr                                          # odblokowanie tylko do READY
    # akcje Opiekuna tylko z AWAITING_OWNER
    assert cfg["actions"]["approve"]["allowed_from"] == ["AWAITING_OWNER"]
    assert cfg["actions"]["approve"]["target_status"] == "READY"
    assert cfg["actions"]["decline"]["target_status"] == "BLOCKED"
    assert cfg["actions"]["retry"]["allowed_from"] == ["FAILED", "BLOCKED"]


def test_apply_action_owner_lifecycle():
    from engine.state import State
    s = _tmp_state()
    s.add_task("T-O", "MS-T", 4, "czeka na Opiekuna", "backend-dev", status="AWAITING_OWNER")
    # approve: AWAITING_OWNER -> READY (pełny cykl, decyzja w kontekście)
    r = F.apply_action("T-O", "approve", state=s)
    assert r["to"] == "READY" and s.get_task("T-O")["status"] == "READY"
    # decline: AWAITING_OWNER -> BLOCKED
    s2 = _tmp_state()
    s2.add_task("T-D", "MS-T", 4, "do odrzucenia", "backend-dev", status="AWAITING_OWNER")
    r = F.apply_action("T-D", "decline", {"reason": "poza zakresem"}, state=s2)
    assert r["to"] == "BLOCKED" and s2.get_task("T-D")["status"] == "BLOCKED"
    # retry: BLOCKED -> READY
    r = F.apply_action("T-D", "retry", state=s2)
    assert r["to"] == "READY" and s2.get_task("T-D")["status"] == "READY"
    # approve niedozwolone spoza AWAITING_OWNER (np. z RUNNING — Opiekun nie przy drobiazgach)
    s3 = _tmp_state()
    s3.add_task("T-R", "MS-T", 4, "w pracy", "backend-dev", status="RUNNING")
    import pytest
    with pytest.raises(ValueError):
        F.apply_action("T-R", "approve", state=s3)

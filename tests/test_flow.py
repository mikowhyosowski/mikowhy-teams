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
    assert F.stage_of_status(cfg, "AWAITING_APPROVAL") == "approval"
    assert F.stage_of_status(cfg, "MERGED") == "done"
    assert F.stage_of_status(cfg, "FAILED") == "exceptions"


def test_validate_bad_transition():
    bad = F.load_default()
    bad["transitions"].append({"from": "done", "to": "queue", "label": "złe", "kind": "auto"})
    errs = F.validate_config(bad)
    assert any("done -> queue" in e for e in errs)


def test_validate_bad_action():
    bad = F.load_default()
    bad["actions"]["approve"]["target_status"] = "MERGED"  # AWAITING_APPROVAL -> MERGED niedozwolone
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
    s.add_task("T-1", "MS-T", 4, "test", "backend-dev", status="AWAITING_APPROVAL")
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
        assert len(cfg["stages"]) == 7
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

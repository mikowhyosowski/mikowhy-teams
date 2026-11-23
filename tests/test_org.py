"""ORG-001: testy pod-zakładki 🌊 Flow → 🏗️ Flow stanowisk — config hierarchii, walidacja, build_org, endpointy."""
import json
import tempfile
import urllib.request
from pathlib import Path

from engine.dashboard import snapshot
from engine.flow import org as O
from engine.state import State


def test_config_valid():
    cfg = O.load_config()
    assert O.validate_config(cfg) == []
    assert cfg["_override_active"] is False
    nodes = cfg["nodes"]
    assert len(nodes) >= 12
    # dokładnie jeden root (reports_to=null) — Sponsor
    roots = [n for n in nodes if n.get("reports_to") is None]
    assert len(roots) == 1 and roots[0]["kind"] == "human"
    # każdy węzeł ma szefa poza rootem
    ids = {n["id"] for n in nodes}
    for n in nodes:
        if n["id"] != roots[0]["id"]:
            assert n["reports_to"] in ids


def test_hierarchy_shape():
    m = O.build_org({"tasks": []})
    assert m["errors"] == []
    by_id = {n["id"]: n for n in m["nodes"]}
    # kto jest czyim szefem — zgodnie z default_org.json
    assert by_id["tech-lead-planner"]["reports_to"] == "sponsor"
    assert by_id["backend-dev"]["reports_to"] == "solutions-architect"
    assert by_id["solutions-architect"]["reports_to"] == "tech-lead-planner"
    assert by_id["health-monitor"]["reports_to"] == "sre-deployer"
    assert by_id["security-auditor"]["reports_to"] == "sponsor"
    assert by_id["ideation-challenger"]["reports_to"] == "product-owner"
    # reports (podwładni) są odwrotnością reports_to
    assert "backend-dev" in by_id["solutions-architect"]["reports"]
    assert "sponsor" in by_id["sponsor"]["reports"] or by_id["sponsor"]["depth"] == 0
    assert set(by_id["sponsor"]["reports"]) == {"tech-lead-planner", "product-owner", "security-auditor", "workload-guardian"}
    # głębokości: sponsor=0, tech-lead=1, architekt=2, dev=3, health-monitor=4
    assert by_id["sponsor"]["depth"] == 0
    assert by_id["tech-lead-planner"]["depth"] == 1
    assert by_id["solutions-architect"]["depth"] == 2
    assert by_id["backend-dev"]["depth"] == 3
    assert by_id["health-monitor"]["depth"] == 4
    # każda rola agenta ma personę (imię + stanowisko)
    for n in m["nodes"]:
        if n["kind"] == "agent":
            assert n["persona"] and n["name"] and n["position"]


def test_validate_cycle():
    bad = O.load_default()
    for n in bad["nodes"]:
        if n["id"] == "backend-dev":
            n["reports_to"] = "health-monitor"  # cykl: backend -> health -> sre -> arch -> tech-lead ... -> backend? nie, ale health-monitor pod sre, a sre pod architekt — zróbmy prawdziwy cykl:
    # prawdziwy cykl: sponsor -> tech-lead-planner i tech-lead-planner -> sponsor
    for n in bad["nodes"]:
        if n["id"] == "tech-lead-planner":
            n["reports_to"] = "sponsor"
        if n["id"] == "sponsor":
            n["reports_to"] = "tech-lead-planner"
    errs = O.validate_config(bad)
    assert errs and any("cykl" in e or "root" in e for e in errs)


def test_validate_two_roots_and_unknown_role():
    bad = O.load_default()
    for n in bad["nodes"]:
        if n["id"] == "product-owner":
            n["reports_to"] = None  # drugi root
    errs = O.validate_config(bad)
    assert any("wiele rootów" in e for e in errs)
    bad2 = O.load_default()
    for n in bad2["nodes"]:
        if n["id"] == "backend-dev":
            n["role"] = "nie-istnieje-794531"
    errs2 = O.validate_config(bad2)
    assert any("nieznana rola" in e for e in errs2)


def test_build_org_with_tasks():
    s = State(db_path=Path(tempfile.mkdtemp()) / "t.db")
    s.add_milestone("MS", "goal", "branch", title="test")
    s.add_task("T-1", "MS", 4, "test zadanie", "backend-dev", status="RUNNING")
    snap = {"tasks": [{"id": "T-1", "status": "RUNNING", "role": "backend-dev", "title": "test zadanie",
                       "priority": "P1", "project_id": "mikowhy-teams", "age": 5}]}
    m = O.build_org(snap, s)
    by_id = {n["id"]: n for n in m["nodes"]}
    dev = by_id["backend-dev"]
    assert dev["busy"] is True
    assert dev["current"]["id"] == "T-1"
    assert dev["tasks"] == 1 and dev["by_status"] == {"RUNNING": 1}
    assert m["stats"]["busy"] == 1
    # sponsor (human) nie ma persony ani zadań
    sp = by_id["sponsor"]
    assert sp["kind"] == "human" and sp["persona"] is None and sp["tasks"] == 0


def test_override_save_reset():
    tmp = Path(tempfile.mkdtemp())
    old = O.OVERRIDE_CFG
    try:
        O.OVERRIDE_CFG = tmp / "org.config.json"
        assert O.save_override({"meta": {"name": "Custom Org"}}) == []
        cfg = O.load_config()
        assert cfg["meta"]["name"] == "Custom Org"
        assert cfg["_override_active"] is True
        # zmiana szefa: backend-dev pod tech-lead-planner
        nodes = O.load_default()["nodes"]
        for n in nodes:
            if n["id"] == "backend-dev":
                n["reports_to"] = "tech-lead-planner"
        assert O.save_override({"nodes": nodes}) == []
        m = O.build_org({"tasks": []})
        assert m["errors"] == []
        by_id = {n["id"]: n for n in m["nodes"]}
        assert by_id["backend-dev"]["reports_to"] == "tech-lead-planner"
        assert by_id["backend-dev"]["depth"] == 2
        assert O.reset_override() is True
        assert O.load_config()["meta"]["name"].startswith("Dark Factory")
        # zły override (cykl) blokowany
        bad_nodes = O.load_default()["nodes"]
        for n in bad_nodes:
            if n["id"] == "sponsor":
                n["reports_to"] = "backend-dev"
        errs = O.save_override({"nodes": bad_nodes})
        assert errs and not O.OVERRIDE_CFG.exists()
    finally:
        O.OVERRIDE_CFG = old


def test_endpoint_live():
    with urllib.request.urlopen("http://127.0.0.1:8765/api/org", timeout=5) as r:
        o = json.loads(r.read().decode())
    assert "config" in o and "nodes" in o and "stats" in o
    assert o["errors"] == []
    roots = [n for n in o["nodes"] if n["reports_to"] is None]
    assert len(roots) == 1
    with urllib.request.urlopen("http://127.0.0.1:8765/api/org/config", timeout=5) as r:
        c = json.loads(r.read().decode())
    assert "default_raw" in c and "errors" in c and c["errors"] == []

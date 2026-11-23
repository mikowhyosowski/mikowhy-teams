"""FIRMA-006: testy widoku Firma — config, build_world, endpoint /api/firma."""
import json
import urllib.request

from engine.dashboard import snapshot
from engine.firma.view import ZONE, build_world, load_config, validate_config
from engine.state import State


def test_config_valid():
    cfg = load_config()
    assert validate_config(cfg) == []
    assert "statusStyle" in cfg and "zones" in cfg
    for status, s in cfg["statusStyle"].items():
        assert s["zone"] in cfg["zones"] or s["zone"] == "desk"


def test_zone_mapping():
    assert ZONE["READY"] == "tray"
    assert ZONE["RUNNING"] == "desk"
    assert ZONE["MERGED"] == "archive"
    assert ZONE["ESCALATED"] == "alarm"
    assert ZONE["AWAITING_APPROVAL"] == "sign"


def test_build_world_full():
    w = build_world(snapshot(), State())
    assert w["config"]["version"] == 1
    pids = {o["project_id"] for o in w["offices"]}
    assert "mikowhy-teams" in pids  # projekt self = biuro na osiedlu
    for o in w["offices"]:
        assert isinstance(o["rooms"], list) and isinstance(o["desks"], list)
        for d in o["desks"]:
            assert d["zone"] in ("tray", "desk", "sign", "review", "outbox", "archive", "alarm", "locked")
    assert w["estate"]["host"]["port"] == 8765


def test_build_world_empty():
    w = build_world({"tasks": [], "projects": [], "team": [], "host": None, "sys": None}, None)
    assert w["offices"] == []
    assert w["estate"]["host"] is None


def test_endpoint_live():
    with urllib.request.urlopen("http://127.0.0.1:8765/api/firma", timeout=5) as r:
        w = json.loads(r.read().decode())
    assert "config" in w and "offices" in w and "estate" in w

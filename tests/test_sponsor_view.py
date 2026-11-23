"""v2.5: widok opiekuna (🎯 Tematy) — /api/sponsor: milestone'y z postępem, decyzje z akcjami, backlog, nowy temat."""
import json
import tempfile
import urllib.request
from pathlib import Path

from engine.dashboard import _sponsor_view
from engine.state import State


def test_sponsor_view_structure():
    d = _sponsor_view(State())
    assert "milestones" in d and "decisions" in d and "approvals" in d and "backlog" in d
    assert "actions_def" in d
    for m in d["milestones"]:
        assert "per_status" in m and "progress" in m and "blockers" in m and "tasks" in m
        assert 0.0 <= m["progress"] <= 1.0
        assert sum(m["per_status"].values()) == m["tasks_total"]
        for b in m["blockers"]:
            assert b["status"] in ("BLOCKED", "ESCALATED", "FAILED", "AWAITING_APPROVAL")
    for t in d["decisions"]:
        assert t["status"] in ("AWAITING_APPROVAL", "ESCALATED")
        assert isinstance(t["available_actions"], list)
    assert all(t["status"] == "READY" for t in d["backlog"])


def test_sponsor_view_tmp_db_decision_actions():
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    s.add_milestone("MS-X", "cel testowy", "feat/x", title="Temat X", project_id="mikowhy-teams")
    s.add_task("T-A", "MS-X", 4, "tech tytul", "backend-dev", status="AWAITING_APPROVAL",
               title_sponsor="Po ludzku: o co chodzi")
    s.add_task("T-B", "MS-X", 4, "zadanie gotowe", "worker", status="READY", priority="P0")
    d = _sponsor_view(s)
    ms = next(m for m in d["milestones"] if m["id"] == "MS-X")
    assert ms["tasks_total"] == 2 and ms["per_status"] == {"AWAITING_APPROVAL": 1, "READY": 1}
    assert ms["progress"] == 0.0 and ms["blockers"][0]["id"] == "T-A"
    dec = next(t for t in d["decisions"] if t["id"] == "T-A")
    assert dec["title_sponsor"] == "Po ludzku: o co chodzi"
    assert "approve" in dec["available_actions"] and "decline" in dec["available_actions"]
    assert d["backlog"][0]["id"] == "T-B" and d["backlog"][0]["priority"] == "P0"


def test_endpoint_live():
    with urllib.request.urlopen("http://127.0.0.1:8765/api/sponsor", timeout=5) as r:
        d = json.loads(r.read().decode())
    assert d["milestones"] and d["decisions"] is not None and "actions_def" in d
    # POST /api/milestone bez wymaganych pól -> 400
    req = urllib.request.Request("http://127.0.0.1:8765/api/milestone", data=b"{}", method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError("pusty POST powinien dać 400")
    except urllib.error.HTTPError as e:
        assert e.code == 400

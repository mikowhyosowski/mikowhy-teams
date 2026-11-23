"""v2.2: testy — kolory/ikony person, staż, by_status, merges z git log, recurring, firma (rooms/conversations/agregacja)."""
import json
import tempfile
import urllib.request
from pathlib import Path

from engine import personas
from engine.dashboard import _git_merges, _merges_info, snapshot
from engine.firma.view import build_world
from engine.state import State


def test_persona_color_icon():
    p1 = personas.persona_for_role("backend-dev")
    p2 = personas.persona_for_role("frontend-dev")
    assert p1["color"].startswith("hsl(") and p1["icon"]
    assert personas.persona_for_role("backend-dev")["color"] == p1["color"]  # deterministyczne
    colors = {personas.persona_for_role(r)["color"] for r in
              ("backend-dev", "frontend-dev", "sre-deployer", "code-reviewer", "worker", "llm-executor")}
    assert len(colors) >= 4  # różne osoby mają różne kolory
    icons = {personas.persona_full(r)["avatar"] for r in
             ("backend-dev", "frontend-dev", "sre-deployer", "code-reviewer", "worker", "llm-executor", "qa")}
    assert len(icons) >= 4  # różne charaktery


def test_team_by_status_and_tenure_sort():
    d = snapshot()
    team = d["team"]
    assert team, "team nie może być pusty"
    for m in team:
        assert "by_status" in m and isinstance(m["by_status"], dict)
        assert sum(m["by_status"].values()) == m["tasks"]
        assert "color" in m and "icon" in m
        assert "tenure_s" in m and "work_s" in m
    tenures = [m.get("tenure_s") or 0 for m in team]
    assert tenures == sorted(tenures, reverse=True), "team posortowany wg stażu (najstarsi pierwsi)"


def test_merges_include_git_log():
    s = State()
    info = _merges_info(s)
    git_m = [m for m in info["recent"] if m.get("source") == "git"]
    # w repo są direct merge'y sponsora ("sponsor: direct merge") — muszą być w liczniku
    assert git_m, "git log --merges powinien zasilić licznik merge'ów"
    assert info["count_24h"] >= len(git_m)
    assert _git_merges(limit=5)


def test_recurring_crud_tmp_db():
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    rid = s.add_recurring("REC-T1", "health-monitor", "przegląd health", schedule="daily", priority="P3")
    assert rid == "REC-T1"
    lst = s.list_recurring(role="health-monitor")
    assert len(lst) == 1 and lst[0]["priority"] == "P3"
    assert s.recurring_due()  # last_run None -> due
    s.set_recurring_active("REC-T1", False)
    assert s.list_recurring(role="health-monitor") == []
    assert s.list_recurring(role="health-monitor", active_only=False)[0]["active"] == 0
    try:
        s.add_recurring("REC-X", "worker", "x", schedule="monthly")
        raise AssertionError("zły schedule powinien rzucić")
    except Exception:
        pass


def test_firma_rooms_persona_and_conversations():
    s = State()
    # świeży wątek -> conversation na mapie
    from engine import team_threads
    th = team_threads.ensure_thread(s, "frontend-dev", "test conv")
    team_threads.add_message(s, th["id"], "sponsor", "świeża wiadomość", kind="msg")
    w = build_world(snapshot(), s)
    rooms = [r for o in w["offices"] for r in o["rooms"]]
    assert rooms
    for r in rooms:
        assert r["color"].startswith("hsl(") and r["avatar"] and "tenure_s" in r and "by_status" in r
    tenures = [r["tenure_s"] for o in w["offices"] for r in o["rooms"]]
    # per biuro pokoje sortowane wg stażu
    for o in w["offices"]:
        ts = [r["tenure_s"] for r in o["rooms"]]
        assert ts == sorted(ts, reverse=True)
    convs = w["conversations"]
    assert any(c["a_role"] == "frontend-dev" and c["b_role"] == "sponsor" for c in convs)
    # desks mają kolor osoby
    desks = [d for o in w["offices"] for d in o["desks"]]
    assert all(d.get("persona_color", "").startswith("hsl(") for d in desks)


def test_new_hires_detection():
    """Nowa rola ze świeżym zadaniem -> new_hires; stara rola / powtórzenie -> puste."""
    import time as _t
    from engine.dashboard import _snapshot
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    s.add_milestone("MS-N", "goal", "b", title="t")
    now = _t.time()
    # świeża rola (nowe zadanie)
    s.add_task("T-NEW", "MS-N", 4, "nowe", "brand-new-role", status="READY")
    # stara rola (zadanie sprzed 3h)
    s.add_task("T-OLD", "MS-N", 4, "stare", "old-role", status="READY")
    s._conn().execute("UPDATE tasks SET created_at=?, updated_at=? WHERE id='T-OLD'", (now - 3 * 3600, now - 3 * 3600))
    d = _snapshot(s)
    roles = [h["role"] for h in d["new_hires"]]
    assert "brand-new-role" in roles, f"świeża rola powinna być new_hire, jest {roles}"
    assert "old-role" not in roles, "stara rola nie może być new_hire"
    h = next(x for x in d["new_hires"] if x["role"] == "brand-new-role")
    assert h["name"] and h["color"] and h["avatar"]
    # drugie wywołanie — już widziana -> pusto
    d2 = _snapshot(s)
    assert d2["new_hires"] == []


def test_endpoints_live():
    with urllib.request.urlopen("http://127.0.0.1:8765/api/recurring?role=health-monitor", timeout=5) as r:
        lst = json.loads(r.read().decode())
    assert isinstance(lst, list) and all(x["priority"] in ("P2", "P3") for x in lst)  # audyt FAILED->READY może być P2
    with urllib.request.urlopen("http://127.0.0.1:8765/api/firma", timeout=5) as r:
        w = json.loads(r.read().decode())
    assert "conversations" in w
    assert w["config"]["colors"]["bg"] == "#f3ead8"  # ciepła płaska paleta
    assert w["config"]["stage2"]["gradients"] is False
    with urllib.request.urlopen("http://127.0.0.1:8765/api/state", timeout=5) as r:
        d = json.loads(r.read().decode())
    assert any(m.get("source") == "git" for m in d["merges"]["recent"]), "licznik merge'ów z git log"

"""Testy Workload Guardian (v2.7.26): hourly obchód — bezczynne role dostają zadanie-plan."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from engine.state import State, READY, RUNNING
from engine import workload_guardian as WG


@pytest.fixture()
def st(tmp_path: Path) -> State:
    s = State(db_path=tmp_path / "t.db")
    s.add_milestone("FACTORY-OPS", "ops", "milestone/factory-ops", title="FACTORY-OPS")
    return s


def _task(s: State, tid: str, role: str, status: str, age_s: float = 0):
    s.add_task(tid, "FACTORY-OPS", 9, f"t {tid}", role, status=status)
    if age_s:
        s._conn().execute("UPDATE tasks SET updated_at=? WHERE id=?", (time.time() - age_s, tid))
        s._conn().commit()


def test_busy_role_is_not_idle(st: State):
    now = time.time()
    _task(st, "T-1", "backend-dev", RUNNING, age_s=0)
    idles = WG.find_idle_roles(st, idle_s=1800, now=now)
    assert all(i["role"] != "backend-dev" for i in idles)


def test_role_without_any_task_is_idle(st: State):
    now = time.time()
    idles = WG.find_idle_roles(st, idle_s=1800, now=now)
    roles = {i["role"] for i in idles}
    assert "workload-guardian" in roles  # rola bez żadnego zadania = bezczynna od zawsze
    for i in idles:
        assert i["idle_s"] is None and i["duties"]


def test_old_finished_task_is_idle_after_30min(st: State):
    now = time.time()
    _task(st, "T-2", "sre-deployer", "MERGED", age_s=3600)  # ostatnia aktywność godzinę temu
    idles = WG.find_idle_roles(st, idle_s=1800, now=now)
    assert any(i["role"] == "sre-deployer" for i in idles)


def test_recently_finished_role_is_not_idle(st: State):
    now = time.time()
    _task(st, "T-3", "sre-deployer", "MERGED", age_s=60)  # skończył minutę temu
    idles = WG.find_idle_roles(st, idle_s=1800, now=now)
    assert all(i["role"] != "sre-deployer" for i in idles)


def test_run_creates_plan_task_and_notification(st: State):
    now = time.time()
    rep = WG.run_idle_check(st, now=now)
    assert rep["created"], "powinno utworzyć zadania-plan dla bezczynnych ról"
    tid = f"IDLE-BACKEND-DEV-{time.strftime('%Y%m%d', time.localtime(now))}"
    t = st.get_task(tid)
    assert t and t["status"] == READY and t["role"] == "backend-dev"
    assert "Obowiązki stanowiska" in (t["acceptance_criteria"] or "")
    notifs = st.list_notifications()
    assert any("Workload Guardian" in n["text"] for n in notifs)


def test_run_is_idempotent_within_a_day(st: State):
    now = time.time()
    r1 = WG.run_idle_check(st, now=now)
    r2 = WG.run_idle_check(st, now=now + 60)
    assert r1["created"] and r2["created"] == []
    # zadanie OPEN => dedup też działa przy innym wywołaniu tego samego dnia
    st.transition(f"IDLE-BACKEND-DEV-{time.strftime('%Y%m%d', time.localtime(now))}", RUNNING)
    r3 = WG.run_idle_check(st, now=now + 120)
    assert f"IDLE-BACKEND-DEV-{time.strftime('%Y%m%d', time.localtime(now))}" not in r3["created"]

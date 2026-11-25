"""Testy kontraktowe adapterów z mikowhy-task-mcp."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from engine.ports import TaskSourcePort
from engine.state import InvalidTransition


def make_sqlite_adapter():
    from mikowhy_task_mcp.adapters.sqlite import SqliteTaskAdapter
    tmp = tempfile.mkdtemp()
    return SqliteTaskAdapter(db_path=Path(tmp) / "state.db")


def make_memory_adapter():
    from mikowhy_task_mcp.adapters.memory import InMemoryTaskAdapter
    return InMemoryTaskAdapter()


@pytest.fixture(params=["sqlite", "memory"], ids=["sqlite", "memory"])
def tasks(request):
    if request.param == "sqlite":
        return make_sqlite_adapter()
    return make_memory_adapter()


def _create(adapter, title="Tytuł", role="backend-dev"):
    if hasattr(adapter, "state"):  # sqlite
        adapter.state.add_milestone("TEST-MS", "test", "test-branch")
        adapter.state.add_task("T-1", "TEST-MS", 9, title, role, status="READY")
        return adapter.state.get_task("T-1")
    return adapter.create_task(title, role=role, id="T-1")


def test_contract_create_get_list(tasks):
    assert isinstance(tasks, TaskSourcePort)
    t = _create(tasks)
    got = tasks.get_task(t["id"])
    assert got and got["status"] == "READY"
    assert any(x["id"] == t["id"] for x in tasks.list_tasks())
    assert any(x["id"] == t["id"] for x in tasks.list_tasks(status="READY"))
    assert not any(x["id"] == t["id"] for x in tasks.list_tasks(status="RUNNING"))


def test_contract_claim_and_forward_transition(tasks):
    t = _create(tasks)
    claimed = tasks.claim_next_task(role="backend-dev", worker_id="w-1")
    assert claimed and claimed["status"] == "RUNNING"
    tasks.transition_status(t["id"], "IN_REVIEW")
    assert tasks.get_task(t["id"])["status"] == "IN_REVIEW"


def test_contract_regression_is_blocked_for_every_adapter(tasks):
    t = _create(tasks)
    tasks.claim_next_task(role="backend-dev", worker_id="w-1")
    tasks.transition_status(t["id"], "IN_REVIEW")
    with pytest.raises(InvalidTransition):
        tasks.transition_status(t["id"], "READY")  # nigdy samo się nie cofa
    # jawna regresja (Sponsor) przechodzi
    tasks.transition_status(t["id"], "READY", allow_regression=True)
    assert tasks.get_task(t["id"])["status"] == "READY"


def test_contract_comment_artifact_subtasks(tasks):
    t = _create(tasks)
    tasks.append_comment(t["id"], "tester", "hello")
    if hasattr(tasks, "state"):  # sqlite
        from engine.state import ROOT
        f = ROOT / ".agents" / "tmp-contract-artifact.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("test", encoding="utf-8")
        try:
            att = tasks.attach_artifact(t["id"], str(f.relative_to(ROOT)), label="x", kind="md")
        finally:
            f.unlink(missing_ok=True)
    else:
        att = tasks.attach_artifact(t["id"], "docs/x.md", label="x", kind="md")
    assert att is not None
    subs = tasks.create_subtasks(t["id"], [{"title": "podzadanie", "role": "backend-dev"}])
    assert len(subs) == 1
"""Testy kontraktowe portów (Ports & Adapters).

Ta sama suita kontraktu leci przeciwko WIELU implementacjom TaskSourcePort
(SqliteTaskAdapter na tempowej bazie i InMemoryTaskAdapter) — dowód, że podmiana
adaptera nie zmienia zachowania silnika. Plus kontrakt kontenera DI.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from engine.container import AppContainer, get_container, set_container
from engine.ports import (
    ExecutionRunnerPort,
    HumanLoopNotifierPort,
    ReasoningEnginePort,
    TaskSourcePort,
    VcsWorkspacePort,
)
from engine.state import InvalidTransition
from engine.container import _discover_entry_points


def make_task_source(kind: str):
    # Ensure entry points are discovered
    _discover_entry_points()
    from engine import adapters as adapters_registry
    
    if kind == "sqlite":
        tmp = tempfile.mkdtemp()
        factory = adapters_registry.get_adapter_factory("task", "sqlite")
        return factory(db_path=Path(tmp) / "state.db")
    if kind == "memory":
        factory = adapters_registry.get_adapter_factory("task", "memory")
        return factory()


def _create(adapter, title="Tytuł", role="backend-dev"):
    if hasattr(adapter, "state"):  # sqlite
        adapter.state.add_milestone("TEST-MS", "test", "test-branch")
        adapter.state.add_task("T-1", "TEST-MS", 9, title, role, status="READY")
        return adapter.state.get_task("T-1")
    return adapter.create_task(title, role=role, id="T-1")


@pytest.fixture(params=["sqlite", "memory"], ids=["sqlite", "memory"])
def tasks(request):
    return make_task_source(request.param)


# --- kontrakt TaskSourcePort: ten sam scenariusz dla każdej implementacji -----

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
    if hasattr(tasks, "state"):  # sqlite: add_attachment wymaga realnego pliku w ROOT
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


# --- kontrakt pozostałych portów (domyślne adaptery) --------------------------

def test_default_adapters_satisfy_protocols():
    c = AppContainer.from_env()
    assert isinstance(c.task_source, TaskSourcePort)
    assert isinstance(c.llm, ReasoningEnginePort)
    assert isinstance(c.runner, ExecutionRunnerPort)
    assert isinstance(c.vcs, VcsWorkspacePort)
    assert isinstance(c.notifier, HumanLoopNotifierPort)


def test_container_env_override(monkeypatch):
    monkeypatch.setenv("TASK_PROVIDER", "memory")
    monkeypatch.setenv("LLM_PROVIDER", "stub")
    monkeypatch.setenv("NOTIFIER_PROVIDER", "local")
    c = AppContainer.from_env()
    # Sprawdzamy interfejsy (porty), nie konkretne klasy — adapter może przyjść z entry point
    assert isinstance(c.task_source, TaskSourcePort)
    assert isinstance(c.llm, ReasoningEnginePort)
    assert isinstance(c.notifier, HumanLoopNotifierPort)


def test_stub_llm_contract():
    _discover_entry_points()
    from engine import adapters as adapters_registry
    factory = adapters_registry.get_adapter_factory("llm", "stub")
    llm = factory()
    assert llm.health_check() is True
    out = llm.complete_structured(dict, system="s", user="u", step="1/5_PLAN")
    assert out["step"] == "1/5_PLAN"


def test_singleton_container(monkeypatch):
    monkeypatch.setenv("TASK_PROVIDER", "memory")
    set_container(None)
    c1 = get_container()
    c2 = get_container()
    assert c1 is c2
    set_container(None)

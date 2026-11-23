import json
from pathlib import Path
from unittest.mock import patch

import pytest

from dashboard import readiness as mod


def _project(tmp_path: Path, name: str, tasks: int) -> dict:
    return {"name": name, "tasks": tasks}


def test_empty_lists_mean_syncing_not_error(tmp_path):
    """AC-1/AC-3: puste listy -> stan 'syncing' z komunikatem, nie 'error' niezależnie od pól None/braku."""
    for raw in ([], None, {}):
        with patch.object(mod, "load_lists", return_value=raw, create=True):
            state = mod.get_readiness_state("proj-x")
            assert state["status"] == "syncing"
            assert state.get("message"), "pusty stan musi mieć wyjaśnienie"
            assert "synchronizacja" in state["message"].lower() or "przygotowanie" in state["message"].lower()


def test_ready_when_all_lists_have_data(tmp_path):
    """AC-1: wszystkie listy wypełnione -> status 'ready' i brak komunikatu synchronizacji."""
    data = {
        "projects": [_project(tmp_path, "a", 3)],
        "tasks": [{"id": 1}],
        "workers": [{"id": 1}],
    }
    with patch.object(mod, "load_lists", return_value=data, create=True):
        state = mod.get_readiness_state("proj-x")
    assert state["status"] == "ready"
    assert not state.get("syncing")


def test_partial_data_still_syncing(tmp_path):
    """Częściowo wypełnione dane (brakuje którejkolwiek listy) -> nadal 'syncing'."""
    data = {
        "projects": [_project(tmp_path, "a", 3)],
        "tasks": [],
        "workers": [{"id": 1}],
    }
    with patch.object(mod, "load_lists", return_value=data, create=True):
        state = mod.get_readiness_state("proj-x")
    assert state["status"] == "syncing"
    assert state["missing"] == ["tasks"]


def test_caretaker_notified_once_on_first_ready():
    """AC-2: opiekun dostaje dokładnie jedno powiadomienie przy przejściu syncing->ready."""
    data_sync = {"lists": {"projects": [], "tasks": [], "workers": []}}
    data_ready = {"lists": {"projects": [{"n": 1}], "tasks": [{"n": 1}], "workers": [{"n": 1}]}}
    sent = []
    with patch.object(mod, "load_lists", side_effect=[data_sync, data_ready, data_ready], create=True), \
         patch.object(mod, "notify_caretaker", side_effect=lambda msg: sent.append(json.dumps(msg, default=str)), create=True):
        st1 = mod.evaluate_readiness("sessions/test", on_change=True)
        st2 = mod.evaluate_readiness("sessions/test", on_change=True)
        st3 = mod.evaluate_readiness("sessions/test", on_change=True)
    assert st1["status"] == "syncing"
    assert st2["status"] == "ready"
    assert st3["status"] == "ready"
    assert len(sent) == 1, "powiadomienie tylko raz, nie przy każdym odpytaniu"


def test_notification_not_sent_for_no_op_ready():
    """Brak powtórzenia powiadomienia, gdy stan już wcześniej był ready (sesje osobne per opiekun)."""
    data_ready = {"lists": {"projects": [{"n": 1}], "tasks": [{"n": 1}], "workers": [{"n": 1}]}}
    sent = []
    with patch.object(mod, "load_lists", return_value=data_ready, create=True), \
         patch.object(mod, "notify_caretaker", side_effect=lambda msg: sent.append(json.dumps(msg, default=str)), create=True):
        mod.evaluate_readiness("sessions/test", on_change=True)
        mod.evaluate_readiness("sessions/test", on_change=True)
    assert sent == []


def test_empty_lists_have_explanatory_payload(tmp_path):
    """AC-3: każda pusta lista dostaje per-list komunikat 'brak danych — trwa synchronizacja/przygotowanie'."""
    data = {"lists": {"projects": [], "tasks": [], "workers": []}}
    with patch.object(mod, "load_lists", return_value=data, create=True):
        state = mod.get_readiness_state("proj-x")
    for lst in ("projects", "tasks", "workers"):
        info = state["lists"][lst]
        assert info["count"] == 0
        assert info["reason"] in ("syncing", "preparing", "brak danych — trwa synchronizacja/przygotowanie")


def test_state_serializable_for_banner_api():
    """Dashboard zwraca stan jako JSON (banner/powiadomienie) — serializacja bez wyjątku."""
    data_sync = {"lists": {"projects": [], "tasks": [], "workers": []}}
    data_ready = {"lists": {"projects": [{"n": 1}], "tasks": [{"n": 1}], "workers": [{"n": 1}]}}
    with patch.object(mod, "load_lists", side_effect=[data_sync, data_ready], create=True):
        st1 = mod.evaluate_readiness("sessions/test", on_change=False)
        st2 = mod.evaluate_readiness("sessions/test", on_change=False)
    json.dumps(st1, default=str)
    json.dumps(st2, default=str)
    assert isinstance(st2["changed_since_last_poll"], bool)


def test_deterministic_no_random(tmp_path):
    """Stan readiness jest deterministyczny (bez Math.random/randint ani sleepów)."""
    data_ready = {"lists": {"projects": [{"n": 1}], "tasks": [{"n": 1}], "workers": [{"n": 1}]}}
    with patch.object(mod, "load_lists", return_value=data_ready, create=True):
        a = mod.get_readiness_state("proj-x")
        b = mod.get_readiness_state("proj-x")
    assert a == b

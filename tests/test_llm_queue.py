"""LLM QUEUE: kolejka wywołań LLM (engine/llm_queue) — enqueue/claim/lease/retry/dispatch + gateway queue-mode."""
import os
import tempfile
import time
from pathlib import Path

from engine import llm_queue
from engine.state import State


def _tmp():
    return State(db_path=Path(tempfile.mkdtemp()) / "t.db")


def test_enqueue_claim_finish():
    s = _tmp()
    j1 = llm_queue.enqueue(s, "complete", {"step": "S1"}, requester="test", step="S1")
    j2 = llm_queue.enqueue(s, "complete", {"step": "S2"}, step="S2")
    assert llm_queue.stats(s)["QUEUED"]["count"] == 2
    job = llm_queue.claim_next(s)
    assert job["id"] == j1 and job["status"] == "RUNNING" and job["attempts"] == 1
    assert llm_queue.claim_next(s)["id"] == j2
    assert llm_queue.claim_next(s) is None  # lease aktywny
    llm_queue.finish(s, j1, "wynik")
    assert llm_queue.wait_result(s, j1, timeout=2) == "wynik"


def test_retry_backoff_and_failed():
    s = _tmp()
    j = llm_queue.enqueue(s, "complete", {"step": "S"}, step="S")
    llm_queue.claim_next(s)
    llm_queue.fail(s, j, "boom", max_attempts=3)  # attempts=1 -> QUEUED z delay
    row = s._conn().execute("SELECT status, delay_until FROM llm_jobs WHERE id=?", (j,)).fetchone()
    assert row[0] == "QUEUED" and row[1] > time.time()
    s._conn().execute("UPDATE llm_jobs SET attempts=3, delay_until=0 WHERE id=?", (j,))
    llm_queue.fail(s, j, "boom3", max_attempts=3)
    assert s._conn().execute("SELECT status FROM llm_jobs WHERE id=?", (j,)).fetchone()[0] == "FAILED"
    try:
        llm_queue.wait_result(s, j, timeout=1)
        raise AssertionError("powinno rzucić")
    except RuntimeError as e:
        assert "FAILED" in str(e)


def test_lease_recovery():
    s = _tmp()
    j = llm_queue.enqueue(s, "complete", {"step": "S"}, step="S")
    assert llm_queue.claim_next(s)["id"] == j
    s._conn().execute("UPDATE llm_jobs SET lease_until=0 WHERE id=?", (j,))  # symulacja crash
    c = llm_queue.claim_next(s)
    assert c and c["id"] == j and c["attempts"] == 2


def test_dispatch_once_with_fake_gateway(monkeypatch):
    s = _tmp()
    j = llm_queue.enqueue(s, "complete", {"system": "s", "user": "u", "step": "S9", "model": "fast-model"}, step="S9")
    import engine.llm_gateway as gw

    class FakeGW:
        def complete(self, *, system, user, step, model):
            return f"OK:{step}"

    monkeypatch.setitem(gw._REGISTRY, "fake", FakeGW)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert llm_queue.dispatch_once(s, max_jobs=5, min_interval_s=0) == 1
    assert llm_queue.wait_result(s, j, timeout=2) == "OK:S9"


def test_gateway_complete_uses_queue(monkeypatch):
    """VibeCliGateway.complete w trybie kolejki enqueue'uje job zamiast wołać CLI."""
    import engine.llm_gateway as gw
    s = _tmp()
    calls = {}

    class FakeState:
        def __init__(self, *a, **k):
            pass

    monkeypatch.setattr("engine.state.State", lambda *a, **k: s)
    monkeypatch.setattr(llm_queue, "enabled", lambda st: True)

    def fake_enqueue(st, kind, payload, requester="", step=""):
        calls["enq"] = payload
        return "J1"

    def fake_wait(st, jid, timeout=0):
        calls["wait"] = jid
        return "wynik-z-kolejki"

    monkeypatch.setattr(llm_queue, "enqueue", fake_enqueue)
    monkeypatch.setattr(llm_queue, "wait_result", fake_wait)
    g = gw.VibeCliGateway(binary="vibe")
    out = g.complete(system="sys", user="usr", step="STEP", model="fast-model")
    assert out == "wynik-z-kolejki"
    assert calls["enq"]["step"] == "STEP" and calls["wait"] == "J1"


def test_enabled_flag():
    s = _tmp()
    assert llm_queue.enabled(s) is True
    os.environ["LLM_QUEUE"] = "0"
    try:
        assert llm_queue.enabled(s) is False
    finally:
        del os.environ["LLM_QUEUE"]
    s.set_kv("llm_queue", 0)
    assert llm_queue.enabled(s) is False

"""LLM QUEUE: kolejka wywołań LLM (engine/llm_queue) — enqueue/claim/lease/retry/dispatch + gateway queue-mode."""
import os
import tempfile
import time
from pathlib import Path

from engine import llm_queue
Q = llm_queue  # alias używany w nowych testach v2
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
        def __init__(self):
            self.direct_seen = None

        def complete(self, *, system, user, step, model, _direct=False, **_kw):
            self.direct_seen = _direct  # dispatcher MUSI wołać z _direct=True (inaczej deadlock: kolejka czeka sama na siebie)
            return f"OK:{step}"

    fake = FakeGW()
    monkeypatch.setitem(gw._REGISTRY, "fake", lambda *a, **k: fake)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    assert llm_queue.dispatch_once(s, max_jobs=5, min_interval_s=0) == 1
    assert llm_queue.wait_result(s, j, timeout=2) == "OK:S9"
    assert fake.direct_seen is True, "dispatch_once musi wołać gw.complete(_direct=True)"


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

    def fake_enqueue(st, kind, payload, requester="", step="", priority="P2"):
        calls["enq"] = payload
        calls["prio"] = priority
        return "J1"

    def fake_wait(st, jid, timeout=0):
        calls["wait"] = jid
        calls["wait_timeout"] = timeout
        return "wynik-z-kolejki"

    monkeypatch.setattr(llm_queue, "enqueue", fake_enqueue)
    monkeypatch.setattr(llm_queue, "wait_result", fake_wait)
    g = gw.VibeCliGateway(binary="vibe")
    out = g.complete(system="sys", user="usr", step="STEP", model="fast-model")
    assert out == "wynik-z-kolejki"
    assert calls["enq"]["step"] == "STEP" and calls["wait"] == "J1"
    # v2: per-model timeout w payloadu i priorytet zadania w kolejce
    assert calls["enq"]["timeout_s"] == llm_queue.model_timeout_s("fast-model") == 120
    assert calls["prio"] == "P2"
    assert calls["wait_timeout"] == 120 * llm_queue.MAX_ATTEMPTS + 240
    out2 = g.complete(system="sys", user="usr", step="STEP2", model="strong-model", priority="P0")
    assert calls["enq"]["timeout_s"] == 300 and calls["prio"] == "P0"


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


# ===== v2: priorytety, dedup, lease, cleanup =====

def test_enqueue_dedup_and_priority_claim():
    s = _tmp()
    Q.ensure_table(s)
    j1 = Q.enqueue(s, "complete", {"a": 1}, step="X", priority="P0")
    j2 = Q.enqueue(s, "complete", {"a": 1}, step="X", priority="P0")   # duplikat -> ten sam job
    j3 = Q.enqueue(s, "complete", {"b": 2}, step="Y", priority="P3")
    assert j1 == j2 and j1 != j3
    c1 = Q.claim_next(s)
    assert c1["id"] == j1 and c1["priority"] == "P0"          # P0 pierwszy mimo kolejności
    c2 = Q.claim_next(s)
    assert c2["id"] == j3 and c2["priority"] == "P3"           # zajęty lease blokuje re-claim
    # re-claim tylko po wygaśnięciu lease (crash recovery)
    s._conn().execute("UPDATE llm_jobs SET lease_until=0 WHERE id=?", (j1,))
    c3 = Q.claim_next(s)
    assert c3["id"] == j1 and c3["attempts"] == 2


def test_lease_covers_subprocess_timeout():
    # v2: lease musi być dłuższy niż timeout subprocessu (inaczej re-claim = duplikaty RUNNING)
    assert Q.LEASE_S >= 360 + 60
    assert Q.model_timeout_s("fast-model") == 120
    assert Q.model_timeout_s("strong-model") == 300


def test_cleanup_removes_old_done_failed():
    s = _tmp()
    Q.ensure_table(s)
    j = Q.enqueue(s, "complete", {"c": 3}, step="Z")
    Q.finish(s, j, "ok")
    s._conn().execute("UPDATE llm_jobs SET done_at=? WHERE id=?", (time.time() - 48 * 3600, j))
    assert Q.cleanup(s, older_than_s=24 * 3600) == 1
    assert s._conn().execute("SELECT COUNT(*) FROM llm_jobs WHERE id=?", (j,)).fetchone()[0] == 0

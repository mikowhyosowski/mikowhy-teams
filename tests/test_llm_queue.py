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
    # v4: cap timeoutu — min(stary wzór, 2 próby + 120s), żeby worker nie wisiał 19 min
    assert calls["wait_timeout"] == min(120 * llm_queue.MAX_ATTEMPTS + 240, 120 * 2 + 120)
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


# ===== v3: batch (wiele pytań = 1 request LLM) + cache odpowiedzi =====

def test_claim_batch_picks_priority_and_first_attempt_only():
    s = _tmp()
    Q.ensure_table(s)
    ids = []
    for i, prio in enumerate(["P2", "P2", "P0"]):
        ids.append(Q.enqueue(s, "complete", {"q": i, "model": "fast-model"}, step=f"S{i}", priority=prio))
    Q.enqueue(s, "complete", {"q": 99, "model": "strong-model"}, step="SX", priority="P0")
    jobs = Q.claim_batch(s, "fast-model", 2)  # limit 2 z 3 -> ma wejść P0 + jeden P2
    got = {j["id"] for j in jobs}
    assert ids[2] in got and len(jobs) == 2
    assert all(j["model"] == "fast-model" for j in jobs)
    # attempts>0 (retry) nie wchodzi do batcha
    Q.fail(s, ids[0], "err")  # attempts było 1 po claim -> fail -> QUEUED attempts=1? fail nie zmienia attempts; claim zwiększył
    jobs2 = Q.claim_batch(s, "fast-model", 5)
    assert all(j["attempts"] == 1 for j in jobs2)


def test_enqueue_cache_hit_done_in_ttl():
    s = _tmp()
    Q.ensure_table(s)
    j1 = Q.enqueue(s, "complete", {"cache": 1, "model": "fast-model"}, step="C")
    Q.finish(s, j1, "wynik-z-cache")
    j2 = Q.enqueue(s, "complete", {"cache": 1, "model": "fast-model"}, step="C")
    assert j2 == j1  # cache hit — bez nowego joba/requestu
    assert Q.wait_result(s, j2, timeout=1) == "wynik-z-cache"
    # po TTL -> nowy job
    s._conn().execute("UPDATE llm_jobs SET done_at=? WHERE id=?", (time.time() - 2 * Q.CACHE_TTL_S, j1))
    j3 = Q.enqueue(s, "complete", {"cache": 1, "model": "fast-model"}, step="C")
    assert j3 != j1


def test_run_batch_one_request_many_answers(monkeypatch):
    """v3: _run_batch robi JEDEN request LLM dla wielu jobów i rozdziela odpowiedzi per job."""
    import engine.llm_gateway as gwmod
    s = _tmp()
    Q.ensure_table(s)
    jobs = [Q.enqueue(s, "complete", {"q": i, "model": "fast-model", "system": f"sys{i}", "user": f"pyt{i}"}, step=f"S{i}")
            for i in range(3)]
    claimed = Q.claim_batch(s, "fast-model", 3)
    calls = {}

    class FakeGw:
        timeout = 120
        def complete(self, *, system, user, step, model, _direct=False, **kw):
            calls["n_requests"] = calls.get("n_requests", 0) + 1
            calls["user"] = user
            calls["model"] = model
            import json as _json
            items = _json.loads(user)["items"]
            # v4: format separatorowy (nie JSON answers — ten padał na zagnieżdżonym JSON)
            return "".join(f"###ODP-{i+1}###\nodp-na-{it['user']}\n" for i, it in enumerate(items))

    monkeypatch.setattr(gwmod, "get_gateway", lambda: FakeGw())
    did = Q._run_batch(s, claimed)
    assert did == 3 and calls["n_requests"] == 1  # 3 pytania = 1 request
    for j, i in zip(claimed, range(3)):
        row = s._conn().execute("SELECT status, result FROM llm_jobs WHERE id=?", (j["id"],)).fetchone()
        assert row["status"] == "DONE" and row["result"] == f"odp-na-pyt{i}"


def test_run_batch_mismatch_fails_jobs(monkeypatch):
    """Gdy model zwróci złą liczbę odpowiedzi -> każdy job osobno do backoff (nie zgubione)."""
    import engine.llm_gateway as gwmod
    s = _tmp()
    Q.ensure_table(s)
    Q.enqueue(s, "complete", {"q": 1, "model": "fast-model"}, step="A")
    Q.enqueue(s, "complete", {"q": 2, "model": "fast-model"}, step="B")
    claimed = Q.claim_batch(s, "fast-model", 2)

    class FakeGw:
        timeout = 120
        def complete(self, *, system, user, step, model, _direct=False, **kw):
            return '{"answers": ["tylko-jedna"]}'

    monkeypatch.setattr(gwmod, "get_gateway", lambda: FakeGw())
    did = Q._run_batch(s, claimed)
    assert did == 2
    for j in claimed:
        row = s._conn().execute("SELECT status FROM llm_jobs WHERE id=?", (j["id"],)).fetchone()
        assert row["status"] == "QUEUED"  # fail -> backoff -> retry pojedynczo


def test_parse_batch_separators_v4():
    """v4: batch parse po separatorach ###ODP-i### — odporny na zagnieżdżony JSON w odpowiedziach."""
    from engine.llm_queue import _parse_batch
    raw = '###ODP-1###\n{"plan": "x \\"z\\" y"}\n###ODP-2###\nplain answer\n###ODP-3###\n{}\n'
    a = _parse_batch(raw, 3)
    assert a[1] == '{"plan": "x \\"z\\" y"}'
    assert a[2] == 'plain answer'
    assert a[3] == '{}'
    # brakująca odpowiedź -> ValueError (fail jobów, retry pojedyncze)
    try:
        _parse_batch('###ODP-1###\nonly one\n', 2)
        assert False
    except ValueError:
        pass


def test_reclaim_expired_leases():
    """v4: _reclaim_expired — RUNNING z wygasłym lease wraca do QUEUED (backoff), a po max attempts -> FAILED."""
    import time as _t
    s = _tmp()
    Q.ensure_table(s)
    j1 = Q.enqueue(s, "complete", {"q": 1, "model": "fast-model", "system": "s", "user": "u"}, step="S")
    j2 = Q.enqueue(s, "complete", {"q": 2, "model": "fast-model", "system": "s", "user": "u2"}, step="S")
    # oba jako RUNNING z wygasłym lease
    s._conn().execute("UPDATE llm_jobs SET status='RUNNING', lease_until=?, attempts=1 WHERE id IN (?,?)",
                      (_t.time() - 10, j1, j2))
    s._conn().execute("UPDATE llm_jobs SET attempts=? WHERE id=?", (Q.MAX_ATTEMPTS, j2))
    n = Q._reclaim_expired(s)
    assert n == 2
    r1 = s._conn().execute("SELECT status, lease_until FROM llm_jobs WHERE id=?", (j1,)).fetchone()
    r2 = s._conn().execute("SELECT status FROM llm_jobs WHERE id=?", (j2,)).fetchone()
    assert r1["status"] == "QUEUED" and r1["lease_until"] == 0   # do kolejki z powrotem
    assert r2["status"] == "FAILED"                               # attempts >= MAX -> FAILED

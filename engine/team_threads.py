"""Wątki rozmów Sponsora z konkretnymi agentami (per rola/persona).

Logika: Sponsor pyta (Telegram lub dashboard) → route_role() wybiera kompetentną rolę
→ wątek (thread) tej osoby w DB → odpowiedź agenta (LLM jako ta persona) ląduje w wątku.
Sponsor może kontynuować dowolny wątek — zarówno na Telegramie, jak i w dashboardzie
(zakładka Zespół → 💬 przy osobie / lista wątków).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from engine.state import State
from engine import personas


def ensure_thread(state: State, role: str, title: str, chat_id: str | None = None) -> dict:
    """Zwraca otwarty wątek dla roli (najnowszy), albo tworzy nowy."""
    c = state._conn()
    row = c.execute(
        "SELECT * FROM threads WHERE role=? AND open=1 ORDER BY updated_at DESC LIMIT 1", (role,)).fetchone()
    now = time.time()
    if row:
        c.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, row["id"]))
        c.commit()
        return dict(row)
    n = (state.get_kv("thread_seq", 0) or 0) + 1
    state.set_kv("thread_seq", n)
    tid = f"th-{n:04d}"
    p = personas.persona_for_role(role)
    c.execute("INSERT INTO threads(id, role, title, chat_id, created_at, updated_at, open) VALUES(?,?,?,?,?,?,1)",
              (tid, role, (title or f"Rozmowa z {p['name']}")[:120], chat_id, now, now))
    c.commit()
    state.record_metric("thread_created", {"thread": tid, "role": role, "title": title[:80]}, None)
    return {"id": tid, "role": role, "title": (title or "")[:120], "chat_id": chat_id,
            "created_at": now, "updated_at": now, "open": 1}


def add_message(state: State, thread_id: str, author: str, text: str, kind: str = "msg") -> dict:
    c = state._conn()
    now = time.time()
    c.execute("INSERT INTO thread_messages(thread_id, ts, author, text, kind) VALUES(?,?,?,?,?)",
              (thread_id, now, author, text, kind))
    c.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
    c.commit()
    return {"ts": now, "author": author, "text": text, "kind": kind}


def get_thread(state: State, thread_id: str, limit: int = 200) -> dict | None:
    c = state._conn()
    c.row_factory = __import__("sqlite3").Row
    row = c.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not row:
        return None
    msgs = c.execute("SELECT ts, author, text, kind FROM thread_messages WHERE thread_id=? ORDER BY ts LIMIT ?",
                     (thread_id, limit)).fetchall()
    t = dict(row)
    t["messages"] = [dict(m) for m in msgs]
    t["persona"] = personas.persona_full(t["role"])
    return t


def list_threads(state: State, role: str | None = None, limit: int = 30) -> list[dict]:
    c = state._conn()
    c.row_factory = __import__("sqlite3").Row
    q = "SELECT * FROM threads"
    args: tuple = ()
    if role:
        q += " WHERE role=?"
        args = (role,)
    q += " ORDER BY updated_at DESC LIMIT ?"
    args = args + (limit,)
    out = []
    for row in c.execute(q, args).fetchall():
        t = dict(row)
        last = c.execute("SELECT ts, author, text FROM thread_messages WHERE thread_id=? ORDER BY ts DESC LIMIT 1",
                         (t["id"],)).fetchone()
        n = c.execute("SELECT COUNT(*) FROM thread_messages WHERE thread_id=?", (t["id"],)).fetchone()[0]
        t["last"] = dict(last) if last else None
        t["count"] = n
        t["persona"] = personas.persona_for_role(t["role"])
        out.append(t)
    return out


def agent_answer(state: State, role: str, thread_id: str, question: str) -> str:
    """Odpowiedź agenta (LLM jako persona roli) na pytanie Sponsora — zapisuje do wątku."""
    from engine.llm_gateway import get_gateway
    from engine import skill_compiler
    from engine.team_chat import build_state_summary
    thread = get_thread(state, thread_id) or {}
    history = thread.get("messages", [])[-8:]
    task = {"id": "TEAM-THREAD", "title": question[:80], "role": role, "status": "RUNNING",
            "milestone": "chat", "phase": 0, "scope_paths": [], "depends_on": [],
            "acceptance_criteria": "", "retry_count": 0, "review_notes": "",
            "project_id": "mikowhy-teams", "repo": "factory", "read_only": True}
    system = skill_compiler.compile_system_prompt(role, stack=None, task=task)
    system += "\n\n# Aktualny stan fabryki (live, z state.db):\n" + build_state_summary(state)
    system += ("\n\n# Styl: odpowiadaj PO POLSKU, zwięźle, prozą. Jesteś osobą przypisaną do roli "
               f"'{role}'. Nie podejmuj decyzji poza kompetencjami — jeśli trzeba decyzji Sponsora, zapytaj.")
    if history:
        system += ("\n\n# Wątek (kontekst):\n" + "\n".join(
            f"- {m['author']}: {str(m['text'])[:400]}" for m in history))
    gw = get_gateway()
    model = skill_compiler.model_for_role(role, kind="fast")
    try:
        answer = gw.complete(system=system, user=question, step=f"THREAD-{thread_id}", model=model)
    except Exception as exc:
        answer = f"(nie udało się uzyskać odpowiedzi: {exc!r})"
    add_message(state, thread_id, role, answer[:4000], kind="reply")
    return answer


def record_exchange(state: State, role: str, title: str, question: str, answer: str,
                    chat_id: str | None = None) -> dict:
    """Zapisuje parę sponsor→agent do wątku roli (używane przez team_chat)."""
    th = ensure_thread(state, role, title, chat_id=chat_id)
    add_message(state, th["id"], "sponsor", question, kind="msg")
    add_message(state, th["id"], role, answer[:4000], kind="reply")
    return th


def sponsor_post_async(state_factory, thread_id: str, text: str) -> None:
    """Asynchroniczna odpowiedź agenta na wiadomość Sponsora (wiadomość dodaje już endpoint)."""
    state = state_factory()
    th = get_thread(state, thread_id)
    if not th:
        return
    role = th["role"]

    def _reply():
        try:
            agent_answer(state_factory(), role, thread_id, text)
        except Exception as exc:
            add_message(state_factory(), thread_id, "system", f"błąd odpowiedzi agenta: {exc!r}", kind="sys")

    threading.Thread(target=_reply, daemon=True).start()

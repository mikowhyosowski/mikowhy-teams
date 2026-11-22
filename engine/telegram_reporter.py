"""Telegram Reporter — Forum Topics (supergrupa z włączonymi Tematami).

Architektura widoku:
  1 temat (createForumTopic) = 1 zadanie (lub mileston)
  - nazwa tematu żyje: editForumTopic przy każdej zmianie statusu (⏳→🔄→🔍→✅/🚨)
  - pierwsza wiadomość w wątku = PRZYPJĘTA KARTA STANU (pinChatMessage);
    aktualizowana wyłącznie przez editMessageText (zero spamu powiadomień)
  - zwykłe wiadomości w wątku (message_thread_id) tylko przy kamieniach milowych:
    SPLIT_TASK, REJECTED z CR, circuit breaker, ESCALATED, MERGED z podsumowaniem
  - close_topic: ✅ + closeForumTopic po mergu do gałęzi milestonu

Wymagania: prywatna supergrupa z Topics, bot jako admin z Manage Topics / Send / Edit / Pin.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (zaczyna się od -100...). Bez env -> no-op (log lokalny).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.telegram.org"

STATUS_ICON = {
    "READY": "⏳", "RUNNING": "🔄", "IN_REVIEW": "🔍", "READY_TO_MERGE": "📦",
    "MERGED": "✅", "FAILED": "❌", "BLOCKED": "⛔", "ESCALATED": "🚨",
}
STEP_OF = {"1/5_PLAN": 1, "2/5_IMPL": 2, "3/5_TEST": 3, "4/5_QG": 4, "4.5_SIMPLIFY": 4, "5/5_COMMIT": 5, "done": 5}


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'").split(" #", 1)[0].strip()
                os.environ.setdefault(k.strip(), v)


def enabled() -> bool:
    _load_env()
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


def _call(method: str, payload: dict[str, Any], _retry: int = 1) -> dict[str, Any] | None:
    """Wywołanie Bot API z obsługą 429 (retry_after). Zwraca result albo None (no-op)."""
    if not enabled():
        return None
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{API}/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as resp:
            out = json.loads(resp.read().decode())
        if out.get("ok"):
            return out["result"]
        if out.get("error_code") == 429 and _retry < 4:
            time.sleep(out.get("parameters", {}).get("retry_after", 2))
            return _call(method, payload, _retry + 1)
        print(f"[telegram] {method} -> {out.get('error_code')} {out.get('description')}", flush=True)
        return None
    except (urllib.error.URLError, OSError) as exc:
        print(f"[telegram] {method} niedostępny: {exc}", flush=True)
        return None


def topic_name(task: dict[str, Any]) -> str:
    icon = STATUS_ICON.get(task.get("status", "READY"), "⏳")
    sub = f" ({task['substep']})" if task.get("substep") and task["substep"] != "done" else ""
    return f"{icon} [{task['id']}] {task['title'][:90]}{sub}"


def status_bar(substep: str | None) -> str:
    n = STEP_OF.get(substep or "", 0)
    return "[" + "■" * n + "□" * (5 - n) + f"] {n}/5"


def render_card(task: dict[str, Any], children: list[dict[str, Any]] | None = None,
                extra: str = "") -> str:
    icon = STATUS_ICON.get(task.get("status", "READY"), "⏳")
    lines = [
        f"📌 *{task['id']}* — {task['title']}",
        f"Status: {icon} *{task.get('status', '?')}* | Postęp: {status_bar(task.get('substep'))}"
        f" | Rola: `{task.get('role', '?')}` | Branch: `{task.get('branch') or '—'}`",
    ]
    if task.get("started_at"):
        lines.append(f"Czas: {int(time.time() - task['started_at'])}s")
    if children:
        lines.append("Podzadania:")
        for c in children:
            ci = STATUS_ICON.get(c.get("status", "READY"), "⏳")
            lines.append(f"  ↳ {ci} [{c['id']}] {c['title'][:60]}")
    if extra:
        lines.append(f"\n{extra}")
    return "\n".join(lines)


def ensure_topic(state, task: dict[str, Any]) -> None:
    """Tworzy temat forum dla zadania (jeśli jeszcze nie ma), wysyła i przypina kartę stanu.
    Zapisuje telegram_thread_id + telegram_summary_msg_id w stanie.
    Fallback: gdy czat NIE jest forum (np. priv) — thread_id=0 i karta jako przypięta
    wiadomość na głównym czacie; eventy idą jako zwykłe wiadomości."""
    if task.get("telegram_thread_id"):
        return
    topic = _call("createForumTopic", {"chat_id": _chat_id(), "name": topic_name(task)[:128]})
    if topic:
        thread_id = topic["message_thread_id"]
        card = _call("sendMessage", {
            "chat_id": _chat_id(), "message_thread_id": thread_id,
            "text": render_card(task),
        })
    else:
        # fallback: czat prywatny / grupa bez Topics
        thread_id = 0
        card = _call("sendMessage", {"chat_id": _chat_id(), "text": render_card(task)})
    msg_id = card["message_id"] if card else None
    if msg_id:
        _call("pinChatMessage", {"chat_id": _chat_id(), "message_id": msg_id, "disable_notification": True})
    state.set_telegram(task["id"], thread_id, msg_id)
    state.record_metric("telegram_topic", {"thread_id": thread_id, "msg_id": msg_id, "forum": bool(topic)}, task["id"])


def _rename_topic(task: dict[str, Any]) -> None:
    tid = task.get("telegram_thread_id")
    if not tid:  # 0 = fallback (brak forum)
        return
    _call("editForumTopic", {"chat_id": _chat_id(), "message_thread_id": tid,
                             "name": topic_name(task)[:128]})


def update_progress(state, task: dict[str, Any], extra: str = "") -> None:
    """Aktualizuje przypiętą kartę (editMessageText) + nazwę tematu. Wywoływane przy każdym mikro-kroku."""
    if task.get("telegram_thread_id") is None or not task.get("telegram_summary_msg_id"):
        return
    children = [t for t in state.list_tasks(milestone=task["milestone"]) if t.get("parent_task") == task["id"]]
    text = render_card(task, children, extra)
    payload: dict[str, Any] = {
        "chat_id": _chat_id(), "message_id": task["telegram_summary_msg_id"],
        "text": text,
    }
    if task.get("telegram_thread_id"):  # wątek forum; 0 = fallback priv
        payload["message_thread_id"] = task["telegram_thread_id"]
    _call("editMessageText", payload)
    _rename_topic(task)


def post_event(state, task: dict[str, Any], text: str) -> None:
    """Zwykła wiadomość do wątku zadania — tylko kamienie milowe (SPLIT, CR REJECTED, breaker, MERGED)."""
    if task.get("telegram_thread_id") is None:
        ensure_topic(state, task)
        task = state.get_task(task["id"]) or task
    if task.get("telegram_thread_id") is None:
        return
    payload: dict[str, Any] = {
        "chat_id": _chat_id(),
        "text": f"[{task['id']}] {text}",
    }
    if task.get("telegram_thread_id"):
        payload["message_thread_id"] = task["telegram_thread_id"]
    _call("sendMessage", payload)


def close_topic(state, task: dict[str, Any], summary: str = "") -> None:
    """MERGED: karta na ✅, event z podsumowaniem, temat zamknięty (archiwum, wciąż czytelne)."""
    task = dict(task)
    task["status"] = "MERGED"
    if task.get("telegram_thread_id"):
        update_progress(state, task, extra=summary)
        post_event(state, task, f"✅ *MERGED*\n{summary}")
        _call("closeForumTopic", {"chat_id": _chat_id(), "message_thread_id": task["telegram_thread_id"]})


def post_milestone(text: str) -> None:
    """Wiadomość na główny temat grupy (General) — np. QG-3 / 7a / 7b dla milestonu."""
    _call("sendMessage", {"chat_id": _chat_id(), "text": text})

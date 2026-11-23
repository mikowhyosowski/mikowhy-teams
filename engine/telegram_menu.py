"""Telegram Menu — interaktywny FSM w prywatnym czacie / forum.

Flow (każdy ekran ma przycisk 🔙 Cofnij do poprzedniego stanu):
  main → [📋 Zadania] → list (po 1 przycisku na zadanie + filtry statusów)
       → [task] → szczegóły zadania + [✅ Approve] [🛑 Decline] [💬 Komentuj] [🔙]
  [🩺 Health] → wynik health check
  [💬 Komentuj] → stan oczekiwania na wiadomość (awaiting input); następny tekst = komentarz do zadania

Komendy tekstowe: start / menu / list / list <STATUS> / status / health / help

Poller jest jeden na bota (lock plikowy .agents/telegram.lock) — 409 Conflict obsłużone.
`run --poll 1` woła poll_once w każdym ticku, więc samo odbiera wiadomości.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from engine import health_monitor, projects, telegram_gate, telegram_reporter
from engine.state import State

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.telegram.org"
LOCK_PATH = ROOT / ".agents" / "telegram.lock"
MAX_LIST_BUTTONS = 25
STATUSES = ["AWAITING_OWNER", "READY", "RUNNING", "CHANGES_REQUESTED", "IN_REVIEW", "READY_TO_MERGE", "MERGED", "BLOCKED", "FAILED", "ESCALATED"]
ICON = telegram_reporter.STATUS_ICON


# --- lock: jeden poller na bota -------------------------------------------------
def _acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


# --- stan menu per czat ------------------------------------------------------------
def _get(state: State, chat_id: str, key: str, default):
    v = state.get_kv(f"menu:{chat_id}:{key}")
    return default if v is None else v


def _set(state: State, chat_id: str, key: str, value) -> None:
    state.set_kv(f"menu:{chat_id}:{key}", value)


def _push_hist(state: State, chat_id: str, screen: str) -> None:
    hist = _get(state, chat_id, "hist", [])
    if not hist or hist[-1] != screen:
        hist.append(screen)
        _set(state, chat_id, "hist", hist[-12:])  # max 12 poziomów


def _pop_hist(state: State, chat_id: str) -> str:
    hist = _get(state, chat_id, "hist", [])
    if len(hist) > 1:
        hist.pop()
        _set(state, chat_id, "hist", hist)
        return hist[-1]
    return "main"


# --- ekrany ------------------------------------------------------------------------
def _main_text() -> str:
    return ("🤖 *Dark Factory* — wybierz:\n"
            "📋 Zadania — lista wszystkich zadań (filtry + akcje per zadanie)\n"
            "🩺 Health — check projektów + auto-zadania FIX\n"
            "📊 Status — projekty i milestone'y\n"
            "Komendy tekstowe: `list`, `list READY`, `status`, `health`, `start`")


def _main_kb() -> dict:
    return {"inline_keyboard": [
        [{"text": "📋 Zadania", "callback_data": "menu:list:ALL"}],
        [{"text": "🩺 Health teraz", "callback_data": "menu:health"}],
        [{"text": "📊 Status projektów", "callback_data": "menu:status"}],
    ]}


def _list_kb(state: State, status_filter: str) -> dict:
    tasks = state.list_tasks()
    if status_filter != "ALL":
        tasks = [t for t in tasks if t["status"] == status_filter]
    rows = []
    for t in tasks[:MAX_LIST_BUTTONS]:
        icon = ICON.get(t["status"], "?")
        label = f"{icon} {t['id']} — {t['title'][:38]}"
        rows.append([{"text": label, "callback_data": f"menu:task:{t['id']}"}])
    if len(tasks) > MAX_LIST_BUTTONS:
        rows.append([{"text": f"… i {len(tasks) - MAX_LIST_BUTTONS} więcej (filtruj statusem)", "callback_data": "menu:noop"}])
    filt = [{"text": s if status_filter != s else f"•{s}•", "callback_data": f"menu:list:{s}"} for s in ["ALL"] + STATUSES]
    rows.append([f for f in filt[:5]])
    rows.append([f for f in filt[5:]])
    rows.append([{"text": "🔙 Cofnij", "callback_data": "menu:back"}])
    return {"inline_keyboard": rows}


def _list_text(status_filter: str, state: State) -> str:
    tasks = state.list_tasks()
    n_all = len(tasks)
    if status_filter != "ALL":
        tasks = [t for t in tasks if t["status"] == status_filter]
    return f"📋 Zadania [{status_filter}] — {len(tasks)}/{n_all}\nKliknij zadanie, żeby zobaczyć szczegóły i akcje."


def _task_text(state: State, task_id: str) -> str:
    t = state.get_task(task_id)
    if not t:
        return f"❌ Nieznane zadanie: {task_id}"
    icon = ICON.get(t["status"], "?")
    lines = [
        f"{icon} *{t['id']}* — {t.get('title_sponsor') or t['title']}",
        f"   (tech: {t['title'][:80]})" if t.get('title_sponsor') and t.get('title_sponsor') != t['title'] else "",
        f"Status: *{t['status']}* | faza: {t['phase']} | rola: {t['role']}",
        f"Projekt: {t.get('project_id')} (approval_policy: {projects.approval_policy(t.get('project_id') or 'mikowhy-teams')}) | repo: {t.get('repo') or 'target'} | read_only: {'TAK — zero commitów' if t.get('read_only') else 'nie'} | milestone: {t['milestone']}",
        f"Zależy od: {t['depends_on']} | substep: {t.get('substep')} | branch: {t.get('branch')}",
        f"Scope: `{t['scope_paths']}`",
        f"AC: {t['acceptance_criteria'][:300]}",
    ]
    if t.get("review_notes"):
        lines.append(f"📝 notes: {t['review_notes'][:400]}")
    comments = state.recent_comments(limit=5, task_id=task_id)
    if comments:
        lines.append("💬 ostatnie komentarze agentów:")
        for c in reversed(comments):
            lines.append(f"  • {c['author']}: {c['text'][:120]}")
    return "\n".join(lines)


def _task_kb(task_id: str, status: str) -> dict:
    rows = []
    if status == "AWAITING_OWNER":  # Opiekun jako ostatnia instancja
        rows.append([{"text": "✅ Approve → READY", "callback_data": f"menu:act:approve:{task_id}"},
                     {"text": "🛑 Decline → BLOCKED", "callback_data": f"menu:act:decline:{task_id}"}])
    rows.append([{"text": "💬 Komentuj (następna wiadomość = uwaga)", "callback_data": f"menu:act:comment:{task_id}"},
                 {"text": "🔄 Odśwież", "callback_data": f"menu:task:{task_id}"}])
    rows.append([{"text": "🔙 Cofnij", "callback_data": "menu:back"}])
    return {"inline_keyboard": rows}


def _status_text(state: State) -> str:
    lines = [f"📊 Aktywny projekt: *{projects.active_project_id()}*"]
    for p in projects.list_projects():
        lines.append(f"  🗂 {p['project_id']}: approval_policy={p.get('approval_policy', 'manual')} read_only={bool(p.get('read_only'))}")
    for ms in state.list_milestones():
        lines.append(f"\n🎯 {ms['id']} [{ms['status']}] ({ms.get('project_id')}) — {ms['goal'][:60]}")
        for t in state.milestone_tasks(ms["id"]):
            lines.append(f"  {ICON.get(t['status'], '?')} {t['id']} [{t['status']}] {t['role']}")
    return "\n".join(lines)


# --- render + nawigacja --------------------------------------------------------------
def render(state: State, chat_id: str, screen: str) -> tuple[str, dict]:
    _push_hist(state, chat_id, screen)
    _set(state, chat_id, "screen", screen)
    if screen == "main":
        return _main_text(), _main_kb()
    if screen.startswith("list:"):
        f = screen.split(":", 1)[1]
        return _list_text(f, state), _list_kb(state, f)
    if screen.startswith("task:"):
        tid = screen.split(":", 1)[1]
        t = state.get_task(tid)
        return _task_text(state, tid), _task_kb(tid, t["status"] if t else "?")
    if screen == "health":
        rep = health_monitor.run(state, create_tasks=True)
        n = len(rep["anomalies"])
        txt = f"🩺 Health: {'✅ OK' if rep['ok'] else f'⚠️ {n} anomalii'}"
        for a in rep["anomalies"][:10]:
            txt += f"\n- [{a['project']}] {a['kind']}: {a['detail'][:100]}"
        txt += "\n\n(szczegóły: .agents/health-report.json, docs/HEALTH.md)"
        return txt, {"inline_keyboard": [[{"text": "🔙 Cofnij", "callback_data": "menu:back"}]]}
    if screen == "status":
        return _status_text(state), {"inline_keyboard": [[{"text": "🔙 Cofnij", "callback_data": "menu:back"}]]}
    return _main_text(), _main_kb()


def _send(chat_id: str, text: str, kb: dict | None = None, message_id: int | None = None) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text.replace("*", "")}
    if kb:
        payload["reply_markup"] = json.dumps(kb)
    if message_id:
        payload["message_id"] = message_id
        telegram_gate._call("editMessageText", payload)
    else:
        telegram_gate._call("sendMessage", payload)


# --- obsługa update'ów ----------------------------------------------------------------
def handle_callback(state: State, chat_id: str, cb_id: str, data: str, message_id: int) -> None:
    telegram_gate.answer_callback(cb_id, "")
    if data == "menu:noop":
        return
    if data == "menu:back":
        screen = _pop_hist(state, chat_id)
    elif data.startswith("menu:act:comment:"):
        tid = data.split(":", 3)[3]
        _set(state, chat_id, "await", {"task": tid, "action": "comment", "ts": time.time()})
        _send(chat_id, f"💬 Napisz komentarz do *{tid}* — Twoja następna wiadomość zostanie zapisana jako uwaga (review_notes).",
              {"inline_keyboard": [[{"text": "🔙 Anuluj", "callback_data": "menu:back"}]]}, message_id)
        return
    elif data.startswith("menu:act:approve:"):
        tid = data.split(":", 3)[3]
        from engine.orchestrator import cmd_approve
        try:
            cmd_approve(tid)
        except SystemExit:
            pass
        screen = f"task:{tid}"
    elif data.startswith("menu:act:decline:"):
        tid = data.split(":", 3)[3]
        from engine.orchestrator import cmd_decline
        cmd_decline(tid, "odrzucone z menu Telegram")
        screen = f"task:{tid}"
    else:
        screen = data[len("menu:"):] if data.startswith("menu:") else "main"
    text, kb = render(state, chat_id, screen)
    _send(chat_id, text, kb, message_id)


def handle_text(state: State, chat_id: str, text: str) -> None:
    cmd = text.strip().lower()
    # komendy ZAWSZE działają — nawet gdy wisiał stan oczekiwania na komentarz
    if cmd in ("start", "/start", "menu", "help", "pomoc"):
        txt, kb = render(state, chat_id, "main")
        _send(chat_id, txt, kb)
    elif cmd == "list" or cmd.startswith("list "):
        parts = cmd.split()
        filt = parts[1].upper() if len(parts) > 1 else "ALL"
        if filt not in STATUSES and filt != "ALL":
            filt = "ALL"
        txt, kb = render(state, chat_id, f"list:{filt}")
        _send(chat_id, txt, kb)
    elif cmd == "status":
        txt, kb = render(state, chat_id, "status")
        _send(chat_id, txt, kb)
    elif cmd == "health":
        txt, kb = render(state, chat_id, "health")
        _send(chat_id, txt, kb)
    elif cmd in ("nowe pytanie", "new", "reset", "nowy wątek"):
        state.set_kv(f"menu:{chat_id}:thread", {"role": None, "history": []})
        _send(chat_id, "🆕 Nowy wątek — następna Twoja wiadomość poleci do drużyny (auto-routing do roli).", _main_kb())
    else:
        # stan oczekiwania na komentarz (max 10 min) — tylko tekst nie będący komendą
        await_ = _get(state, chat_id, "await", None)
        if await_ and time.time() - await_.get("ts", 0) < 600:
            _set(state, chat_id, "await", None)
            from engine.orchestrator import cmd_note
            cmd_note(await_["task"], text.strip())
            screen = f"task:{await_['task']}"
            txt, kb = render(state, chat_id, screen)
            _send(chat_id, f"✅ Zapisano uwagę do {await_['task']}\n\n{txt}", kb)
            return
        # czat z drużyną: osobny proces w osobnej sesji — NIGDY nie blokuje ticka orkiestratora
        # (lock per czat trzyma sam team_chat — patrz engine/team_chat.py main)
        log = open(ROOT / ".agents" / "logs" / f"team_chat.{chat_id}.log", "a", encoding="utf-8")
        subprocess.Popen([sys.executable, "-m", "engine.team_chat", chat_id, text],
                         cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True)  # odczepiony od ticka, nie ubija się z nim
        _send(chat_id, "🤖 Kieruję do zespołu… odpowiedź przyjdzie jako wiadomość z podpisem (kto odpisał). "
                       "Napisz «nowe pytanie», żeby zacząć nowy temat.")


def _handle_legacy_callback(state: State, cb_id: str, data: str) -> None:
    """Stare callbacki: approve:/decline: (deploy 7b) i approve_task:/decline_task: (AWAITING_OWNER)."""
    action, _, ref = data.partition(":")
    if action == "approve":
        state.resolve_approval(ref, "APPROVED")
        telegram_gate.answer_callback(cb_id, "Deploy PROD zatwierdzony 🚀")
        telegram_gate.send_message(f"🚀 Deploy PROD zatwierdzony dla `{ref}`.")
    elif action == "decline":
        state.resolve_approval(ref, "DECLINED")
        telegram_gate.answer_callback(cb_id, "Rollback zapisany 🛑")
        telegram_gate.send_message(f"🛑 Rollback dla `{ref}` — produkcja wstrzymana.")
    elif action == "approve_task":
        from engine.orchestrator import cmd_approve
        try:
            cmd_approve(ref)
            telegram_gate.answer_callback(cb_id, f"{ref} → READY ✅")
        except SystemExit as e:
            telegram_gate.answer_callback(cb_id, str(e))
    elif action == "decline_task":
        from engine.orchestrator import cmd_decline
        cmd_decline(ref, "odrzucone przyciskiem na Telegramie")
        telegram_gate.answer_callback(cb_id, f"{ref} → BLOCKED 🛑")


def poll_once(state: State, timeout: int = 0) -> int:
    """Jeden nieblokujący (lub krótki) getUpdates. Zwraca liczba obsłużonych update'ów.
    Lock plikowy: jeśli inny proces już polleruje — wychodzimy (brak 409)."""
    if not telegram_reporter.enabled():
        return 0
    fh = _acquire_lock()
    if fh is None:
        return 0
    try:
        offset = state.get_kv("tg_offset", 0)
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        q = urllib.parse.urlencode({"offset": offset, "timeout": timeout,
                                    "allowed_updates": json.dumps(["message", "callback_query"])})
        try:
            with urllib.request.urlopen(f"{API}/bot{token}/getUpdates?{q}", timeout=timeout + 10) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 409:  # ktoś inny polleruje
                return 0
            raise
        n = 0
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            n += 1
            cb = upd.get("callback_query")
            if cb:
                data_str = cb["data"]
                chat_id = str(cb["message"]["chat"]["id"])
                if data_str.startswith(("approve:", "decline:", "approve_task:", "decline_task:")):
                    _handle_legacy_callback(state, cb["id"], data_str)  # deploy 7b / approve_task
                else:
                    handle_callback(state, chat_id, cb["id"], data_str, cb["message"]["message_id"])
            msg = upd.get("message")
            if msg and msg.get("text") and not msg.get("from", {}).get("is_bot"):
                handle_text(state, str(msg["chat"]["id"]), msg["text"])
        state.set_kv("tg_offset", offset)
        return n
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()

"""Telegram gate — zmiana #6: Faza 7 dzieli się na 7a (auto staging + E2E smoke)
i 7b (Human-in-the-Loop). Product Sponsor dostaje raport z linkiem do staging,
diffem kluczowych zmian biznesowych i przyciskami inline:
[🚀 Deploy Prod] / [🛑 Rollback / Uwagi]. Nikt poza człowiekiem nie deploysuje
na produkcję — nawet przy zielonych smoke-testach.

Konfiguracja: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.telegram.org"


def _load_env() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'")
                v = v.split(" #", 1)[0].strip()  # obetnij komentarz inline
                os.environ.setdefault(k.strip(), v)


_load_env()


def _enabled() -> bool:
    _load_env()
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def _call(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"{API}/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as resp:
        return json.loads(resp.read().decode())


def send_message(text: str, reply_markup: dict | None = None) -> dict[str, Any] | None:
    if not _enabled():
        print(f"[telegram-gate] (wyłączone) {text}", flush=True)
        return None
    payload: dict[str, Any] = {"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
                               }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    return _call("sendMessage", payload)


def request_prod_approval(milestone: str, report: dict[str, Any]) -> str | None:
    """Faza 7b: wysyła raport i czeka na decyzję człowieka. Zwraca approval_id."""
    approval_id = f"DEPLOY_PROD:{milestone}"
    text = (
        f"*🚦 Kamień milowy `{milestone}` gotowy do produkcji*\n\n"
        f"• Wdrożono na staging: `{report.get('staging_url', '—')}`\n"
        f"• Testy: `{report.get('tests', '—')}`\n"
        f"• Koszt tokenów: `{report.get('tokens', '—')}`\n"
        f"• Kluczowe zmiany biznesowe:\n{report.get('business_diff', '—')}\n\n"
        f"Decyzja należy do Product Sponsora:"
    )
    markup = {"inline_keyboard": [[
        {"text": "🚀 Deploy Prod", "callback_data": f"approve:{approval_id}"},
        {"text": "🛑 Rollback / Uwagi", "callback_data": f"decline:{approval_id}"},
    ]]}
    send_message(text, markup)
    return approval_id


def poll_updates(offset: int = 0, timeout: int = 25) -> tuple[int, list[dict[str, Any]]]:
    """Long-polling Telegrama. Zwraca (nowy offset, lista callbacków do obsłużenia)."""
    if not _enabled():
        return offset, []
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    q = urllib.parse.urlencode({"offset": offset, "timeout": timeout, "allowed_updates": ["callback_query"]})
    with urllib.request.urlopen(f"{API}/bot{token}/getUpdates?{q}", timeout=timeout + 10) as resp:
        data = json.loads(resp.read().decode())
    callbacks: list[dict[str, Any]] = []
    new_offset = offset
    for upd in data.get("result", []):
        new_offset = upd["update_id"] + 1
        cb = upd.get("callback_query")
        if cb:
            callbacks.append({"id": cb["id"], "data": cb["data"], "chat_id": cb["message"]["chat"]["id"]})
    return new_offset, callbacks


def answer_callback(callback_id: str, text: str) -> None:
    if not _enabled():
        return
    _call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

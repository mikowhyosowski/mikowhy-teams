"""Adaptery HumanLoopNotifierPort: telegram (telegram_gate/reporter), local (log), slack, webhook."""
from __future__ import annotations

from engine.ports import DecisionHandle, DecisionPacket


class TelegramNotifierAdapter:
    def __init__(self):
        from engine import telegram_reporter, telegram_gate
        self._reporter = telegram_reporter
        self._gate = telegram_gate

    def notify_status(self, task_id: str, message: str) -> None:
        try:
            from engine.state import State
            task = State().get_task(task_id) or {"id": task_id, "title": task_id, "status": "", "role": ""}
            self._reporter.post_event(State(), task, message)
        except Exception:
            pass

    def request_decision(self, packet: DecisionPacket) -> DecisionHandle:
        from engine.state import State
        s = State()
        task = s.get_task(packet.task_id) or {"id": packet.task_id, "title": packet.title, "status": "AWAITING_OWNER", "role": ""}
        self._reporter.ensure_topic(s, task)
        self._reporter.post_event(s, task,
                                  f"🧑‍💼 PAKIET dla Opiekuna: {packet.essence}\nOpcje: {packet.options}\nRekomendacja: {packet.recommendation}")
        return DecisionHandle(task_id=packet.task_id, channel_ref="telegram")

    def post_milestone_update(self, milestone_id: str, message: str) -> None:
        try:
            self._gate.send_message(f"🏁 {milestone_id}: {message}")
        except Exception:
            pass


class LocalNotifierAdapter:
    """Fallback: loguje do stdout — używany gdy TELEGRAM wyłączony / testy."""

    def notify_status(self, task_id: str, message: str) -> None:
        print(f"[notify] {task_id}: {message}", flush=True)

    def request_decision(self, packet: DecisionPacket) -> DecisionHandle:
        print(f"[decision] {packet.task_id}: {packet.essence} | opcje={packet.options} | rekomendacja={packet.recommendation}", flush=True)
        return DecisionHandle(task_id=packet.task_id, channel_ref="local")

    def post_milestone_update(self, milestone_id: str, message: str) -> None:
        print(f"[milestone] {milestone_id}: {message}", flush=True)


class SlackNotifierAdapter:
    """
    Adapter HumanLoopNotifierPort dla Slack.
    
    Wymaga: SLACK_BOT_TOKEN z uprawnieniami chat:write, channels:read
    Konfiguracja przez env vars:
    - SLACK_BOT_TOKEN: xoxb-...
    - SLACK_CHANNEL: kanał do powiadomień (domyślnie: #general)
    """

    def __init__(
        self,
        token: str | None = None,
        channel: str | None = None,
    ):
        import os
        self.token = token or os.environ.get("SLACK_BOT_TOKEN")
        self.channel = channel or os.environ.get("SLACK_CHANNEL", "#general")
        self._client = None  # slack_sdk.WebClient

    def _get_client(self):
        if self._client is None:
            try:
                from slack_sdk import WebClient
                if not self.token:
                    raise RuntimeError("SLACK_BOT_TOKEN not set")
                self._client = WebClient(token=self.token)
            except ImportError:
                raise RuntimeError("slack_sdk not installed. pip install slack_sdk")
        return self._client

    def notify_status(self, task_id: str, message: str) -> None:
        try:
            client = self._get_client()
            client.chat_postMessage(channel=self.channel, text=f"📋 `{task_id}`: {message}")
        except Exception:
            pass  # nie blokujemy wykonania

    def request_decision(self, packet: DecisionPacket) -> DecisionHandle:
        try:
            client = self._get_client()
            blocks = [
                {"type": "header", "text": {"type": "plain_text", "text": f"🧑‍💼 Decyzja: {packet.task_id}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": packet.essence}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Opcje:* {', '.join(packet.options) or '-'}"}},
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*Rekomendacja:* {packet.recommendation or '-'}"}},
            ]
            client.chat_postMessage(channel=self.channel, blocks=blocks, text=f"Decyzja dla {packet.task_id}")
        except Exception:
            pass
        return DecisionHandle(task_id=packet.task_id, channel_ref=f"slack:{self.channel}")

    def post_milestone_update(self, milestone_id: str, message: str) -> None:
        try:
            client = self._get_client()
            client.chat_postMessage(channel=self.channel, text=f"🏁 *{milestone_id}*: {message}")
        except Exception:
            pass


class WebhookNotifierAdapter:
    """
    Adapter HumanLoopNotifierPort dla ogólnego Webhook (HTTP POST).
    
    Konfiguracja przez env vars:
    - WEBHOOK_URL: URL do POST (wymagany)
    - WEBHOOK_SECRET: opcjonalny sekret do podpisywania (HMAC-SHA256)
    - WEBHOOK_HEADERS: JSON string z dodatkowymi nagłówkami
    """

    def __init__(
        self,
        url: str | None = None,
        secret: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        import os
        import json
        self.url = url or os.environ.get("WEBHOOK_URL")
        self.secret = secret or os.environ.get("WEBHOOK_SECRET")
        self.headers = headers or json.loads(os.environ.get("WEBHOOK_HEADERS", "{}"))

    def _sign(self, payload: bytes) -> str | None:
        if not self.secret:
            return None
        import hmac
        import hashlib
        return hmac.new(self.secret.encode(), payload, hashlib.sha256).hexdigest()

    def _post(self, payload: dict) -> None:
        if not self.url:
            return
        import httpx
        import json
        body = json.dumps(payload).encode()
        h = {"Content-Type": "application/json", **self.headers}
        sig = self._sign(body)
        if sig:
            h["X-Signature"] = sig
        try:
            httpx.post(self.url, content=body, headers=h, timeout=5.0)
        except Exception:
            pass

    def notify_status(self, task_id: str, message: str) -> None:
        self._post({"type": "status", "task_id": task_id, "message": message})

    def request_decision(self, packet: DecisionPacket) -> DecisionHandle:
        self._post({
            "type": "decision",
            "task_id": packet.task_id,
            "title": packet.title,
            "essence": packet.essence,
            "options": packet.options,
            "recommendation": packet.recommendation,
        })
        return DecisionHandle(task_id=packet.task_id, channel_ref=f"webhook:{self.url}")

    def post_milestone_update(self, milestone_id: str, message: str) -> None:
        self._post({"type": "milestone", "milestone_id": milestone_id, "message": message})

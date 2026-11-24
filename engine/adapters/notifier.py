"""Adaptery HumanLoopNotifierPort: telegram (telegram_gate/reporter) i local (log)."""
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

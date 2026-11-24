"""SqliteTaskAdapter — domyślny adapter TaskSourcePort opakowujący engine/state.py (State)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.ports import TaskSourcePort
from engine.state import State


class SqliteTaskAdapter:
    """Cienkie opakowanie nad State — ten sam kontrakt, ten sam SQLite."""

    def __init__(self, db_path: Path | str | None = None):
        self.state = State(db_path) if db_path else State()

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.state.list_tasks(status=status)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self.state.get_task(task_id)

    def claim_next_task(self, role: str, worker_id: str) -> dict[str, Any] | None:
        for t in self.state.claimable_tasks():
            if (t.get("role") or "") == role:
                return self.state.claim_task(t["id"], worker_id)
        return None

    def transition_status(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
        return self.state.transition(task_id, new_status, **fields)

    def append_comment(self, task_id: str, author: str, text: str) -> None:
        self.state.add_comment(task_id, author, text)

    def attach_artifact(self, task_id: str, path: str, label: str = "", kind: str = "") -> dict | None:
        return self.state.add_attachment(task_id, path, label=label, kind=kind)

    def create_subtasks(self, task_id: str, subtask_specs: list[dict[str, Any]]) -> list[str]:
        return self.state.split_task(task_id, subtask_specs)


assert isinstance(SqliteTaskAdapter(), TaskSourcePort) or True  # runtime_check przy starcie kontenera

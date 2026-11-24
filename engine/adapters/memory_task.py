"""InMemoryTaskAdapter — adapter testowy/fallback TaskSourcePort (bez bazy)."""
from __future__ import annotations

import time
from typing import Any

from engine.state import InvalidTransition, VALID_TRANSITIONS, PIPELINE_RANK


class InMemoryTaskAdapter:
    """Ten sam kontrakt co SqliteTaskAdapter, trzymany w słowniku — do testów kontraktowych."""

    def __init__(self):
        self._tasks: dict[str, dict[str, Any]] = {}
        self._comments: list[tuple[str, str, str]] = []
        self._attachments: list[dict] = []
        self._seq = 0

    def _new_id(self) -> str:
        self._seq += 1
        return f"T-{self._seq:04d}"

    def create_task(self, title: str, role: str = "backend-dev", status: str = "READY",
                    milestone: str = "TEST", **extra) -> dict[str, Any]:
        tid = extra.pop("id", None) or self._new_id()
        t = {"id": tid, "title": title, "role": role, "status": status, "milestone": milestone,
             "retry_count": 0, "updated_at": time.time(), **extra}
        self._tasks[tid] = t
        return t

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        ts = list(self._tasks.values())
        if status:
            ts = [t for t in ts if t["status"] == status]
        return sorted(ts, key=lambda t: -(t.get("updated_at") or 0))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._tasks.get(task_id)

    def claim_next_task(self, role: str, worker_id: str) -> dict[str, Any] | None:
        for t in self.list_tasks(status="READY"):
            if (t.get("role") or "") == role:
                return self.transition_status(t["id"], "RUNNING", claimed_by=worker_id)
        return None

    def transition_status(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        current = task["status"]
        allow_regression = bool(fields.pop("allow_regression", False))
        cur_rank = PIPELINE_RANK.get(current)
        new_rank = PIPELINE_RANK.get(new_status)
        is_regression = cur_rank is not None and new_rank is not None and new_rank < cur_rank
        if new_status != current and not (allow_regression and is_regression) \
                and new_status not in VALID_TRANSITIONS.get(current, set()):
            raise InvalidTransition(f"{current} -> {new_status} niedozwolone dla {task_id}")
        if not allow_regression and is_regression:
            raise InvalidTransition(f"{current} -> {new_status} to cofnięcie dla {task_id}")
        task["status"] = new_status
        task.update(fields)
        task["updated_at"] = time.time()
        return dict(task)

    def append_comment(self, task_id: str, author: str, text: str) -> None:
        self._comments.append((task_id, author, text))

    def attach_artifact(self, task_id: str, path: str, label: str = "", kind: str = "") -> dict | None:
        att = {"id": len(self._attachments) + 1, "task_id": task_id, "path": path, "label": label, "kind": kind}
        self._attachments.append(att)
        return att

    def create_subtasks(self, task_id: str, subtask_specs: list[dict[str, Any]]) -> list[str]:
        out = []
        for spec in subtask_specs:
            t = self.create_task(spec.get("title", "sub"), role=spec.get("role", "backend-dev"),
                                 parent_task=task_id)
            out.append(t["id"])
        return out

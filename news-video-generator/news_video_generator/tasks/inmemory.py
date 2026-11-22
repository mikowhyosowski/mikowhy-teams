from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .models import Task, TaskStatus


class InMemoryTaskRepository:
    '''Deterministic in-memory TaskRepository for tests and local runs.'''

    def __init__(self) -> None:
        self._tasks: dict[uuid.UUID, Task] = {}

    def enqueue(self, task: Task) -> Task:
        self._tasks[task.task_id] = task
        return task

    def claim_next_pending(self, worker_id: str) -> Task | None:
        pending = [t for t in self._tasks.values() if t.status is TaskStatus.PENDING]
        if not pending:
            return None
        task = min(pending, key=lambda t: (t.created_at, t.task_id))
        task.status = TaskStatus.RUNNING
        task.attempts += 1
        task.updated_at = datetime.now(timezone.utc)
        return task

    def save(self, task: Task) -> None:
        self._tasks[task.task_id] = task

    def get(self, task_id: uuid.UUID) -> Task | None:
        return self._tasks.get(task_id)


class InMemoryFeedRepository:
    '''In-memory FeedRepository; comments kept in insertion order.'''

    def __init__(self) -> None:
        self.comments: list[dict[str, str]] = []

    def add_comment(self, task_id: uuid.UUID, author: str, body: str) -> None:
        self.comments.append({'task_id': str(task_id), 'author': author, 'body': body})

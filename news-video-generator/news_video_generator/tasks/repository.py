from __future__ import annotations

import uuid
from typing import Protocol

from .models import Task


class TaskRepository(Protocol):
    '''Storage contract for the task queue (Postgres in production).'''

    def claim_next_pending(self, worker_id: str) -> Task | None:
        '''Atomically move the oldest pending task to running and return it.'''
        ...

    def save(self, task: Task) -> None:
        ...

    def get(self, task_id: uuid.UUID) -> Task | None:
        ...


class FeedRepository(Protocol):
    '''Append-only comment feed attached to tasks.'''

    def add_comment(self, task_id: uuid.UUID, author: str, body: str) -> None:
        ...

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


class TaskStatus(str, enum.Enum):
    '''Lifecycle of a queued task: pending -> running -> done | escalated.'''

    PENDING = 'pending'
    RUNNING = 'running'
    DONE = 'done'
    ESCALATED = 'escalated'


@dataclass(frozen=True)
class RetryPolicy:
    '''Retry policy for failed tasks: at most max_attempts executions.'''

    max_attempts: int = 3


@dataclass
class Task:
    '''A unit of work pulled from the queue.

    attempts counts executions (incremented on every claim), which makes
    retries observable and lets the worker detect policy exhaustion.
    '''

    task_id: uuid.UUID
    payload: dict[str, object]
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

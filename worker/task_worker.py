"""Worker zadań: deterministyczny cykl pending -> running -> done / retry / ESCALATED."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

ERROR_TASK_EXECUTION = "TASK_EXECUTION_FAILED"


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ESCALATED = "escalated"


@dataclass
class FeedComment:
    task_id: str
    author: str
    body: str
    created_at: str


@dataclass
class Task:
    task_id: str
    payload: dict[str, Any]
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3
    error: str | None = None


class TaskStore(Protocol):
    """Port magazynu zadań — kontrakt wewnętrzny workera."""

    def claim_next_pending(self) -> Task | None: ...
    def save(self, task: Task) -> None: ...
    def add_feed_comment(self, comment: FeedComment) -> None: ...
    def list_feed_comments(self, task_id: str) -> list[FeedComment]: ...


class InMemoryTaskStore:
    """Deterministyczna implementacja in-memory (dev/testy, bez I/O zewnętrznego)."""

    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._feed: list[FeedComment] = []

    def enqueue(self, task: Task) -> None:
        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def claim_next_pending(self) -> Task | None:
        for task in self._tasks.values():
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.RUNNING
                return task
        return None

    def save(self, task: Task) -> None:
        self._tasks[task.task_id] = task

    def add_feed_comment(self, comment: FeedComment) -> None:
        self._feed.append(comment)

    def list_feed_comments(self, task_id: str) -> list[FeedComment]:
        return [c for c in self._feed if c.task_id == task_id]


def utc_now_iso() -> str:
    """Bieżący czas ISO-8601 z timezone UTC (reguła globalna: daty tylko UTC)."""
    return datetime.now(timezone.utc).isoformat()


def build_event(event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Zdarzenie zgodne z FROZEN contracts/events.schema.json."""
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": utc_now_iso(),
        "payload": payload,
    }


Handler = Callable[[dict[str, Any]], None]


class TaskWorker:
    """Konsument kolejki: pobiera pending, wykonuje, zamyka done albo eskaluje."""

    def __init__(self, store: TaskStore, handler: Handler) -> None:
        self._store = store
        self._handler = handler

    def process_next(self) -> Task | None:
        """Pobiera jedno zadanie i przeprowadza je przez pełny cykl życia."""
        task = self._store.claim_next_pending()
        if task is None:
            logger.info("Kolejka pusta", extra={"event": build_event("task.queue.empty.v1", {})})
            return None
        logger.info("Zadanie rozpoczęte", extra={"event": build_event("task.started.v1", {"task_id": task.task_id}), "attempt": task.attempts + 1})
        try:
            self._handler(task.payload)
        except Exception as exc:
            self._handle_failure(task, exc)
        else:
            task.status = TaskStatus.DONE
            task.error = None
            self._store.save(task)
            logger.info("Zadanie zakończone", extra={"event": build_event("task.done.v1", {"task_id": task.task_id})})
        return task

    def _handle_failure(self, task: Task, exc: Exception) -> None:
        """Polityka retry: próba < max -> pending, wyczerpanie prób -> ESCALATED (AC#2)."""
        task.attempts += 1
        task.error = f"{type(exc).__name__}: {exc}"
        if task.attempts >= task.max_attempts:
            self._escalate(task)
            return
        task.status = TaskStatus.PENDING
        self._store.save(task)
        logger.warning("Zadanie do ponowienia", extra={"event": build_event("task.retry.v1", {"task_id": task.task_id, "attempts": task.attempts}), "error_code": ERROR_TASK_EXECUTION})

    def _escalate(self, task: Task) -> None:
        """Oznacza zadanie jako ESCALATED i dodaje komentarz do feedu (AC#2)."""
        task.status = TaskStatus.ESCALATED
        self._store.save(task)
        self._store.add_feed_comment(
            FeedComment(
                task_id=task.task_id,
                author="task-worker",
                body=f"Escalacja po {task.attempts} próbach ({ERROR_TASK_EXECUTION}): {task.error}",
                created_at=utc_now_iso(),
            )
        )
        logger.error("Zadanie eskalowane", extra={"event": build_event("task.escalated.v1", {"task_id": task.task_id, "attempts": task.attempts}), "error_code": ERROR_TASK_EXECUTION})

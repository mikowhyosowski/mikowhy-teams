"""Testy jednostkowe cyklu życia zadania workera: happy path, retry, eskalacja."""

import re
from typing import Any

from worker.task_worker import (
    InMemoryTaskStore,
    Task,
    TaskStatus,
    TaskWorker,
    build_event,
)

EVENT_TYPE_RE = re.compile(r"^[a-z]+\.[a-z_]+\.v[0-9]+$")


def make_task(task_id: str = "task-1", max_attempts: int = 3) -> Task:
    return Task(task_id=task_id, payload={"kind": "test"}, max_attempts=max_attempts)


def test_happy_path_pending_running_done() -> None:
    store = InMemoryTaskStore()
    task = make_task()
    store.enqueue(task)
    observed: dict[str, Any] = {}

    def handler(payload: dict[str, Any]) -> None:
        current = store.get(task.task_id)
        observed["status_during_run"] = current.status if current else None

    worker = TaskWorker(store, handler)
    result = worker.process_next()
    assert result is not None
    assert observed["status_during_run"] == TaskStatus.RUNNING
    assert result.status == TaskStatus.DONE
    assert result.attempts == 0
    stored = store.get(task.task_id)
    assert stored is not None
    assert stored.status == TaskStatus.DONE


def test_failure_requeues_then_succeeds_on_retry() -> None:
    store = InMemoryTaskStore()
    task = make_task(max_attempts=3)
    store.enqueue(task)
    calls = {"n": 0}

    def handler(payload: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("chwilowy błąd")

    worker = TaskWorker(store, handler)
    first = worker.process_next()
    assert first is not None
    assert first.status == TaskStatus.PENDING
    assert first.attempts == 1
    second = worker.process_next()
    assert second is not None
    assert second.status == TaskStatus.DONE
    assert second.attempts == 1


def test_exhausted_attempts_escalate_with_feed_comment() -> None:
    store = InMemoryTaskStore()
    task = make_task(max_attempts=2)
    store.enqueue(task)

    def handler(payload: dict[str, Any]) -> None:
        raise RuntimeError("zawsze się psuje")

    worker = TaskWorker(store, handler)
    worker.process_next()
    final = worker.process_next()
    assert final is not None
    assert final.status == TaskStatus.ESCALATED
    assert final.attempts == 2
    comments = store.list_feed_comments(task.task_id)
    assert len(comments) == 1
    assert comments[0].author == "task-worker"
    assert "Escalacja" in comments[0].body
    assert "TASK_EXECUTION_FAILED" in comments[0].body


def test_empty_queue_returns_none() -> None:
    store = InMemoryTaskStore()
    worker = TaskWorker(store, lambda payload: None)
    assert worker.process_next() is None


def test_build_event_matches_frozen_events_schema() -> None:
    event = build_event("task.escalated.v1", {"task_id": "t-1"})
    assert set(event.keys()) == {"event_id", "event_type", "occurred_at", "payload"}
    assert EVENT_TYPE_RE.match(event["event_type"]) is not None
    assert event["payload"] == {"task_id": "t-1"}

"""Testy jednostkowe workera zadań (CHAT-0002).

AC#1: end-to-end pending -> running -> done bez błędów w logach.
AC#2: błąd wykonania -> retry wg polityki; wyczerpanie prób -> ESCALATED + komentarz w feedzie.
Pobór zadania jest wyłączny (brak podwójnego claim przez dwa workery).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from engine.task_worker import RetryPolicy, Task, TaskStatus, TaskWorker


class InMemoryTaskRepository:
    """In-memory fake repozytorium zadań."""

    def __init__(self, tasks: list[Task]) -> None:
        self._tasks: dict[str, Task] = {task.id: task for task in tasks}

    def claim_next_pending(self, worker_id: str) -> Task | None:
        for task in self._tasks.values():
            if task.status is TaskStatus.PENDING:
                task.status = TaskStatus.RUNNING
                task.claimed_by = worker_id
                task.attempts += 1
                return task
        return None

    def save(self, task: Task) -> None:
        self._tasks[task.id] = task

    def get(self, task_id: str) -> Task:
        return self._tasks[task_id]

    def all(self) -> list[Task]:
        return list(self._tasks.values())


class InMemoryFeed:
    """In-memory fake feedu komentarzy zadania."""

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    def post(self, task_id: str, message: str) -> None:
        self.comments.append((task_id, message))


def _make_task(task_id: str = "CHAT-0001") -> Task:
    return Task(id=task_id, title="Zadanie testowe", status=TaskStatus.PENDING)


def _make_worker(
    repo: InMemoryTaskRepository,
    feed: InMemoryFeed,
    executor: Callable[[Task], str],
    max_retries: int = 3,
) -> TaskWorker:
    return TaskWorker(
        repository=repo,
        feed=feed,
        executor=executor,
        retry_policy=RetryPolicy(max_retries=max_retries),
        worker_id="worker-test",
    )


def test_end_to_end_pending_running_done(caplog: pytest.LogCaptureFixture) -> None:
    """AC#1: pending -> running -> done, bez błędów w logach."""
    task = _make_task()
    repo = InMemoryTaskRepository([task])
    feed = InMemoryFeed()
    seen: list[TaskStatus] = []

    def executor(t: Task) -> str:
        seen.append(t.status)
        return "ok"

    worker = _make_worker(repo, feed, executor)
    with caplog.at_level(logging.INFO):
        result = worker.run_once()

    assert result is not None and result.id == task.id
    assert seen == [TaskStatus.RUNNING]
    final = repo.get(task.id)
    assert final.status is TaskStatus.DONE
    assert final.attempts == 1
    assert final.claimed_by == "worker-test"
    assert feed.comments == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_run_once_without_pending_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    """Brak zadań pending -> no-op bez błędów."""
    repo = InMemoryTaskRepository([])
    feed = InMemoryFeed()
    worker = _make_worker(repo, feed, lambda t: "ok")
    with caplog.at_level(logging.INFO):
        assert worker.run_once() is None
    assert repo.all() == []
    assert feed.comments == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_claim_is_exclusive_between_workers() -> None:
    """Dwa workery nie mogą pobrać tego samego zadania."""
    task = _make_task()
    repo = InMemoryTaskRepository([task])
    feed = InMemoryFeed()
    worker_a = _make_worker(repo, feed, lambda t: "ok")
    worker_b = _make_worker(repo, feed, lambda t: "ok")

    assert worker_a.run_once() is not None
    assert worker_b.run_once() is None
    assert repo.get(task.id).status is TaskStatus.DONE


def test_execution_error_retries_then_succeeds() -> None:
    """AC#2: błąd -> zadanie wraca do kolejki (retry), kolejna próba -> done."""
    task = _make_task()
    repo = InMemoryTaskRepository([task])
    feed = InMemoryFeed()
    calls: int = 0

    def flaky(t: Task) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return "ok"

    worker = _make_worker(repo, feed, flaky, max_retries=3)

    assert worker.run_once() is not None
    retried = repo.get(task.id)
    assert retried.status is TaskStatus.PENDING
    assert retried.attempts == 1

    assert worker.run_once() is not None
    final = repo.get(task.id)
    assert final.status is TaskStatus.DONE
    assert final.attempts == 2
    assert feed.comments == []


def test_exhausted_retries_escalates_with_feed_comment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC#2: wyczerpanie prób -> ESCALATED + komentarz w feedzie."""
    task = _make_task()
    repo = InMemoryTaskRepository([task])
    feed = InMemoryFeed()

    def always_fail(t: Task) -> str:
        raise RuntimeError("boom")

    worker = _make_worker(repo, feed, always_fail, max_retries=2)

    worker.run_once()
    assert repo.get(task.id).status is TaskStatus.PENDING

    with caplog.at_level(logging.INFO):
        worker.run_once()
    final = repo.get(task.id)
    assert final.status is TaskStatus.ESCALATED
    assert final.attempts == 2
    assert len(feed.comments) == 1
    task_id, message = feed.comments[0]
    assert task_id == task.id
    assert task.id in message

    # Zadanie ESCALATED nie jest ponownie pobierane.
    assert worker.run_once() is None

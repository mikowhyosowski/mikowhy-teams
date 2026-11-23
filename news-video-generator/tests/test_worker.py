from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from news_video_generator.tasks.inmemory import (
    InMemoryFeedRepository,
    InMemoryTaskRepository,
)
from news_video_generator.tasks.models import RetryPolicy, Task, TaskStatus
from news_video_generator.tasks.worker import TaskWorker


def _task(created_at: datetime | None = None) -> Task:
    task = Task(task_id=uuid.uuid4(), payload={'kind': 'test'})
    if created_at is not None:
        task.created_at = created_at
    return task


def test_happy_path_pending_running_done() -> None:
    '''AC#1: a single run_once drives the task pending -> running -> done.'''
    # Scenario in one coroutine: deterministic via asyncio.run, no sleeps.
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()
    task = repo.enqueue(_task())
    observed: list[TaskStatus] = []

    async def handler(t: Task) -> None:
        observed.append(t.status)

    async def scenario() -> None:
        worker = TaskWorker(repo, feed, handler)
        assert await worker.run_once() is True

    asyncio.run(scenario())
    assert observed == [TaskStatus.RUNNING]
    assert task.status is TaskStatus.DONE
    assert task.attempts == 1
    assert task.last_error is None
    assert feed.comments == []


def test_failure_is_requeued_for_retry() -> None:
    '''AC#2a: a handler error requeues the task as pending, never done.'''
    # Single-coroutine scenario keeps the assertion on requeue deterministic.
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()
    task = repo.enqueue(_task())

    async def failing(t: Task) -> None:
        raise RuntimeError('boom')

    async def scenario() -> None:
        worker = TaskWorker(repo, feed, failing, retry_policy=RetryPolicy(max_attempts=3))
        assert await worker.run_once() is True

    asyncio.run(scenario())
    assert task.status is TaskStatus.PENDING
    assert task.attempts == 1
    assert task.last_error is not None
    assert 'boom' in task.last_error
    assert feed.comments == []


def test_exhaustion_marks_escalated_with_feed_comment() -> None:
    '''AC#2b: after max_attempts the task is ESCALATED and the feed has a comment.'''
    # Loop of run_once calls mirrors real polling without wall-clock sleeps.
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()
    task = repo.enqueue(_task())
    calls = 0

    async def failing(t: Task) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError('boom')

    async def scenario() -> None:
        worker = TaskWorker(repo, feed, failing, retry_policy=RetryPolicy(max_attempts=3))
        for _ in range(3):
            assert await worker.run_once() is True

    asyncio.run(scenario())
    assert calls == 3
    assert task.status is TaskStatus.ESCALATED
    assert task.attempts == 3
    assert len(feed.comments) == 1
    comment = feed.comments[0]
    assert comment['task_id'] == str(task.task_id)
    assert 'boom' in comment['body']
    assert '3 attempts' in comment['body']


def test_transient_failure_recovers_on_retry() -> None:
    '''AC#2c: a transient error succeeds on the next attempt -> done.'''
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()
    task = repo.enqueue(_task())
    calls = 0

    async def flaky(t: Task) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError('transient')

    async def scenario() -> None:
        worker = TaskWorker(repo, feed, flaky, retry_policy=RetryPolicy(max_attempts=3))
        assert await worker.run_once() is True
        assert task.status is TaskStatus.PENDING
        assert await worker.run_once() is True

    asyncio.run(scenario())
    assert calls == 2
    assert task.status is TaskStatus.DONE
    assert feed.comments == []


def test_run_once_on_empty_queue_returns_false() -> None:
    '''Edge: nothing to claim -> run_once returns False without side effects.'''
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()

    async def handler(t: Task) -> None:
        return None

    async def scenario() -> None:
        worker = TaskWorker(repo, feed, handler)
        assert await worker.run_once() is False

    asyncio.run(scenario())
    assert feed.comments == []


def test_oldest_pending_task_is_claimed_first() -> None:
    '''Edge: FIFO claim order is deterministic (oldest created_at first).'''
    # Explicit created_at values remove clock dependence from the ordering.
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    first = repo.enqueue(_task(created_at=base))
    second = repo.enqueue(_task(created_at=base + timedelta(seconds=1)))
    processed: list[uuid.UUID] = []

    async def handler(t: Task) -> None:
        processed.append(t.task_id)

    async def scenario() -> None:
        worker = TaskWorker(repo, feed, handler)
        await worker.run_once()
        await worker.run_once()

    asyncio.run(scenario())
    assert processed == [first.task_id, second.task_id]
    assert first.status is TaskStatus.DONE
    assert second.status is TaskStatus.DONE

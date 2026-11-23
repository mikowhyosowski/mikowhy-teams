from __future__ import annotations

import asyncio
import uuid

from news_video_generator.tasks.inmemory import (
    InMemoryFeedRepository,
    InMemoryTaskRepository,
)
from news_video_generator.tasks.models import RetryPolicy, Task, TaskStatus
from news_video_generator.tasks.worker import TaskWorker


def test_end_to_end_pending_running_done_via_run_forever() -> None:
    '''AC#1 integration: run_forever drains the queue and closes the task.'''
    # poll_interval=0 keeps the loop purely cooperative (no wall-clock sleeps),
    # so the test is deterministic and fast while still exercising run_forever.
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()
    task = repo.enqueue(Task(task_id=uuid.uuid4(), payload={'kind': 'render'}))

    async def handler(t: Task) -> None:
        return None

    async def scenario() -> None:
        stop = asyncio.Event()
        worker = TaskWorker(repo, feed, handler, poll_interval_seconds=0.0)

        async def stop_when_done() -> None:
            current = repo.get(task.task_id)
            while current is None or current.status is not TaskStatus.DONE:
                await asyncio.sleep(0)
                current = repo.get(task.task_id)
            stop.set()

        watcher = asyncio.create_task(stop_when_done())
        await asyncio.wait_for(worker.run_forever(stop), timeout=5.0)
        await watcher

    asyncio.run(scenario())
    final = repo.get(task.task_id)
    assert final is not None
    assert final.status is TaskStatus.DONE
    assert final.attempts == 1
    assert feed.comments == []


def test_end_to_end_escalation_after_retry_exhaustion() -> None:
    '''AC#2 integration: persistent failure ends ESCALATED with a feed comment.'''
    # Same cooperative-loop approach as the happy-path test; wait_for is only
    # a hang guard, the outcome does not depend on timing.
    repo = InMemoryTaskRepository()
    feed = InMemoryFeedRepository()
    task = repo.enqueue(Task(task_id=uuid.uuid4(), payload={'kind': 'render'}))
    attempts = 0

    async def always_failing(t: Task) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError('permanent')

    async def scenario() -> None:
        stop = asyncio.Event()
        worker = TaskWorker(
            repo,
            feed,
            always_failing,
            retry_policy=RetryPolicy(max_attempts=2),
            poll_interval_seconds=0.0,
        )

        async def stop_when_escalated() -> None:
            current = repo.get(task.task_id)
            while current is None or current.status is not TaskStatus.ESCALATED:
                await asyncio.sleep(0)
                current = repo.get(task.task_id)
            stop.set()

        watcher = asyncio.create_task(stop_when_escalated())
        await asyncio.wait_for(worker.run_forever(stop), timeout=5.0)
        await watcher

    asyncio.run(scenario())
    assert attempts == 2
    final = repo.get(task.task_id)
    assert final is not None
    assert final.status is TaskStatus.ESCALATED
    assert len(feed.comments) == 1
    assert feed.comments[0]['task_id'] == str(task.task_id)

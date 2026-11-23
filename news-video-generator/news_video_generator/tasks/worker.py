from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from .models import RetryPolicy, Task, TaskStatus
from .repository import FeedRepository, TaskRepository

logger = logging.getLogger(__name__)

TaskHandler = Callable[[Task], Awaitable[None]]


class TaskWorker:
    '''Polls the queue and drives tasks through pending -> running -> done.

    A handler failure requeues the task (pending) until the retry policy is
    exhausted; the final failure marks the task ESCALATED and appends a
    comment to the task feed. Claiming increments attempts, so exhaustion
    is detected exactly at max_attempts.
    '''

    def __init__(
        self,
        repository: TaskRepository,
        feed: FeedRepository,
        handler: TaskHandler,
        *,
        retry_policy: RetryPolicy | None = None,
        worker_id: str | None = None,
        poll_interval_seconds: float = 0.1,
    ) -> None:
        self._repository = repository
        self._feed = feed
        self._handler = handler
        self._retry_policy = retry_policy or RetryPolicy()
        self._worker_id = worker_id or f'worker-{uuid.uuid4()}'
        self._poll_interval_seconds = poll_interval_seconds
        self._running = False

    async def run_once(self) -> bool:
        '''Claim and execute at most one task; True when a task was processed.'''
        task = self._repository.claim_next_pending(self._worker_id)
        if task is None:
            return False
        await self._process(task)
        return True

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        '''Poll until stop() is called or stop_event is set on an empty poll.'''
        self._running = True
        while self._running:
            processed = await self.run_once()
            if not processed:
                if stop_event is not None and stop_event.is_set():
                    break
                await asyncio.sleep(self._poll_interval_seconds)

    def stop(self) -> None:
        self._running = False

    async def _process(self, task: Task) -> None:
        try:
            await self._handler(task)
        except Exception as exc:
            self._handle_failure(task, exc)
        else:
            self._handle_success(task)

    def _handle_success(self, task: Task) -> None:
        task.status = TaskStatus.DONE
        task.last_error = None
        task.updated_at = datetime.now(timezone.utc)
        self._repository.save(task)
        logger.info(
            'task.done',
            extra={'task_id': str(task.task_id), 'attempts': task.attempts},
        )

    def _handle_failure(self, task: Task, exc: Exception) -> None:
        task.last_error = f'{type(exc).__name__}: {exc}'
        task.updated_at = datetime.now(timezone.utc)
        if task.attempts >= self._retry_policy.max_attempts:
            task.status = TaskStatus.ESCALATED
            self._repository.save(task)
            self._feed.add_comment(
                task.task_id,
                author=self._worker_id,
                body=(
                    f'Task escalated after {task.attempts} attempts. '
                    f'Last error: {task.last_error}'
                ),
            )
            logger.error(
                'task.escalated',
                extra={'task_id': str(task.task_id), 'attempts': task.attempts},
            )
        else:
            task.status = TaskStatus.PENDING
            self._repository.save(task)
            logger.warning(
                'task.retry',
                extra={'task_id': str(task.task_id), 'attempts': task.attempts},
            )

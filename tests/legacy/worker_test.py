'''Testy jednostkowe workera zadań (CHAT-0002).

Kontrakt workera:
- pobiera zadanie PENDING, oznacza RUNNING, wykonuje i zamyka DONE,
- błąd wykonania -> zadanie wraca do PENDING, attempts rośnie (retry wg polityki),
- wyczerpanie prób (attempts == max_attempts) -> ESCALATED + komentarz w feedzie.
'''
import logging

import pytest

from news_video_generator.worker import (
    RetryPolicy,
    Task,
    TaskStatus,
    TaskWorker,
)


class InMemoryTaskStore:
    '''Fixture: repozytorium zadań w pamięci (wyłącznie na potrzeby testów).'''

    def __init__(self):
        self.tasks = {}
        self.feed = []

    def add(self, task):
        self.tasks[task.task_id] = task

    def get(self, task_id):
        return self.tasks.get(task_id)

    def next_pending(self):
        for task in self.tasks.values():
            if task.status == TaskStatus.PENDING:
                return task
        return None

    def save(self, task):
        self.tasks[task.task_id] = task

    def add_feed_comment(self, task_id, message):
        self.feed.append({'task_id': task_id, 'message': message})


@pytest.fixture
def store():
    return InMemoryTaskStore()


@pytest.fixture
def retry_policy():
    return RetryPolicy(max_attempts=3, backoff_seconds=0)


@pytest.fixture
def worker(store, retry_policy):
    return TaskWorker(store=store, retry_policy=retry_policy)


def make_task(task_id='task-001', payload=None):
    return Task(
        task_id=task_id,
        payload=payload or {'job': 'render'},
        status=TaskStatus.PENDING,
        attempts=0,
    )


def test_happy_path_pending_running_done(store, worker, caplog):
    store.add(make_task())
    with caplog.at_level(logging.INFO):
        processed = worker.process_next()
    assert processed is not None
    task = store.get('task-001')
    assert task.status == TaskStatus.DONE
    assert task.attempts == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []


def test_execution_error_returns_task_to_pending_with_attempt(store, worker):
    store.add(make_task())

    def failing_handler(task):
        raise RuntimeError('render failed')

    worker.process_next(handler=failing_handler)
    task = store.get('task-001')
    assert task.status == TaskStatus.PENDING
    assert task.attempts == 1


def test_retry_eventually_succeeds(store, worker):
    store.add(make_task())
    calls = {'n': 0}

    def flaky_handler(task):
        calls['n'] += 1
        if calls['n'] < 2:
            raise RuntimeError('transient error')

    worker.process_next(handler=flaky_handler)
    worker.process_next(handler=flaky_handler)
    task = store.get('task-001')
    assert task.status == TaskStatus.DONE
    assert calls['n'] == 2


def test_exhausted_attempts_escalate_with_feed_comment(store, worker):
    store.add(make_task())

    def always_failing(task):
        raise RuntimeError('permanent failure')

    for _ in range(3):
        worker.process_next(handler=always_failing)

    task = store.get('task-001')
    assert task.status == TaskStatus.ESCALATED
    assert task.attempts == 3
    comments = [c for c in store.feed if c['task_id'] == 'task-001']
    assert len(comments) == 1


def test_done_task_is_not_reexecuted(store, worker):
    store.add(make_task())
    worker.process_next()
    calls = {'n': 0}

    def handler(task):
        calls['n'] += 1

    assert worker.process_next(handler=handler) is None
    assert calls['n'] == 0


def test_no_pending_task_is_noop(worker):
    assert worker.process_next() is None
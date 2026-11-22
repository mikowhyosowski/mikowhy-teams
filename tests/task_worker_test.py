'''Testy workera zadań (CHAT-0002): e2e pending -> running -> done oraz scenariusze brzegowe (retry, ESCALATED).'''
from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from task_worker import RetryPolicy, Task, TaskStatus, TaskWorker


class FakeRepository:
    '''In-memory fixture kontraktu repozytorium zadań (tylko w testach).'''

    def __init__(self) -> None:
        self.tasks: dict[str, Task] = {}
        self.transitions: list[tuple[str, TaskStatus]] = []

    def enqueue(self, task: Task) -> None:
        self.tasks[task.id] = task

    def claim_next_pending(self) -> Task | None:
        for task in self.tasks.values():
            if task.status == TaskStatus.PENDING:
                return task
        return None

    def save(self, task: Task) -> None:
        self.tasks[task.id] = task
        self.transitions.append((task.id, task.status))

    def get(self, task_id: str) -> Task:
        return self.tasks[task_id]


class FakeFeed:
    '''In-memory fixture feedu komentarzy (tylko w testach).'''

    def __init__(self) -> None:
        self.comments: list[tuple[str, str]] = []

    def add_comment(self, task_id: str, text: str) -> None:
        self.comments.append((task_id, text))


def _task(task_id: str = 'task-1') -> Task:
    return Task(id=task_id, status=TaskStatus.PENDING, attempts=0, payload={'kind': 'test'})


def _worker(
    repo: FakeRepository,
    feed: FakeFeed,
    handler: Callable[[Task], None],
    max_attempts: int = 3,
) -> TaskWorker:
    return TaskWorker(repo=repo, feed=feed, handler=handler, policy=RetryPolicy(max_attempts=max_attempts))


def test_end_to_end_pending_running_done_bez_bledow_w_logach(caplog: pytest.LogCaptureFixture) -> None:
    repo = FakeRepository()
    feed = FakeFeed()
    wykonane: list[str] = []

    def handler(task: Task) -> None:
        wykonane.append(task.id)

    repo.enqueue(_task())
    worker = _worker(repo, feed, handler)

    with caplog.at_level(logging.INFO):
        assert worker.run_once() is True

    task = repo.get('task-1')
    assert wykonane == ['task-1']
    assert task.status == TaskStatus.DONE
    assert task.attempts == 1
    assert repo.transitions == [('task-1', TaskStatus.RUNNING), ('task-1', TaskStatus.DONE)]
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_brak_zadania_pending_is_noop(caplog: pytest.LogCaptureFixture) -> None:
    repo = FakeRepository()
    feed = FakeFeed()
    worker = _worker(repo, feed, lambda t: None)

    with caplog.at_level(logging.INFO):
        assert worker.run_once() is False

    assert repo.transitions == []
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


def test_blad_wykonania_retry_i_kolejna_proba_konczy_done() -> None:
    repo = FakeRepository()
    feed = FakeFeed()
    repo.enqueue(_task())
    state = {'fail': True}

    def handler(task: Task) -> None:
        if state['fail']:
            state['fail'] = False
            raise RuntimeError('boom')

    worker = _worker(repo, feed, handler, max_attempts=3)

    assert worker.run_once() is True
    task = repo.get('task-1')
    assert task.status == TaskStatus.PENDING
    assert task.attempts == 1

    assert worker.run_once() is True
    task = repo.get('task-1')
    assert task.status == TaskStatus.DONE
    assert task.attempts == 2
    assert repo.transitions == [
        ('task-1', TaskStatus.RUNNING),
        ('task-1', TaskStatus.PENDING),
        ('task-1', TaskStatus.RUNNING),
        ('task-1', TaskStatus.DONE),
    ]


def test_wyczerpanie_prob_escalated_i_komentarz_w_feedzie() -> None:
    repo = FakeRepository()
    feed = FakeFeed()
    repo.enqueue(_task())

    def handler(task: Task) -> None:
        raise RuntimeError('zawsze pada')

    worker = _worker(repo, feed, handler, max_attempts=2)

    assert worker.run_once() is True
    assert worker.run_once() is True
    assert worker.run_once() is False

    task = repo.get('task-1')
    assert task.status == TaskStatus.ESCALATED
    assert task.attempts == 2
    assert len(feed.comments) == 1
    task_id, text = feed.comments[0]
    assert task_id == 'task-1'
    assert 'escalated' in text.lower()
    assert repo.transitions[-1] == ('task-1', TaskStatus.ESCALATED)

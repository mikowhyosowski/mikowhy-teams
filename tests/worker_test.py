'''Testy jednostkowe workera zadań (CHAT-0002).'''
import logging

from engine.worker import TaskStatus, TaskWorker


class FakeTask:
    def __init__(self, task_id, task_type='demo', payload=None, max_attempts=3):
        self.id = task_id
        self.type = task_type
        self.payload = payload or {}
        self.status = TaskStatus.PENDING
        self.attempts = 0
        self.max_attempts = max_attempts


class InMemoryTaskRepo:
    def __init__(self, tasks=()):
        self._tasks = {t.id: t for t in tasks}

    def claim_next_pending(self):
        for task in self._tasks.values():
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.RUNNING
                return task
        return None

    def save(self, task):
        self._tasks[task.id] = task

    def get(self, task_id):
        return self._tasks[task_id]


class RecordingFeed:
    def __init__(self):
        self.comments = []

    def add_comment(self, task_id, text):
        self.comments.append({'task_id': task_id, 'text': text})


def make_worker(tasks=(), handler=None):
    repo = InMemoryTaskRepo(tasks)
    feed = RecordingFeed()
    worker = TaskWorker(repo=repo, feed=feed, handler=handler or (lambda task: None))
    return worker, repo, feed


def test_claim_pending_task_transitions_pending_to_running():
    worker, _, _ = make_worker([FakeTask('t-1')])
    task = worker.poll()
    assert task is not None
    assert task.id == 't-1'
    assert task.status == TaskStatus.RUNNING


def test_successful_execution_closes_task_as_done():
    worker, repo, _ = make_worker([FakeTask('t-1')])
    worker.run_once()
    assert repo.get('t-1').status == TaskStatus.DONE
    assert repo.get('t-1').attempts == 1


def test_poll_returns_none_when_no_pending_tasks():
    worker, _, _ = make_worker([])
    assert worker.poll() is None


def test_execution_error_retries_according_to_policy():
    def failing(task):
        raise RuntimeError('boom')

    worker, repo, feed = make_worker([FakeTask('t-1', max_attempts=3)], handler=failing)
    worker.run_once()
    task = repo.get('t-1')
    assert task.status == TaskStatus.PENDING
    assert task.attempts == 1
    assert feed.comments == []


def test_exhausted_attempts_set_escalated_with_feed_comment():
    def failing(task):
        raise RuntimeError('boom')

    worker, repo, feed = make_worker([FakeTask('t-1', max_attempts=2)], handler=failing)
    worker.run_once()
    worker.run_once()
    assert repo.get('t-1').status == TaskStatus.ESCALATED
    assert len(feed.comments) == 1
    assert feed.comments[0]['task_id'] == 't-1'


def test_happy_path_produces_no_error_logs(caplog):
    worker, repo, _ = make_worker([FakeTask('t-1')])
    with caplog.at_level(logging.DEBUG):
        worker.run_once()
    assert repo.get('t-1').status == TaskStatus.DONE
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []

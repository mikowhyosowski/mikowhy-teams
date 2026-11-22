'''Testy integracyjne end-to-end workera zadań (CHAT-0002).'''
import logging

from engine.worker import TaskStatus, TaskWorker


class FakeTask:
    def __init__(self, task_id, payload=None, max_attempts=3):
        self.id = task_id
        self.payload = payload or {}
        self.status = TaskStatus.PENDING
        self.attempts = 0
        self.max_attempts = max_attempts


class InMemoryTaskRepo:
    def __init__(self, tasks=()):
        self._tasks = {t.id: t for t in tasks}
        self.history = []

    def claim_next_pending(self):
        for task in self._tasks.values():
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.RUNNING
                self.history.append((task.id, task.status))
                return task
        return None

    def save(self, task):
        self.history.append((task.id, task.status))
        self._tasks[task.id] = task

    def get(self, task_id):
        return self._tasks[task_id]


class RecordingFeed:
    def __init__(self):
        self.comments = []

    def add_comment(self, task_id, text):
        self.comments.append({'task_id': task_id, 'text': text})


def build(tasks, handler):
    repo = InMemoryTaskRepo(tasks)
    feed = RecordingFeed()
    worker = TaskWorker(repo=repo, feed=feed, handler=handler)
    return worker, repo, feed


def test_pending_running_done_end_to_end_without_error_logs(caplog):
    worker, repo, _ = build([FakeTask('e2e-1')], lambda task: None)
    with caplog.at_level(logging.DEBUG):
        worker.run_once()
    statuses = [status for _, status in repo.history]
    assert TaskStatus.RUNNING in statuses
    assert statuses[-1] == TaskStatus.DONE
    assert repo.get('e2e-1').attempts == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []


def test_error_then_retry_then_done_end_to_end():
    state = {'calls': 0}

    def flaky(task):
        state['calls'] += 1
        if state['calls'] < 2:
            raise RuntimeError('temporary failure')

    worker, repo, feed = build([FakeTask('e2e-2', max_attempts=3)], flaky)
    worker.run_once()
    assert repo.get('e2e-2').status == TaskStatus.PENDING
    assert repo.get('e2e-2').attempts == 1
    worker.run_once()
    assert repo.get('e2e-2').status == TaskStatus.DONE
    assert feed.comments == []


def test_retry_exhaustion_escalates_with_feed_comment_end_to_end():
    def always_fail(task):
        raise RuntimeError('permanent failure')

    worker, repo, feed = build([FakeTask('e2e-3', max_attempts=2)], always_fail)
    worker.run_once()
    worker.run_once()
    task = repo.get('e2e-3')
    assert task.status == TaskStatus.ESCALATED
    assert task.attempts == 2
    assert len(feed.comments) == 1
    assert feed.comments[0]['task_id'] == 'e2e-3'

'''Test integracyjny end-to-end workera zadań (CHAT-0002).

Scenariusze:
- pełna pętla pending -> running -> done bez błędów w logach,
- błąd -> retry -> wyczerpanie prób -> ESCALATED z komentarzem w feedzie.
'''
import logging

from news_video_generator.worker import RetryPolicy, Task, TaskStatus, TaskWorker


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


def build_worker(store, max_attempts=3):
    policy = RetryPolicy(max_attempts=max_attempts, backoff_seconds=0)
    return TaskWorker(store=store, retry_policy=policy)


def test_end_to_end_lifecycle_done_without_log_errors(caplog):
    store = InMemoryTaskStore()
    worker = build_worker(store)
    store.add(Task(task_id='e2e-1', payload={'job': 'render'}, status=TaskStatus.PENDING, attempts=0))
    with caplog.at_level(logging.INFO):
        worker.run()
    task = store.get('e2e-1')
    assert task.status == TaskStatus.DONE
    assert task.attempts == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []


def test_end_to_end_retry_exhaustion_escalates_with_feed_comment(caplog):
    store = InMemoryTaskStore()
    worker = build_worker(store, max_attempts=2)
    store.add(Task(task_id='e2e-2', payload={'job': 'render'}, status=TaskStatus.PENDING, attempts=0))

    def always_failing(task):
        raise RuntimeError('boom')

    with caplog.at_level(logging.INFO):
        worker.run(handler=always_failing)
    task = store.get('e2e-2')
    assert task.status == TaskStatus.ESCALATED
    assert task.attempts == 2
    assert any(c['task_id'] == 'e2e-2' for c in store.feed)
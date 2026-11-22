import uuid
from datetime import datetime, timezone

import pytest

from worker.task_worker import TaskWorker, TaskStatus
from worker.repository import InMemoryTaskRepository
from worker.feed import InMemoryFeed


def make_task(payload=None, max_retries=3):
    return {
        "task_id": str(uuid.uuid4()),
        "payload": payload or {"kind": "test", "value": 42},
        "status": TaskStatus.PENDING.value,
        "attempts": 0,
        "max_retries": max_retries,
    }


@pytest.fixture
def repo():
    return InMemoryTaskRepository()


@pytest.fixture
def feed():
    return InMemoryFeed()


@pytest.fixture
def worker(repo, feed):
    return TaskWorker(repo=repo, feed=feed, poll_interval=0)


def test_worker_processes_task_end_to_end(worker, repo, feed):
    task = make_task()
    repo.enqueue(task)

    result = worker.run_once()

    assert result["processed"] == 1
    stored = repo.get(task["task_id"])
    assert stored["status"] == TaskStatus.DONE.value
    assert stored["attempts"] == 1
    assert "error" not in stored


def test_worker_marks_running_before_execution(worker, repo):
    observed = []

    def handler(payload):
        observed.append(repo.get_current_status())
        return {"ok": True}

    worker.register_handler("test", handler)
    task = make_task()
    repo.enqueue(task)
    worker.run_once()

    assert observed == [TaskStatus.RUNNING.value]
    assert repo.get(task["task_id"])["status"] == TaskStatus.DONE.value


def test_worker_retries_on_failure_according_to_policy(worker, repo):
    calls = []

    def failing_handler(payload):
        calls.append(1)
        raise RuntimeError("boom")

    worker.register_handler("test", failing_handler)
    task = make_task(max_retries=3)
    repo.enqueue(task)

    worker.run_once()
    stored = repo.get(task["task_id"])
    assert stored["status"] == TaskStatus.PENDING.value
    assert stored["attempts"] == 1
    assert stored["last_error"] == "boom"

    worker.run_once()
    worker.run_once()
    stored = repo.get(task["task_id"])
    assert stored["status"] == TaskStatus.PENDING.value
    assert stored["attempts"] == 3
    assert len(calls) == 3


def test_worker_escalates_after_retries_exhausted(worker, repo, feed):
    def failing_handler(payload):
        raise ValueError("permanent failure")

    worker.register_handler("test", failing_handler)
    task = make_task(max_retries=2)
    repo.enqueue(task)

    worker.run_once()
    worker.run_once()
    stored = repo.get(task["task_id"])
    assert stored["status"] == TaskStatus.ESCALATED.value
    assert stored["attempts"] == 2

    comments = feed.comments_for(task["task_id"])
    assert len(comments) == 1
    assert "permanent failure" in comments[0]["text"]
    assert comments[0]["author"] == "task-worker"


def test_worker_does_not_retry_successful_task(worker, repo):
    worker.register_handler("test", lambda payload: {"ok": True})
    task = make_task()
    repo.enqueue(task)
    worker.run_once()
    worker.run_once()
    assert repo.get(task["task_id"])["attempts"] == 1


def test_worker_claims_only_pending_tasks(worker, repo):
    done_task = make_task()
    done_task["status"] = TaskStatus.DONE.value
    pending_task = make_task()
    repo.enqueue(done_task)
    repo.enqueue(pending_task)

    result = worker.run_once()

    assert result["processed"] == 1
    assert repo.get(done_task["task_id"])["status"] == TaskStatus.DONE.value
    assert repo.get(pending_task["task_id"])["status"] == TaskStatus.DONE.value


def test_worker_is_idempotent_on_requeue(worker, repo):
    worker.register_handler("test", lambda payload: {"ok": True})
    task = make_task()
    repo.enqueue(task)
    worker.run_once()
    # requeue of an already-done task must not re-execute it
    worker.run_once()
    assert repo.get(task["task_id"])["attempts"] == 1


def test_worker_handles_unknown_task_kind(worker, repo, feed):
    task = make_task(payload={"kind": "unknown_kind"})
    repo.enqueue(task)
    worker.run_once()
    stored = repo.get(task["task_id"])
    assert stored["status"] == TaskStatus.ESCALATED.value
    assert "unknown" in stored["last_error"].lower()


def test_event_timestamps_are_iso8601_utc(worker, repo):
    worker.register_handler("test", lambda payload: {"ok": True})
    task = make_task()
    repo.enqueue(task)
    worker.run_once()
    stored = repo.get(task["task_id"])
    ts = datetime.fromisoformat(stored["finished_at"].replace("Z", "+00:00"))
    assert ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == 0

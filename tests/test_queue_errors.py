"""Deterministic tests for the queue repair loop (no real sleeps, no randomness)."""
from __future__ import annotations

import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from engine.errors import (
    ErrorClass,
    OwnerDecisionError,
    QueueError,
    ScopeViolationError,
    StepFailedError,
    TransientQueueError,
    classify_exception,
    error_event,
    should_escalate,
)
from engine.queue import InMemoryIdempotencyStore, RetryPolicy, TaskQueue


class FakeSleeper:
    """Records sleep durations instead of sleeping — keeps tests deterministic."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_queue(max_retries: int = 3, base: float = 1.0, cap: float = 8.0) -> tuple[TaskQueue, FakeSleeper]:
    policy = RetryPolicy(
        max_retries={
            ErrorClass.TRANSIENT: max_retries,
            ErrorClass.STEP_FAILED: max_retries,
            ErrorClass.SCOPE: max_retries,
            ErrorClass.MISSING_FILE: max_retries,
            ErrorClass.TEST_FAILED: max_retries,
            ErrorClass.OWNER_DECISION: 0,
        },
        base_backoff_s=base,
        max_backoff_s=cap,
    )
    sleeper = FakeSleeper()
    return TaskQueue(policy=policy, sleeper=sleeper), sleeper


def test_backoff_exponential_and_capped() -> None:
    queue, _ = make_queue(base=1.0, cap=8.0)
    assert [queue.backoff_s(i) for i in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_transient_error_auto_retried_then_succeeds() -> None:
    queue, sleeper = make_queue(max_retries=3, base=1.0, cap=100.0)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("llm timeout")
        return "ok"

    assert queue.execute_step("T-1", "2/5_IMPL", flaky) == "ok"
    assert calls["n"] == 3
    assert sleeper.calls == [1.0, 2.0]
    assert queue.dead_letters == []
    assert queue.escalations == []


def test_idempotency_key_prevents_double_execution() -> None:
    queue, _ = make_queue()
    queue.store = InMemoryIdempotencyStore()
    counter = {"n": 0}

    def work() -> int:
        counter["n"] += 1
        return counter["n"]

    assert queue.execute_step("T-2", "1/5_PLAN", work) == 1
    assert queue.execute_step("T-2", "1/5_PLAN", work) == 1
    assert queue.execute_step("T-2", "2/5_IMPL", work) == 2
    assert counter["n"] == 2
    assert TaskQueue.idempotency_key("T-2", "1/5_PLAN") == "T-2:1/5_PLAN"


def test_dead_letter_after_max_retries_no_escalation() -> None:
    queue, sleeper = make_queue(max_retries=2, base=0.5, cap=10.0)

    def always_down() -> None:
        raise ConnectionError("db unreachable")

    with pytest.raises(TransientQueueError):
        queue.execute_step("T-3", "3/5_TEST", always_down)
    assert len(queue.dead_letters) == 1
    assert queue.dead_letters[0].attempts == 3
    assert queue.dead_letters[0].code == "QUEUE_TRANSIENT"
    assert queue.escalations == []
    assert sleeper.calls == [0.5, 1.0]
    assert queue.emitted_events[-1]["event_type"] == "queue.dead_letter.v1"


def test_owner_decision_escalates_instead_of_dead_letter() -> None:
    queue, _ = make_queue()

    def conflict() -> None:
        raise OwnerDecisionError("contradictory business requirements", context={"options": ["A", "B"]})

    with pytest.raises(OwnerDecisionError):
        queue.execute_step("T-4", "1/5_PLAN", conflict)
    assert len(queue.escalations) == 1
    assert queue.dead_letters == []


def test_routine_errors_never_escalate() -> None:
    routine = (TimeoutError(), ConnectionError(), FileNotFoundError("x"), AssertionError("test"), RuntimeError("boom"))
    for exc in routine:
        assert not should_escalate(classify_exception(exc))


def test_classify_exception_mapping() -> None:
    assert classify_exception(TimeoutError()).error_class is ErrorClass.TRANSIENT
    assert classify_exception(ConnectionError()).error_class is ErrorClass.TRANSIENT
    assert classify_exception(FileNotFoundError()).error_class is ErrorClass.MISSING_FILE
    assert classify_exception(AssertionError("fail")).error_class is ErrorClass.TEST_FAILED
    assert classify_exception(RuntimeError()).error_class is ErrorClass.STEP_FAILED
    original = StepFailedError("x")
    assert classify_exception(original) is original


def test_scope_violation_auto_retried_not_escalated() -> None:
    queue, _ = make_queue(max_retries=2)
    attempts = {"n": 0}

    def scope_then_ok() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ScopeViolationError()
        return "fixed"

    assert queue.execute_step("T-5", "2/5_IMPL", scope_then_ok) == "fixed"
    assert queue.escalations == []


def test_error_event_matches_frozen_schema() -> None:
    event = error_event(TransientQueueError("x"), event_type="queue.error.v1")
    assert set(event) == {"event_id", "event_type", "occurred_at", "payload"}
    uuid.UUID(event["event_id"])
    assert re.fullmatch(r"^[a-z]+\.[a-z_]+\.v[0-9]+$", event["event_type"])
    parsed = datetime.fromisoformat(event["occurred_at"])
    assert parsed.tzinfo is not None
    assert isinstance(event["payload"], dict)


def test_invalid_event_type_rejected() -> None:
    with pytest.raises(ValueError):
        error_event(QueueError("C", "m", ErrorClass.STEP_FAILED), event_type="Bad Type")

"""Task queue with the automatic repair loop (Sponsor hard rule 2026-11-23 16:33).

Routine failures (timeout, missing file, out-of-scope file, failing test) are
handled by auto-retry with exponential backoff and a per-class retry limit —
they are NEVER escalated to the Owner. Only OWNER_DECISION errors escalate
(AWAITING_OWNER) with a problem package. Exhausted retries go to a dead-letter
queue instead of re-enqueueing forever. Step execution is idempotent: the
idempotency key is ``task_id:substep`` so a retried step never runs twice.

Assumption (self-check rule 6): the task's scope_paths was empty, so the scope
was auto-extended within the repo to the paths named in the approved plan
(src/engine/queue.py, src/engine/errors.py, tests/test_queue_errors.py).
"""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from engine.errors import (
    ErrorClass,
    QueueError,
    classify_exception,
    error_event,
    log_error,
    should_escalate,
)

DEFAULT_MAX_RETRIES: dict[ErrorClass, int] = {
    ErrorClass.TRANSIENT: 5,
    ErrorClass.STEP_FAILED: 3,
    ErrorClass.SCOPE: 2,
    ErrorClass.MISSING_FILE: 2,
    ErrorClass.TEST_FAILED: 3,
    ErrorClass.OWNER_DECISION: 0,
}


@dataclass(frozen=True)
class RetryPolicy:
    """Retry limits per error class plus backoff bounds — pure policy."""

    max_retries: Mapping[ErrorClass, int] = field(default_factory=lambda: dict(DEFAULT_MAX_RETRIES))
    base_backoff_s: float = 0.5
    max_backoff_s: float = 30.0


class Sleeper(Protocol):
    """Injectable sleep so tests stay deterministic (no real waiting)."""

    def __call__(self, seconds: float) -> None: ...


class IdempotencyStore(Protocol):
    """Storage for executed-step results, keyed by idempotency key."""

    def seen(self, key: str) -> bool: ...

    def mark(self, key: str, result: Any) -> None: ...

    def result(self, key: str) -> Any: ...


class InMemoryIdempotencyStore:
    """Process-local idempotency store keyed by ``task_id:substep``."""

    def __init__(self) -> None:
        self._results: dict[str, Any] = {}

    def seen(self, key: str) -> bool:
        return key in self._results

    def mark(self, key: str, result: Any) -> None:
        self._results[key] = result

    def result(self, key: str) -> Any:
        return self._results[key]


@dataclass(frozen=True)
class DeadLetter:
    """Terminal record for a step whose retries are exhausted (no re-enqueue)."""

    task_id: str
    substep: str
    code: str
    error_class: ErrorClass
    attempts: int
    occurred_at: str


class TaskQueue:
    """Executes steps idempotently with backoff retry and dead-lettering."""

    def __init__(
        self,
        *,
        policy: RetryPolicy | None = None,
        store: IdempotencyStore | None = None,
        sleeper: Sleeper | None = None,
    ) -> None:
        self.policy = policy or RetryPolicy()
        self.store: IdempotencyStore = store or InMemoryIdempotencyStore()
        self._sleeper: Sleeper = sleeper or time.sleep
        self.dead_letters: list[DeadLetter] = []
        self.escalations: list[QueueError] = []
        self.emitted_events: list[dict[str, Any]] = []

    @staticmethod
    def idempotency_key(task_id: str, substep: str) -> str:
        """Idempotency key for a step: task_id + substep (anti-double-run)."""
        return f"{task_id}:{substep}"

    def backoff_s(self, attempt: int) -> float:
        """Exponential backoff capped at max_backoff_s; attempt is 1-based."""
        return min(self.policy.max_backoff_s, self.policy.base_backoff_s * (2 ** (attempt - 1)))

    def execute_step(self, task_id: str, substep: str, fn: Callable[[], Any]) -> Any:
        # One loop on purpose (>20 lines): the idempotency check, the retry
        # loop and the terminal routing must stay atomic for a single step —
        # splitting them would let a retried step run twice or lose a
        # dead-letter record mid-flight.
        key = self.idempotency_key(task_id, substep)
        if self.store.seen(key):
            return self.store.result(key)
        attempt = 0
        while True:
            try:
                result = fn()
            except Exception as exc:
                error = classify_exception(exc)
                attempt += 1
                max_retries = self.policy.max_retries.get(error.error_class, 0)
                if should_escalate(error) or attempt > max_retries:
                    self._handle_terminal(task_id, substep, error, attempt)
                    raise error
                log_error(error, {"task_id": task_id, "substep": substep, "attempt": attempt})
                self._sleeper(self.backoff_s(attempt))
                continue
            self.store.mark(key, result)
            return result

    def _handle_terminal(self, task_id: str, substep: str, error: QueueError, attempts: int) -> None:
        """Route a terminal failure: escalate (owner-decision only) or dead-letter."""
        log_error(error, {"task_id": task_id, "substep": substep, "attempts": attempts, "terminal": True})
        if should_escalate(error):
            self.escalations.append(error)
            self.emitted_events.append(error_event(error, event_type="queue.escalation.v1"))
            return
        self.dead_letters.append(
            DeadLetter(
                task_id=task_id,
                substep=substep,
                code=error.code,
                error_class=error.error_class,
                attempts=attempts,
                occurred_at=datetime.now(timezone.utc).isoformat(),
            )
        )
        self.emitted_events.append(error_event(error, event_type="queue.dead_letter.v1"))

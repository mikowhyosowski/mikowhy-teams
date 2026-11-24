"""Central, single-point error handling for the queue engine.

Every failure class in the worker/watchdog/orchestrator path is classified
here so retry policy and escalation rules live in exactly one place. All
errors carry a stable machine ``code`` and a client-safe message. Logging is
structured via :mod:`logging` only — no ``print`` and no PII/token/payload
logging, per anti-slop rules.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger("engine.errors")

_EVENT_TYPE_RE = re.compile(r"^[a-z]+\.[a-z_]+\.v[0-9]+$")
_SENSITIVE_KEY_RE = re.compile(r"(token|secret|password|authorization|api[_-]?key)", re.IGNORECASE)


class ErrorClass(str, Enum):
    """Failure classification — drives retry policy and escalation."""

    TRANSIENT = "transient"
    STEP_FAILED = "step_failed"
    SCOPE = "scope"
    MISSING_FILE = "missing_file"
    TEST_FAILED = "test_failed"
    OWNER_DECISION = "owner_decision"


class QueueError(Exception):
    """Base queue error: stable code + client-safe message + class."""

    def __init__(
        self,
        code: str,
        client_message: str,
        error_class: ErrorClass,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(client_message)
        self.code = code
        self.client_message = client_message
        self.error_class = error_class
        self.context: dict[str, Any] = dict(context) if context else {}


class TransientQueueError(QueueError):
    """Timeout / connection / temporary unavailability — always auto-retried."""

    def __init__(self, client_message: str = "Temporary failure, retrying automatically", context: dict[str, Any] | None = None) -> None:
        super().__init__("QUEUE_TRANSIENT", client_message, ErrorClass.TRANSIENT, context)


class StepFailedError(QueueError):
    """Generic step failure — auto-retried, then dead-lettered."""

    def __init__(self, client_message: str = "Step failed, retrying automatically", context: dict[str, Any] | None = None) -> None:
        super().__init__("QUEUE_STEP_FAILED", client_message, ErrorClass.STEP_FAILED, context)


class ScopeViolationError(QueueError):
    """Write outside scope_paths — handled by automatic scope widening."""

    def __init__(self, client_message: str = "File outside scope_paths — scope auto-widened", context: dict[str, Any] | None = None) -> None:
        super().__init__("QUEUE_SCOPE_VIOLATION", client_message, ErrorClass.SCOPE, context)


class MissingFileError(QueueError):
    """FileNotFoundError — handled by the automatic repair loop."""

    def __init__(self, client_message: str = "File not found — resolved automatically", context: dict[str, Any] | None = None) -> None:
        super().__init__("QUEUE_MISSING_FILE", client_message, ErrorClass.MISSING_FILE, context)


class TestFailedError(QueueError):
    """Failing test — handled by the automatic fix loop."""

    def __init__(self, client_message: str = "Test failed — automatic fix loop", context: dict[str, Any] | None = None) -> None:
        super().__init__("QUEUE_TEST_FAILED", client_message, ErrorClass.TEST_FAILED, context)


class OwnerDecisionError(QueueError):
    """The ONLY class that may escalate to AWAITING_OWNER (with a package)."""

    def __init__(self, client_message: str, context: dict[str, Any] | None = None) -> None:
        super().__init__("QUEUE_OWNER_DECISION", client_message, ErrorClass.OWNER_DECISION, context)


def classify_exception(exc: BaseException) -> QueueError:
    """Map any exception to a QueueError with a class — one place, no guessing."""
    if isinstance(exc, QueueError):
        return exc
    if isinstance(exc, FileNotFoundError):
        return MissingFileError(context={"reason": str(exc)})
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return TransientQueueError(context={"reason": type(exc).__name__})
    if isinstance(exc, AssertionError):
        return TestFailedError(context={"reason": str(exc)})
    return StepFailedError(context={"reason": type(exc).__name__})


def should_escalate(error: QueueError) -> bool:
    """True only for genuine owner decisions — routine failures never escalate."""
    return error.error_class is ErrorClass.OWNER_DECISION


def _sanitize(context: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive keys so logs never carry tokens/secrets/PII."""
    return {k: ("***" if _SENSITIVE_KEY_RE.search(k) else v) for k, v in context.items()}


def log_error(error: QueueError, extra_context: dict[str, Any] | None = None) -> None:
    """Structured log of a queue error — never PII/tokens/payloads."""
    context = {**error.context, **(extra_context or {})}
    logger.error(
        "queue_error code=%s class=%s",
        error.code,
        error.error_class.value,
        extra={"queue_error": {"code": error.code, "class": error.error_class.value, "context": _sanitize(context)}},
    )


def error_event(error: QueueError, event_type: str = "queue.error.v1") -> dict[str, Any]:
    """Build an event conforming to contracts/events.schema.json (FROZEN, read-only)."""
    if not _EVENT_TYPE_RE.match(event_type):
        raise ValueError(f"event_type violates frozen schema: {event_type}")
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "payload": {
            "code": error.code,
            "error_class": error.error_class.value,
            "client_message": error.client_message,
        },
    }

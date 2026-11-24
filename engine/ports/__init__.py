"""
Warstwa kontraktów (Ports & Adapters) — czyste interfejsy, zero zależności zewnętrznych.

Rdzeń (orchestrator / worker_lifecycle) zna WYŁĄCZNIE te Protobole.
Konkretne technologie (SQLite, vibe CLI, git worktree, Telegram, subprocess)
wpinają się jako adaptery w engine/adapters/ i są wybierane w engine/container.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, Type, runtime_checkable


# --- modele kontraktowe (niemutowalne tam, gdzie to możliwe) -----------------

@dataclass(frozen=True)
class ExecutionResult:
    run_id: str
    exit_code: int | None
    duration_ms: int | None
    log_path: str | None
    stdout_tail: str = ""
    stderr_tail: str = ""
    status: str = "SUCCEEDED"  # QUEUED | RUNNING | SUCCEEDED | FAILED | TIMEOUT | KILLED


@dataclass(frozen=True)
class ExecutionContext:
    task_id: str | None = None
    agent_role: str | None = None
    substep: str | None = None


@dataclass(frozen=True)
class DecisionPacket:
    task_id: str
    title: str
    essence: str
    options: list[str] = field(default_factory=list)
    recommendation: str = ""


@dataclass(frozen=True)
class DecisionHandle:
    task_id: str
    channel_ref: str  # np. telegram thread id albo "local"


@dataclass(frozen=True)
class WorkspaceHandle:
    task_id: str
    path: str
    branch: str
    base: str


# --- porty --------------------------------------------------------------------

@runtime_checkable
class TaskSourcePort(Protocol):
    """Fasada źródła zadań (Task Manager). Przełączalne: sqlite | memory | mcp | github."""

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]: ...
    def get_task(self, task_id: str) -> dict[str, Any] | None: ...
    def claim_next_task(self, role: str, worker_id: str) -> dict[str, Any] | None: ...
    def transition_status(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]: ...
    def append_comment(self, task_id: str, author: str, text: str) -> None: ...
    def attach_artifact(self, task_id: str, path: str, label: str = "", kind: str = "") -> dict | None: ...
    def create_subtasks(self, task_id: str, subtask_specs: list[dict[str, Any]]) -> list[str]: ...


@runtime_checkable
class ReasoningEnginePort(Protocol):
    """Fasada modeli LLM. Przełączalne: vibe_cli | litellm/http | stub."""

    def complete_text(self, *, system: str, user: str, step: str, model: str = "") -> str: ...
    def complete_structured(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any: ...
    def health_check(self) -> bool: ...


@runtime_checkable
class ExecutionRunnerPort(Protocol):
    """Fasada powłoki/procesów. Przełączalne: local | docker | ssh."""

    def run_command(self, cmd: list[str] | str, *, cwd: str | None = None, timeout_s: float | None = None,
                    env: dict[str, str] | None = None, context: ExecutionContext | None = None) -> ExecutionResult: ...
    def kill_execution(self, run_id: str) -> dict[str, Any]: ...


@runtime_checkable
class VcsWorkspacePort(Protocol):
    """Fasada VCS + izolacji workspace. Przełączalne: git_worktree | (przyszłe: github_api)."""

    def prepare_workspace(self, task: dict[str, Any]) -> WorkspaceHandle: ...
    def verify_scope(self, workspace: WorkspaceHandle, scope_paths: list[str]) -> list[str]: ...
    def commit_and_push(self, workspace: WorkspaceHandle, task: dict[str, Any]) -> None: ...
    def integrate_to_target(self, task: dict[str, Any]) -> dict[str, Any]: ...
    def cleanup_workspace(self, workspace: WorkspaceHandle) -> None: ...


@runtime_checkable
class HumanLoopNotifierPort(Protocol):
    """Fasada komunikacji z człowiekiem (Sponsor Gate). Przełączalne: telegram | webhook | local."""

    def notify_status(self, task_id: str, message: str) -> None: ...
    def request_decision(self, packet: DecisionPacket) -> DecisionHandle: ...
    def post_milestone_update(self, milestone_id: str, message: str) -> None: ...

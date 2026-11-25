# Zrzut API portów (Ports API Snapshot)

**Data:** 2026-11-25  
**Commit:** `$(git rev-parse HEAD)`  
**Branch:** `feat/modular-architecture-ports-adapters`

---

## `engine/ports/__init__.py` — Pełna treść

```python
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
```

---

## Modele danych (dataclasses)

| Nazwa | Pola | Uwagi |
|-------|------|-------|
| `ExecutionResult` | `run_id`, `exit_code`, `duration_ms`, `log_path`, `stdout_tail`, `stderr_tail`, `status` | `frozen=True` |
| `ExecutionContext` | `task_id`, `agent_role`, `substep` | `frozen=True` |
| `DecisionPacket` | `task_id`, `title`, `essence`, `options[]`, `recommendation` | `frozen=True` |
| `DecisionHandle` | `task_id`, `channel_ref` | `frozen=True` |
| `WorkspaceHandle` | `task_id`, `path`, `branch`, `base` | `frozen=True` |

---

## Porty (Protocol) — sygnatury metod

### `TaskSourcePort`

| Metoda | Parametry | Zwraca | Opis |
|--------|-----------|--------|------|
| `list_tasks` | `status: str \| None` | `list[dict[str, Any]]` | Lista zadań, opcjonalnie filtrowana po statusie |
| `get_task` | `task_id: str` | `dict[str, Any] \| None` | Pobranie pojedynczego zadania |
| `claim_next_task` | `role: str`, `worker_id: str` | `dict[str, Any] \| None` | Zajęcie zadania przez workera (zmienia status na RUNNING) |
| `transition_status` | `task_id: str`, `new_status: str`, `**fields` | `dict[str, Any]` | Przejście statusu (z walidacją reguł) |
| `append_comment` | `task_id: str`, `author: str`, `text: str` | `None` | Dodanie komentarza do zadania |
| `attach_artifact` | `task_id: str`, `path: str`, `label: str = ""`, `kind: str = ""` | `dict \| None` | Dołączenie artefaktu (plik) |
| `create_subtasks` | `task_id: str`, `subtask_specs: list[dict[str, Any]]` | `list[str]` | Tworzenie podzadań, zwraca listę ID |

### `ReasoningEnginePort`

| Metoda | Parametry | Zwraca | Opis |
|--------|-----------|--------|------|
| `complete_text` | `system: str`, `user: str`, `step: str`, `model: str = ""` | `str` | Generowanie tekstu |
| `complete_structured` | `schema: Type[Any]`, `system: str`, `user: str`, `step: str`, `model: str = ""` | `Any` | Generowanie struktury (JSON → dataclass) |
| `health_check` | — | `bool` | Sprawdzenie dostępności modelu |

### `ExecutionRunnerPort`

| Metoda | Parametry | Zwraca | Opis |
|--------|-----------|--------|------|
| `run_command` | `cmd: list[str] \| str`, `cwd: str \| None`, `timeout_s: float \| None`, `env: dict[str, str] \| None`, `context: ExecutionContext \| None` | `ExecutionResult` | Uruchomienie komendy w izolacji |
| `kill_execution` | `run_id: str` | `dict[str, Any]` | Zabicie wykonania (PGID kill) |

### `VcsWorkspacePort`

| Metoda | Parametry | Zwraca | Opis |
|--------|-----------|--------|------|
| `prepare_workspace` | `task: dict[str, Any]` | `WorkspaceHandle` | Przygotowanie worktree/gałęzi dla zadania |
| `verify_scope` | `workspace: WorkspaceHandle`, `scope_paths: list[str]` | `list[str]` | Weryfikacja, że zmiany mieszczą się w scope |
| `commit_and_push` | `workspace: WorkspaceHandle`, `task: dict[str, Any]` | `None` | Commit + push zmian |
| `integrate_to_target` | `task: dict[str, Any]` | `dict[str, Any]` | Integracja do gałęzi docelowej (merge/rebase) |
| `cleanup_workspace` | `workspace: WorkspaceHandle` | `None` | Sprzątanie worktree |

### `HumanLoopNotifierPort`

| Metoda | Parametry | Zwraca | Opis |
|--------|-----------|--------|------|
| `notify_status` | `task_id: str`, `message: str` | `None` | Powiadomienie o statusie |
| `request_decision` | `packet: DecisionPacket` | `DecisionHandle` | Żądanie decyzji od Sponsora (przyciski) |
| `post_milestone_update` | `milestone_id: str`, `message: str` | `None` | Aktualizacja milestonu |

---

## Domyślne adaptery (zarejestrowane w `engine/adapters/__init__.py`)

| Port | Adaptery | Domyślny (env var) |
|------|----------|-------------------|
| `TaskSourcePort` | `sqlite` (`SqliteTaskAdapter`), `memory` (`InMemoryTaskAdapter`) | `TASK_PROVIDER=sqlite` |
| `ReasoningEnginePort` | `vibe_cli` (`VibeCliAdapter`), `stub` (`StubLlmAdapter`) | `LLM_PROVIDER=vibe_cli` |
| `ExecutionRunnerPort` | `local` (`LocalProcessRunner`) | `RUNNER_PROVIDER=local` |
| `VcsWorkspacePort` | `git_worktree` (`GitWorktreeAdapter`) | `VCS_PROVIDER=git_worktree` |
| `HumanLoopNotifierPort` | `telegram` (`TelegramNotifierAdapter`), `local` (`LocalNotifierAdapter`) | `NOTIFIER_PROVIDER=telegram` |

---

## Entry Points (group: `mikowhy_teams.adapters`)

**Stan na 2026-11-25:** Zarejestrowane przez `mikowhy-task-mcp` i `mikowhy-llm-router`:

```
llm:litellm = mikowhy_llm_router.adapters.litellm:LiteLLMAdapter
llm:ollama = mikowhy_llm_router.adapters.ollama:OllamaAdapter
llm:stub = mikowhy_llm_router.adapters.stub:StubLlmAdapter
llm:vibe_cli = mikowhy_llm_router.adapters.vibe_cli:VibeCliAdapter
task:github = mikowhy_task_mcp.adapters.github:GitHubTaskAdapter
task:linear = mikowhy_task_mcp.adapters.linear:LinearTaskAdapter
task:mcp = mikowhy_task_mcp.adapters.mcp:McpTaskAdapter
task:memory = mikowhy_task_mcp.adapters.memory:InMemoryTaskAdapter
task:sqlite = mikowhy_task_mcp.adapters.sqlite:SqliteTaskAdapter
```

Format: `kind:name = module.path:FactoryClass`

---

## Weryfikacja kontraktów (testy)

Plik: `tests/test_ports_contracts.py`

Scenariusze testowe (parametryzowane dla `sqlite` i `memory`):
1. `test_contract_create_get_list` — create, get, list, filtr statusu
2. `test_contract_claim_and_forward_transition` — claim → RUNNING → IN_REVIEW
3. `test_contract_regression_is_blocked_for_every_adapter` — IN_REVIEW → READY zablokowane, `allow_regression=True` przechodzi
4. `test_contract_comment_artifact_subtasks` — comment, attach_artifact, create_subtasks

Testy kontenera:
5. `test_default_adapters_satisfy_protocols` — wszystkie domyślne adaptery implementują porty
6. `test_container_env_override` — env vars nadpisują domyślne adaptery
7. `test_stub_llm_contract` — StubLlmAdapter spełnia ReasoningEnginePort
8. `test_singleton_container` — `get_container()` zwraca ten sam obiekt

**Wynik (2026-11-25):** ✅ 12/12 testów przechodzi
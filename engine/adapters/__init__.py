"""Adaptery portów — rejestracja domyślnych providerów (nadpisywane przez entry_points)."""
from __future__ import annotations

_REGISTRY: dict[str, dict[str, callable]] = {
    "task": {},
    "llm": {},
    "runner": {},
    "vcs": {},
    "notifier": {},
}


def register_adapter(kind: str, name: str, factory: callable) -> None:
    _REGISTRY.setdefault(kind, {})[name] = factory


def get_adapter_factory(kind: str, name: str) -> callable:
    try:
        return _REGISTRY[kind][name]
    except KeyError:
        raise KeyError(f"nieznany adapter {kind}={name!r}; dostępne: {sorted(_REGISTRY.get(kind, {}))}")


def available(kind: str) -> list[str]:
    return sorted(_REGISTRY.get(kind, {}))


# --- domyślne providery -------------------------------------------------------
def _register_defaults() -> None:
    from engine.adapters.sqlite_task import SqliteTaskAdapter
    from engine.adapters.memory_task import InMemoryTaskAdapter
    from engine.adapters.llm import VibeCliAdapter, StubLlmAdapter
    from engine.adapters.process_runner import LocalProcessRunner
    from engine.adapters.git_workspace import GitWorktreeAdapter
    from engine.adapters.notifier import TelegramNotifierAdapter, LocalNotifierAdapter

    register_adapter("task", "sqlite", SqliteTaskAdapter)
    register_adapter("task", "memory", InMemoryTaskAdapter)
    register_adapter("llm", "vibe_cli", VibeCliAdapter)
    register_adapter("llm", "stub", StubLlmAdapter)
    register_adapter("runner", "local", LocalProcessRunner)
    register_adapter("vcs", "git_worktree", GitWorktreeAdapter)
    register_adapter("notifier", "telegram", TelegramNotifierAdapter)
    register_adapter("notifier", "local", LocalNotifierAdapter)


_register_defaults()

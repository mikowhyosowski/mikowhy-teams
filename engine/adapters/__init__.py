"""Adaptery portów — rejestracja domyślnych providerów (nadpisywane przez entry_points).
Core adapters (VCS, Notifier) are registered here; Task/LLM/Runner come from entry points (mikowhy-task-mcp, mikowhy-llm-router, mikowhy-process-runner).
"""
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


# --- domyślne providery (core only: VCS + Notifier) ---------------------------------
def _register_defaults() -> None:
    from engine.adapters.git_workspace import GitWorktreeAdapter
    from engine.adapters.github_api import GitHubApiAdapter
    from engine.adapters.notifier import TelegramNotifierAdapter, LocalNotifierAdapter, SlackNotifierAdapter, WebhookNotifierAdapter

    register_adapter("vcs", "git_worktree", GitWorktreeAdapter)
    register_adapter("vcs", "github_api", GitHubApiAdapter)
    register_adapter("notifier", "telegram", TelegramNotifierAdapter)
    register_adapter("notifier", "local", LocalNotifierAdapter)
    register_adapter("notifier", "slack", SlackNotifierAdapter)
    register_adapter("notifier", "webhook", WebhookNotifierAdapter)


_register_defaults()

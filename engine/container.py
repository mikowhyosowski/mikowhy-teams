"""
AppContainer — lekki kontener zależności (composition root).

Składa zestaw adapterów dla portów z engine/ports/ na podstawie zmiennych
środowiskowych (TASK_PROVIDER, LLM_PROVIDER, RUNNER_PROVIDER, VCS_PROVIDER,
NOTIFIER_PROVIDER — domyślnie: sqlite | vibe_cli | local | git_worktree | telegram)
oraz wtyczek odkrywanych przez importlib.metadata entry_points
(group="mikowhy_teams.adapters") — każdy wydzielony pakiet po `pip install`
może zarejestrować własny adapter bez zmiany w core.
"""
from __future__ import annotations

import importlib.metadata
import os
from dataclasses import dataclass
from typing import Any

from engine import adapters as adapters_registry
from engine.ports import (
    ExecutionRunnerPort,
    HumanLoopNotifierPort,
    ReasoningEnginePort,
    TaskSourcePort,
    VcsWorkspacePort,
)

ENTRY_POINT_GROUP = "mikowhy_teams.adapters"


def _discover_entry_points() -> None:
    """Odkryj zewnętrzne wtyczki: entry point 'kind:name' w grupie mikowhy_teams.adapters."""
    try:
        eps = importlib.metadata.entry_points(group=ENTRY_POINT_GROUP)
    except Exception:
        return
    for ep in eps:
        try:
            kind, _, name = ep.name.partition(":")
            factory = ep.load()  # Call load() to get the actual class/factory
            adapters_registry.register_adapter(kind, name, factory)
        except Exception:
            continue


@dataclass
class AppContainer:
    task_source: TaskSourcePort
    llm: ReasoningEnginePort
    runner: ExecutionRunnerPort
    vcs: VcsWorkspacePort
    notifier: HumanLoopNotifierPort

    @classmethod
    def from_env(cls, **overrides: Any) -> "AppContainer":
        _discover_entry_points()
        cfg = {
            "task": os.environ.get("TASK_PROVIDER", "sqlite"),
            "llm": os.environ.get("LLM_PROVIDER", "vibe_cli"),
            "runner": os.environ.get("RUNNER_PROVIDER", "local"),
            "vcs": os.environ.get("VCS_PROVIDER", "git_worktree"),
            "notifier": os.environ.get("NOTIFIER_PROVIDER", "telegram"),
        }
        cfg.update({k: v for k, v in overrides.items() if v})
        return cls(
            task_source=adapters_registry.get_adapter_factory("task", cfg["task"])(),
            llm=adapters_registry.get_adapter_factory("llm", cfg["llm"])(),
            runner=adapters_registry.get_adapter_factory("runner", cfg["runner"])(),
            vcs=adapters_registry.get_adapter_factory("vcs", cfg["vcs"])(),
            notifier=adapters_registry.get_adapter_factory("notifier", cfg["notifier"])(),
        )

    @property
    def state(self):
        """Wygodny dostęp do State gdy task_source=sqlite (back-compat dla starszych ścieżek)."""
        return getattr(self.task_source, "state", None)


_CONTAINER: AppContainer | None = None


def get_container() -> AppContainer:
    global _CONTAINER
    if _CONTAINER is None:
        _CONTAINER = AppContainer.from_env()
    return _CONTAINER


def set_container(c: AppContainer | None) -> None:
    global _CONTAINER
    _CONTAINER = c

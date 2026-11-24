# Architektura modularna — Ports & Adapters (mikowhy-teams)

## Warstwy

```
core (orchestrator.py, worker_lifecycle.py)  ── zna tylko ──▶  engine/ports/ (Protocol + dataclasses)
                                                                      ▲
                                                          engine/adapters/ (wybór w engine/container.py)
                                                                      ▲
                                              SQLite / vibe CLI / subprocess / git / Telegram / MCP (przyszłe)
```

- **`engine/ports/`** — czyste kontrakty, zero zależności zewnętrznych:
  `TaskSourcePort`, `ReasoningEnginePort`, `ExecutionRunnerPort`, `VcsWorkspacePort`,
  `HumanLoopNotifierPort` + modele (`ExecutionResult`, `DecisionPacket`, `DecisionHandle`,
  `WorkspaceHandle`, `ExecutionContext`).
- **`engine/adapters/`** — domyślne providery: `sqlite` (State), `memory` (testy),
  `vibe_cli`/`stub` (llm_gateway), `local` (sponsor_shell), `git_worktree` (git_guard),
  `telegram`/`local` (notifier).
- **`engine/container.py`** — `AppContainer.from_env()` składa adaptery ze zmiennych
  `TASK_PROVIDER | LLM_PROVIDER | RUNNER_PROVIDER | VCS_PROVIDER | NOTIFIER_PROVIDER`
  i odkrywa zewnętrzne wtyczki przez `importlib.metadata.entry_points(group="mikowhy_teams.adapters")`.

## Wpięcie w rdzeń

- `Orchestrator(poll, container=None)` — dostaje porty z kontenera; przejścia statusów
  w `_handle_merges` idą przez `self.tasks.transition_status(...)` (TaskSourcePort).
- `worker_lifecycle.run()` — claim/transition przez `container.task_source`.
- `self.state` zostaje jako back-compat tylko gdy `TASK_PROVIDER=sqlite`.

## Macierz podziału: Core vs osobne repozytoria

| Moduł | Gdzie | Uzasadnienie | Kontrakt komunikacji |
|---|---|---|---|
| Maszyna stanów zadań, scheduler, quality gates, budżet błędów, orkiestracja cyklu życia | **zostaje: `mikowhy-teams-core`** | logika biznesowa; zero zależności od baz/CLI/komunikatorów | porty w `engine/ports/` |
| `mikowhy-task-mcp` — serwer MCP + adaptery źródeł zadań (SQLite/GitHub/Linear) | **osobne repo/pakiet** | używalne też w news-video-generator, lead-discovery-lab | `TaskSourcePort` + narzędzia MCP `list_tasks`/`claim_task`/`update_task_status` |
| `mikowhy-llm-router` — kolejka, batching, cache, adaptery Vibe CLI/LiteLLM/Ollama | **osobne repo** | samodzielna infrastruktura AI | `ReasoningEnginePort` (`complete_text`/`complete_structured`/`health_check`) |
| `mikowhy-process-runner` — bezpieczny runner powłoki (PGID kill, rejestr runów, logi) | **osobne repo** | uniwersalny dla wszystkich botów (już wydzielony jako `engine/sponsor_shell.py`) | `ExecutionRunnerPort` (`run_command`/`kill_execution`) |
| `centrum-dowodzenia-ui` — frontend React | **osobne repo** | komunikuje się tylko przez REST + WebSocket | REST `:8765` + WS `:8767` (pulse/log) — patrz `docs/PROCESS_HANDLING.md` |
| VCS/git worktree + PR | **pakiet w core lub osobne repo** | izolacja workspace per zadanie | `VcsWorkspacePort` |
| Telegram/Slack/webhook | **adapter w core, wymienny** | kanał człowieka | `HumanLoopNotifierPort` |

## Weryfikacja

`tests/test_ports_contracts.py` — jedna suita kontraktu przeciwko `sqlite` i `memory`
(create/claim/transition/regresja-zablokowana/comment/artifact/subtasks) oraz kontrakt
kontenera (env override, entry points, singleton). Pełna suita: `pytest tests/`.

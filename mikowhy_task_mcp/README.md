# mikowhy-task-mcp

MCP Server + TaskSourcePort adapters for mikowhy-teams.

## Adaptery

| Adapter | Opis | Wymagania |
|---------|------|-----------|
| `sqlite` | SQLite przez `engine.state.State` (domyślny) | — |
| `memory` | In-memory do testów | — |
| `mcp` | Komunikacja przez MCP (Model Context Protocol) | `fastmcp`, serwer MCP na porcie 8766 |
| `github` | GitHub Issues/Projects v2 | `GITHUB_TOKEN` |
| `linear` | Linear (linear.app) | `LINEAR_API_KEY` |

## Instalacja

```bash
pip install -e .
```

## Użycie w mikowhy-teams

```bash
# Domyślny (SQLite)
TASK_PROVIDER=sqlite python -m engine.orchestrator run

# In-memory (testy)
TASK_PROVIDER=memory python -m engine.orchestrator run

# MCP (zdalny serwer zadań)
TASK_PROVIDER=mcp python -m engine.orchestrator run

# GitHub Projects
TASK_PROVIDER=github GITHUB_TOKEN=xxx GITHUB_OWNER=org GITHUB_REPO=repo python -m engine.orchestrator run
```

## Serwer MCP

Uruchomienie serwera MCP (udostępnia narzędzia `list_tasks`, `claim_task`, `update_task_status`, `add_comment`, `attach_artifact`, `create_subtasks`):

```bash
python -m mikowhy_task_mcp.server
```

Serwer nasłuchuje na `http://localhost:8766/mcp` (konfigurowalne przez `MCP_HOST`/`MCP_PORT`).

## Entry Points

Pakiet rejestruje się w grupie `mikowhy_teams.adapters`:

```
task:sqlite = mikowhy_task_mcp.adapters.sqlite:SqliteTaskAdapter
task:memory = mikowhy_task_mcp.adapters.memory:InMemoryTaskAdapter
task:mcp    = mikowhy_task_mcp.adapters.mcp:McpTaskAdapter
task:github = mikowhy_task_mcp.adapters.github:GitHubTaskAdapter
task:linear = mikowhy_task_mcp.adapters.linear:LinearTaskAdapter
```

Dzięki temu `engine.container.AppContainer.from_env()` automatycznie wykrywa i ładuje te adaptery bez konieczności importowania ich w kodzie core.
# mikowhy-process-runner

Process Runner z PGID kill, rejestrem runów, logami + adaptery dla mikowhy-teams.

## Adaptery

| Adapter | Opis | Wymagania |
|---------|------|-----------|
| `local` | Lokalny proces przez `engine/sponsor_shell.py` (PGID kill, logi, rejestr) | — |
| `docker` | Komendy w kontenerach Docker (`docker run --rm`) | Docker daemon |
| `ssh` | Zdalne wykonanie przez SSH | SSH klucz/dostęp do hosta |

## Instalacja

```bash
pip install -e .

# Z opcjonalnymi zależnościami
pip install -e .[docker]
```

## Użycie w mikowhy-teams

```bash
# Domyślny (lokalny przez sponsor_shell)
RUNNER_PROVIDER=local python -m engine.orchestrator run

# Docker
RUNNER_PROVIDER=docker DOCKER_IMAGE=python:3.12-slim python -m engine.orchestrator run

# SSH
RUNNER_PROVIDER=ssh SSH_HOST=server.example.com SSH_USER=deploy python -m engine.orchestrator run
```

## LocalProcessRunner (domyślny)

Używa `engine/sponsor_shell.py` - deterministyczny gateway:

- **Izolacja**: Każda komenda w nowej sesji/grupie procesów (PGID = PID)
- **Kill**: `os.killpg(pgid, SIGTERM)` → grace period → `SIGKILL` (całe drzewo procesów)
- **Rejestracja**: `process_runs` tabela w SQLite (run_id, pid, pgid, status, logi)
- **Logi**: Pełny log w `.agents/logs/runs/<run_id>.log`, tail 4000 znaków w bazie
- **Timeout**: Responsywny (select/poll), status TIMEOUT/KILLED

## DockerProcessRunner

```bash
RUNNER_PROVIDER=docker \
  DOCKER_IMAGE=python:3.12-slim \
  DOCKER_HOST=unix:///var/run/docker.sock \
  python -m engine.orchestrator run
```

Używa `docker run --rm -i -v <cwd>:/workspace -w /workspace <image> /bin/sh -c "<cmd>"`

## SshProcessRunner

```bash
RUNNER_PROVIDER=ssh \
  SSH_HOST=server.example.com \
  SSH_USER=deploy \
  SSH_KEY=/path/to/key \
  python -m engine.orchestrator run
```

Używa `ssh user@host "cd <cwd> && export VAR=val && <cmd>"`

## Entry Points

Pakiet rejestruje się w grupie `mikowhy_teams.adapters`:

```
runner:local = mikowhy_process_runner.adapters.local:LocalProcessRunner
runner:docker = mikowhy_process_runner.adapters.docker:DockerProcessRunner
runner:ssh = mikowhy_process_runner.adapters.ssh:SshProcessRunner
```

Dzięki temu `engine.container.AppContainer.from_env()` automatycznie wykrywa i ładuje te adaptery.
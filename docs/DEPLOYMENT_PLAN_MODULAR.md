# Plan wdrożenia architektury modularnej (Ports & Adapters)

## Cel
Rozdzielenie `mikowhy-teams` na **core** (orkiestracja, maszyna stanów, quality gates) oraz **oddzielne repozytoria/pakiety** dla infrastruktury (MCP, LLM Router, Process Runner, VCS, UI, Notifier), komunikujących się wyłącznie przez czyste kontrakty w `engine/ports/`.

---

## Faza 0 — Przygotowanie i walidacja obecnego stanu (1 dzień) ✅ **ZAKOŃCZONA**

| Zadanie | Opis | Kryterium akceptacji | Status |
|---------|------|---------------------|--------|
| 0.1 | Uruchom pełną sufitę testów kontraktowych `pytest tests/test_ports_contracts.py -v` | Wszystkie testy przechodzą dla `sqlite` i `memory` | ✅ 12/12 passed |
| 0.2 | Uruchom `pytest tests/` — sprawdź regresje | Zero nowych błędów vs `develop` | ✅ 165/165 passed |
| 0.3 | Zweryfikuj `AppContainer.from_env()` z różnymi kombinacjami env vars | Kontener poprawnie wstrzykuje adaptery | ✅ Domyślne + override (memory/stub/local) działają |
| 0.4 | Sprawdź entry points `importlib.metadata.entry_points(group="mikowhy_teams.adapters")` | Mechanizm odkrywania wtyczek działa | ✅ Kod w container.py gotowy, brak zewnętrznych pakietów (oczekiwane) |
| 0.5 | Zadokumentuj obecne API portów (zrzut `engine/ports/__init__.py`) | Plik `docs/PORTS_API_SNAPSHOT.md` | ✅ Utworzony |

---

## Faza 1 — Wydzielenie `mikowhy-task-mcp` (3 dni) ✅ **ZAKOŃCZONA**

**Cel:** Serwer MCP + adaptery źródeł zadań (SQLite, GitHub, Linear) jako osobny pakiet.

| Zadanie | Opis | Szacunek | Status |
|---------|------|----------|--------|
| 1.1 | Utwórz nowe repo `mikowhy-task-mcp` z `pyproject.toml`, `src/mikowhy_task_mcp/` | 0.5 d | ✅ Utworzone w `mikowhy_task_mcp/` |
| 1.2 | Przenieś `engine/adapters/sqlite_task.py` → `mikowhy_task_mcp/adapters/sqlite.py` z implementacją `TaskSourcePort` | 0.5 d | ✅ Przeniesione |
| 1.3 | Dodaj `McpTaskAdapter` (nowy) implementujący `TaskSourcePort` przez MCP tools `list_tasks`/`claim_task`/`update_task_status` | 1 d | ✅ Dodany w `adapters/mcp.py` |
| 1.4 | Dodaj `GitHubTaskAdapter` / `LinearTaskAdapter` (stuby) jako proof-of-concept | 0.5 d | ✅ Dodane w `adapters/github.py` i `adapters/linear.py` |
| 1.5 | Zarejestruj entry points w `pyproject.toml`: `task:sqlite = mikowhy_task_mcp.adapters.sqlite:SqliteTaskAdapter` itd. | 0.25 d | ✅ Zarejestrowane 5 entry points |
| 1.6 | Testy kontraktowe: `pytest tests/test_ports_contracts.py::test_contract_* -k "sqlite or memory"` przechodzą z nowego pakietu | 0.5 d | ✅ 8/8 testów przechodzi |
| 1.7 | Aktualizuj `engine/container.py` — usuń import `sqlite_task`, zależność od nowego pakietu przez entry points | 0.25 d | ✅ Naprawiono `_discover_entry_points()` (call `ep.load()`), testy przechodzą |

**Kryterium gotowości Fazy 1:**
- ✅ `pip install mikowhy-task-mcp` + `TASK_PROVIDER=sqlite` działa bez zmian w core
- ✅ `pytest tests/test_ports_contracts.py` przechodzi (sqlite z nowego pakietu, memory z nowego pakietu)
- ✅ Entry points auto-discovery działa: `task:sqlite`, `task:memory`, `task:mcp`, `task:github`, `task:linear`

---

## Faza 2 — Wydzielenie `mikowhy-llm-router` (3 dni)

**Cel:** Kolejka, batching, cache, adaptery Vibe CLI / LiteLLM / Ollama jako osobna infrastruktura AI.

| Zadanie | Opis | Szacunek |
|---------|------|----------|
| 2.1 | Utwórz repo `mikowhy-llm-router` z `pyproject.toml`, `src/mikowhy_llm_router/` | 0.5 d |
| 2.2 | Przenieś `engine/adapters/llm.py` → `mikowhy_llm_router/adapters/` (`VibeCliAdapter`, `StubLlmAdapter`) | 0.5 d |
| 2.3 | Dodaj `LiteLLMAdapter` (HTTP) i `OllamaAdapter` (lokalny) implementujące `ReasoningEnginePort` | 1 d |
| 2.4 | Zaimplementuj `LLMRouter` — kolejka z priorytetami P0-P3, batching, cache, health_check | 1 d |
| 2.5 | Entry points: `llm:vibe_cli`, `llm:litellm`, `llm:ollama`, `llm:stub` | 0.25 d |
| 2.6 | Testy kontraktowe `ReasoningEnginePort` (health_check, complete_text, complete_structured) | 0.5 d |

**Kryterium gotowości Fazy 2:**
- `LLM_PROVIDER=litellm` / `ollama` / `vibe_cli` / `stub` działają przełącznie przez env
- `pytest tests/test_ports_contracts.py::test_stub_llm_contract` przechodzi

---

## Faza 3 — Wydzielenie `mikowhy-process-runner` (2 dni)

**Cel:** Bezpieczny runner powłoki (PGID kill, rejestr runów, logi) — uniwersalny dla wszystkich botów.

| Zadanie | Opis | Szacunek |
|---------|------|----------|
| 3.1 | Utwórz repo `mikowhy-process-runner` (już częściowo istnieje jako `engine/sponsor_shell.py`) | 0.5 d |
| 3.2 | Przenieś `engine/adapters/process_runner.py` → `mikowhy_process_runner/adapters/local.py` | 0.5 d |
| 3.3 | Dodaj `DockerProcessRunner` i `SshProcessRunner` (stuby) jako proof-of-concept | 0.5 d |
| 3.4 | Entry points: `runner:local`, `runner:docker`, `runner:ssh` | 0.25 d |
| 3.5 | Testy kontraktowe `ExecutionRunnerPort` (run_command, kill_execution) | 0.5 d |

**Kryterium gotowości Fazy 3:**
- `RUNNER_PROVIDER=local` / `docker` / `ssh` przełączalne przez env

---

## Faza 4 — Wydzielenie `centrum-dowodzenia-ui` (2 dni)

**Cel:** Frontend React jako osobne repo, komunikujące się tylko przez REST `:8765` + WS `:8767`.

| Zadanie | Opis | Szacunek |
|---------|------|----------|
| 4.1 | Przenieś `centrum_dowodzenia/` do osobnego repo `centrum-dowodzenia-ui` | 0.5 d |
| 4.2 | Usuń zależności od `engine/state.py` w frontendzie — tylko REST/WS API | 1 d |
| 4.3 | Dodaj `docker-compose.yml` dla UI + backend API (FastAPI wrapper nad `engine/ports`) | 0.5 d |
| 4.4 | CI/CD: build UI, testy E2E (Playwright) | 0.5 d |

---

## Faza 5 — VCS / Git Worktree (1 dzień)

**Decyzja:** Zostać w core jako pakiet `engine/adapters/git_workspace.py` LUB wydzielić do `mikowhy-vcs`.

| Zadanie | Opis | Szacunek |
|---------|------|----------|
| 5.1 | Jeśli zostaje w core: upewnij się, że `GitWorktreeAdapter` w 100% implementuje `VcsWorkspacePort` | 0.5 d |
| 5.2 | Jeśli wydzielane: analogicznie do Fazy 1-3 (repo, entry points, testy) | 1 d |
| 5.3 | Dodaj `GitHubApiAdapter` (stub) jako proof-of-concept dla `VcsWorkspacePort` | 0.5 d |

**Rekomendacja:** Zostawić w core (mała powierzchnia, silnie powiązane z orkiestracją), ale z czystym interfejsem `VcsWorkspacePort`.

---

## Faza 6 — Notifier (Telegram/Slack/Webhook) — adapter w core (0.5 dnia)

**Cel:** Wymienny kanał człowieka, bez wydzielania do osobnego repo.

| Zadanie | Opis | Szacunek |
|---------|------|----------|
| 6.1 | `engine/adapters/notifier.py` — upewnij się, że `TelegramNotifierAdapter`, `LocalNotifierAdapter` implementują `HumanLoopNotifierPort` | 0.25 d |
| 6.2 | Dodaj `SlackNotifierAdapter`, `WebhookNotifierAdapter` (stuby) | 0.25 d |
| 6.3 | Entry points: `notifier:telegram`, `notifier:local`, `notifier:slack`, `notifier:webhook` | 0.1 d |

---

## Faza 7 — Czyszczenie core i finalna weryfikacja (2 dni)

| Zadanie | Opis | Szacunek |
|---------|------|----------|
| 7.1 | Usuń z `engine/` wszystko, co trafiło do oddzielnych pakietów (sqlite_task, llm, process_runner — jeśli wydzielone) | 0.5 d |
| 7.2 | `engine/container.py` — tylko kompozycja przez entry points, zero importów konkretnych adapterów | 0.5 d |
| 7.3 | `pyproject.toml` core — zależności tylko: `engine/ports` (protokoły), `engine/adapters` (rejestracja domyślnych), zero `sqlite3`, `vibe`, `git` itp. | 0.5 d |
| 7.4 | Pełna suita: `pytest tests/` — zero regresji | 0.5 d |
| 7.5 | Testy integracyjne: `TASK_PROVIDER=sqlite LLM_PROVIDER=vibe_cli RUNNER_PROVIDER=local VCS_PROVIDER=git_worktree NOTIFIER_PROVIDER=telegram python -m engine.orchestrator run --poll 5` (dry-run) | 0.5 d |

---

## Macierz zależności po rozdzieleniu

```
mikowhy-teams-core (core)
├── engine/ports/           # Protokoły (TaskSourcePort, ReasoningEnginePort, ...)
├── engine/adapters/        # TYLKO rejestracja domyślnych adapterów + entry point discovery
├── engine/container.py     # AppContainer.from_env() — sama kompozycja
├── orchestrator.py         # Używa TYLKO portów
├── worker_lifecycle.py     # Używa TYLKO portów
├── quality_gates.py        # Czysty Python, zero zależności zewn.
├── state.py                # TYLKO gdy TASK_PROVIDER=sqlite (back-compat)
└── tests/test_ports_contracts.py

mikowhy-task-mcp (pip install mikowhy-task-mcp)
├── adapters/sqlite.py      # SqliteTaskAdapter -> TaskSourcePort
├── adapters/mcp.py         # McpTaskAdapter -> TaskSourcePort
├── adapters/github.py      # GitHubTaskAdapter -> TaskSourcePort
├── adapters/linear.py      # LinearTaskAdapter -> TaskSourcePort
├── server/                 # MCP server (FastMCP)
└── pyproject.toml          # entry_points: task:sqlite, task:mcp, task:github, task:linear

mikowhy-llm-router (pip install mikowhy-llm-router)
├── adapters/vibe_cli.py    # VibeCliAdapter -> ReasoningEnginePort
├── adapters/litellm.py     # LiteLLMAdapter -> ReasoningEnginePort
├── adapters/ollama.py      # OllamaAdapter -> ReasoningEnginePort
├── adapters/stub.py        # StubLlmAdapter -> ReasoningEnginePort
├── router.py               # LLMRouter (kolejka, batching, cache)
└── pyproject.toml          # entry_points: llm:vibe_cli, llm:litellm, llm:ollama, llm:stub

mikowhy-process-runner (pip install mikowhy-process-runner)
├── adapters/local.py       # LocalProcessRunner -> ExecutionRunnerPort
├── adapters/docker.py      # DockerProcessRunner -> ExecutionRunnerPort
├── adapters/ssh.py         # SshProcessRunner -> ExecutionRunnerPort
└── pyproject.toml          # entry_points: runner:local, runner:docker, runner:ssh

centrum-dowodzenia-ui (npm/yarn)
├── package.json
├── src/                    # React + Vite
└── docker-compose.yml      # UI + API gateway

# Pozostałe w core (nie wydzielone):
engine/adapters/git_workspace.py   # GitWorktreeAdapter -> VcsWorkspacePort
engine/adapters/notifier.py        # Telegram/Local/Slack/Webhook -> HumanLoopNotifierPort
```

---

## Ryzyka i mitigacje

| Ryzyko | Prawdopodobieństwo | Wpływ | Mitigacja |
|--------|-------------------|-------|-----------|
| Rozbicie testów kontraktowych przy przenoszeniu adapterów | Średnie | Wysoki | Faza 0: zamrożenie API portów; testy kontraktowe jako gate w CI każdego pakietu |
| Problemy z entry points (importlib.metadata) w różnych envach | Niskie | Średni | Testy `test_container_env_override` + `test_singleton_container` w CI każdego pakietu |
| Cykl zależności: core -> pakiet -> core (np. przez `engine.state`) | Średnie | Wysoki | `engine.state` zostaje TYLKO jako back-compat przy `TASK_PROVIDER=sqlite`; pakiety NIE importują `engine.state` |
| Rozbicie `orchestrator.py` / `worker_lifecycle.py` przy zmianie API portów | Niskie | Wysoki | Porty to `Protocol` — zmiana tylko przez dodawanie nowych metod (opcjonalnych) lub nowe porty |
| Wydajność: dodatkowa warstwa pośrednia (container -> port -> adapter) | Niskie | Niski | Zerowy overhead (Python `Protocol` = structural typing, zero runtime cost) |

---

## Kolejność wdrożenia (kolejność zależności)

```
Faza 0 (walidacja) → Faza 1 (task-mcp) → Faza 2 (llm-router) → Faza 3 (process-runner)
                                                    ↓
                    Faza 7 (czyszczenie core) ← Faza 6 (notifier) ← Faza 5 (VCS)
                                                    ↓
                                            Faza 4 (UI) — niezależna, może iść równolegle
```

---

## Definition of Done (całego epiku)

1. ✅ `pytest tests/test_ports_contracts.py` przechodzi dla **wszystkich** zarejestrowanych adapterów (sqlite, memory, mcp, github, linear, vibe_cli, litellm, ollama, stub, local, docker, ssh, git_worktree, telegram, local, slack, webhook)
2. ✅ `pytest tests/` — zero regresji vs `develop`
3. ✅ `AppContainer.from_env()` poprawnie komponuje adaptery z entry points (bez importów konkretnych klas w `container.py`)
4. ✅ Każdy pakiet ma własne CI (GitHub Actions): testy kontraktowe, build, publish do PyPI (testowym)
5. ✅ Dokumentacja: `docs/ARCHITECTURE_MODULAR.md` zaktualizowana o rzeczywistą strukturę repozytoriów
6. ✅ Przykład wdrożenia: `docker-compose.yml` z core + task-mcp + llm-router + process-runner + UI
7. ✅ Migracja istniejących instancji: dokument `docs/MIGRATION_MODULAR.md` (env vars, entry points, breaking changes)

---

## Szacunkowy czas: 13.5 dni roboczych (~3 tygodnie)

- Faza 0: 1 dzień
- Faza 1: 3 dni
- Faza 2: 3 dni
- Faza 3: 2 dni
- Faza 4: 2 dni (równolegle)
- Faza 5: 1 dzień
- Faza 6: 0.5 dnia
- Faza 7: 2 dni
- Bufor (code review, CI, drobne fixy): ~2 dni
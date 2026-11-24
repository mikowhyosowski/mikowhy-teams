# Obsługa procesów w mikowhy-teams

Jak projekt rejestruje, nadzoruje, streamuje i zabija procesy wykonywane przez agentów
(sponsor-shell + pipeline runów + widoki w dashboardach).

## 1. Deterministyczny gateway wykonywania komend — `engine/sponsor_shell.py`

Jeden centralny punkt wejścia dla każdej komendy wykonywanej przez agenta/silnik:

```bash
python3 -m engine.sponsor_shell --task <TASK_ID> --agent <ROLE> --step <SUBSTEP> \
    [--timeout 300] [--cwd ...] -- <komenda...>
python3 -m engine.sponsor_shell --kill <RUN_ID>     # zabij istniejący run
```

Albo z Python: `from engine.sponsor_shell import run_command, kill_run`.

Zasady działania:

- **Izolacja procesu** — `subprocess.Popen(..., start_new_session=True)`, więc `pid == pgid`
  i `os.killpg` trafia w całą grupę (żadnych zombie).
- **Rejestracja runu** — wiersz w tabeli `process_runs` (chyba że `--no-register`):
  `run_id`, `task_id`, `agent_role`, `substep`, `command`, `cwd`, `pid`, `pgid`, `status`,
  `started_at` (ISO-8601 UTC), `timeout_s`, `log_path`, na końcu `finished_at`,
  `duration_ms`, `exit_code`, `stdout_tail`, `stderr_tail`.
- **Live capture** — stdout/stderr czytane na bieżąco (bez blokującego buforowania),
  pełny log w `.agents/logs/runs/<run_id>.log`; wynik strukturyzowany:
  `exit_code`, `duration_ms`, `log_path`, tail stdout/stderr.
- **Sprzątanie** — timeout / SIGINT / kill z UI → `killpg(SIGTERM)`, eskalacja `killpg(SIGKILL)`;
  status końcowy `TIMEOUT` / `KILLED`.
- **Statusy runu**: `QUEUED | RUNNING | SUCCEEDED | FAILED | TIMEOUT | KILLED`.

## 2. Model danych — tabela `process_runs` w `.agents/state.db`

Indeksy po `task_id`, `agent_role`, `status`, `started_at`. Warstwa dostępu w `engine/state.py`:

- `get_process_run(run_id)`
- `list_process_runs(task_id, agent_role, status, limit)` — chronologiczna oś czasu (najnowsze pierwsze)
- `active_process_runs()` — żywe (`QUEUED`/`RUNNING`)
- `agent_pipeline(role)` — **Agent Execution Pipeline**: wszystkie zadania roli + ich runy
  (łącznie z nieprzypisanymi), pogrupowane per zadanie

## 3. REST API (stary dashboard, `engine/dashboard.py`, port 8765)

| Endpoint | Opis |
|---|---|
| `GET /api/pipeline/runs?task_id=&agent_role=&status=&limit=` | lista runów z filtrami |
| `GET /api/pipeline/runs/<run_id>/log?tail=` | pełny log runu (ANSI-safe plain text; `tail=0` = całość) |
| `POST /api/pipeline/runs/<run_id>/kill` | zabicie zawieszonego procesu przez Sponsora (cała grupa pgid) |
| `GET /api/agents/<role>/pipeline` | pełna historia zadań + procesów roli |
| `GET /api/workers` | procesy workerów z OS (`engine.worker_supervisor.inspect`) |
| `GET /api/workers/health` | health per zadanie RUNNING: `idle_s`, `stall`, `llm_job_running` |
| `POST /api/task/<id>/force_retry` | requeue zawieszonego RUNNING → READY (z `allow_regression=True`) |

**Stall detection**: zadanie RUNNING z `idle_s >= stall_threshold_s` (kv w bazie, domyślnie 600 s)
i bez aktywnego joba LLM = „zacięcie” (lista `stalled` w `/api/workers/health` i w pulse WS).

## 4. WebSocket gateway — `engine/ws_gateway.py` (port 8767)

Live push zamiast pollingu (centrum_dowodzenia łączy się z `ws://<host>:8767/`):

- **`pulse`** — broadcast co ~1 s, tylko gdy stan się zmienił: `live` (aktywne runy),
  `recent` (ostatnie 20), `workers` + `stalled` (health jak w `/api/workers/health`).
- **`log`** — stream nowych linii logu subskrybowanego runu:
  klient wysyła `{"type":"subscribe_log","run_id":...,"tail":400}` → dostaje aktualny tail
  (`complete:true`), potem tylko dopływające linie (z obsługą rotacji/truncowania pliku);
  `unsubscribe_log` kończy subskrypcję.
- **`hello` / `pong`** — handshake i keepalive (`{"type":"ping"}`).

Gateway startuje w wątku z `engine.dashboard.main()` (`--ws-port 8767`, wyłączenie: `--no-ws`)
i wchodzi w skład usługi `mikowhy-teams-dashboard.service`.

## 5. Widoki w dashboardach

**Stary dashboard (HTML, :8765)** — kafle procesów, `/pipeline`, logi runów, przyciski
kill/force_retry/approve/decline/activate/boost na zadaniach.

**Centrum Dowodzenia (React, :8081, `centrum_dowodzenia/`)** — trzy zakładki:

- *Centrum Operacyjne* — kafle „Puls Fabryki” (żywe procesy, aktywne zadania, zacięcia,
  decyzje Sponsora, nieudane), lista **Żywe procesy** (z timera live, przycisk „Zabij” →
  `POST /api/pipeline/runs/<id>/kill`, rozwinięcie logu przez `LogViewer`), **Ostatnie runy**.
  Dane lecą pulsem przez WebSocket (fallback: polling REST co 2 s).
- *Agenci & Pipeline* — historia per rola (`/api/agents/<role>/pipeline`).
- *Decyzje Sponsora* — karty zadań czekających na akcję; przycisk zależy od statusu:
  `AWAITING_OWNER → „Ready”/„Odrzuć”`, `READY_TO_MERGE → „Merguj”`, `BLOCKED → „Ready full back”`
  (akcje przez `POST /api/flow/action`, walidacja `allowed_from` po stronie serwera).

`LogViewer` streamuje log runu przez WS (`subscribe_log`), z doklejaniem nowych linii;
fallback na pełny fetch REST.

## 6. Zabijanie procesów

- z UI (oba dashboardy): `POST /api/pipeline/runs/<run_id>/kill` → `sponsor_shell.kill_run`
  (`killpg` SIGTERM → SIGKILL, status `KILLED`);
- z CLI: `python3 -m engine.sponsor_shell --kill <RUN_ID>`;
- `force_retry` na zawieszonym RUNNING (requeue do READY, jawna regresja).

## 7. Uwaga o przepływie statusów zadań

Zadanie **nigdy nie cofa się samo**: `State.transition()` blokuje regresję w rangu pipeline
(`READY 0 → RUNNING/CHANGES_REQUESTED 1 → IN_REVIEW 2 → READY_TO_MERGE 3 → MERGED 4`),
chyba że wywołujący poda `allow_regression=True` (jawnie: Sponsor albo pauza tury workera).
Błędy procesów parkują zadanie w `BLOCKED` (strata czasu/konflikt/regresja po rebase),
decyzję „Ready full back” podejmuje Sponsor.

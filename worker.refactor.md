# WORKER REFACTOR & OBSERVABILITY — LOG IMPLEMENTACJI I WDROŻENIA

> Dokument śledzenia prac, decyzji architektonicznych oraz postępu wdrożenia (TODO checklist).  
> Projekt: `mikowhy-teams` (Dark Factory)  
> Data rozpoczęcia: 2026-11-24  
> Autor: Mikołaj Osowski <mikolaj.osowski@gmail.com>

---

## 1. Cel i Zakres Refaktoryzacji

Wyeliminowanie wąskiego gardła jednowątkowej kolejki LLM (`llm_queue`), usunięcie błędów dostępu do systemowego keyringa (`rc=3`), wprowadzenie pełnego podglądu konsoli agentów w czasie rzeczywistym oraz automatycznego watchdoga wykrywającego zawieszone procesy (Stall Watchdog).

---

## 2. SEKCJA TODO (Lista Kontrolna Implementacji i Wdrożenia)

### Faza 1: Architektura i Analiza Porównawcza
- [x] **1.1. Audyt wąskiego gardła LLM** — analiza czasu wywołań (~487s), flock na keyringu, wyznaczenie Opcji A′ (env-auth + dispatchery per model fast/strong).
- [x] **1.2. Analiza przechwytywania konsoli** — porównanie GNU `screen` vs Python `PTY` Streamer; wybór natywnego pseudoterminala (`pty.openpty()`).
- [x] **1.3. Specyfikacja 10-minutowej żywotności** — algorytm `ACTIVE` (<600s) vs `STALLED` (>=600s), ochrona przed fałszywym alarmem (`llm_busy`), dedup alertów.
- [x] **1.4. Zatwierdzenie dokumentu architektonicznego** — utworzenie `docs/WORKER_OBSERVABILITY_PLAN.md`.
  - *Commit:* `0b7bdf1` — `docs(plan): worker observability and parallel queue plan`

---

### Faza 2: Implementacja Silnika i Kolejki
- [x] **2.1. Eliminacja wyścigów keyringa (rc=3)** — `engine/llm_gateway.py`: bezwarunkowe ładowanie zmiennych `*_API_KEY` do `os.environ` z `.env` z pominięciem gnome-keyring.
  - *Commit:* `6b67afc` — `fix(llm): use env vars for API keys to eliminate keyring rc=3`
- [x] **2.2. Per-model Dispatchery i wydłużenie dzierżawy** — `engine/llm_queue.py`: `dispatch_loop_model`, funkcja `model_prefix_for` (`fast` vs `strong`), `LEASE_S = 900s`.
  - *Commit:* `cbfdd60` — `feat(llm-queue): per-model dispatchers and 900s lease time`
- [x] **2.3. PTY Console Streamer** — utworzenie `engine/console_streamer.py`, logowanie zdarzeń (`cmd`, `stdout`, `stderr`, `meta`), rotacja 50 MB, migracja `last_activity_at` w `engine/state.py`.
- [x] **2.4. Integracja Console Streamera w cyklu życia workera** — `engine/worker_lifecycle.py`: rejestrowanie każdego kroku (0..5) i wyjścia z procesu do `.agents/logs/console/<TASK>.jsonl`.
  - *Commit:* `40031fe` — `feat(console): PTY streamer and per-task console jsonl logging`
- [x] **2.5. Stall Watchdog w orkiestratorze** — `engine/orchestrator.py`: metoda `_stall_watchdog` w `tick()`, detekcja ciszy >=600s, brak duplikacji alertów, pomijanie zadań oczekujących na model (`llm_busy`), uruchomienie 2 wątków dispatchera w `run()`.
  - *Commit:* `40aa706` — `feat(orchestrator): stall watchdog with non-duplicating alerts`

---

### Faza 3: Dashboard i Wizualizacja Live
- [x] **3.1. Endpointy API podglądu konsoli i zdrowia workerów** — `engine/dashboard.py`:
  - `GET /api/workers/health` — status wszystkich aktywnych workerów, `idle_s`, `stall`, `llm_job_running`.
  - `GET /api/task/<id>/console` — odczyt zbuforowanego logu `.jsonl` z filtrowaniem (`tail`, `stream`, `since_ts`).
  - `POST /api/task/<id>/kill_and_retry` — wymuszenie restartu i ponowienia zadania.
  - `POST /api/task/<id>/dismiss_stall` — wyciszenie alertu stall dla danego zadania.
- [x] **3.2. Widok konsoli w panelu bocznym zadania** — nowa zakładka `🖥️ Konsola` w `#detail`, kolorowanie strumieni (`cmd` ❯ niebieski, `stderr` ⚠ czerwony, `meta` ℹ fioletowy, `stdout` biały), filtry strumieni, auto-scroll.
- [x] **3.3. Wskaźniki żywotności na kafelkach „Teraz pracują”** — zielony `🟢 output: Xs temu`, żółty `🟡 Czeka na model LLM`, czerwony `🔴 BEZ OUTPUTU: Xm (STALL)`.
  - *Commit:* `c50c064` — `feat(dashboard): live console view, health endpoints and stall badges`

---

### Faza 4: Weryfikacja i Testy Automatyczne
- [x] **4.1. Pakiet testów jednostkowych i integracyjnych** — utworzenie `tests/test_worker_console_observability.py`.
  - Test zapisu i odczytu `.jsonl` (format zdarzeń, timestampy, kolejność).
  - Test progów detekcji `ACTIVE` vs `STALLED`.
  - Test watchdoga orkiestratora i deduplikacji powiadomień.
  - Test odporności na `llm_busy` (brak fałszywych alarmów przy oczekiwaniu na odpowiedź LLM).
  - Test endpointów dashboardu `/api/task/<id>/console`, `/api/workers/health` i `/kill_and_retry`.
- [x] **4.2. Testy routingu kolejki modeli** — `tests/test_llm_queue.py`: `test_dispatch_model_routing_and_prefix`.
- [x] **4.3. Pełny regresyjny przelot pytest** — 103/103 testy zielone (100% pass).
  - *Commit:* `28289a8` — `test(observability): tests for console logging, stall watchdog and health`

---

### Faza 5: Wdrożenie Produkcyjne (Deployment)
- [ ] **5.1. Utworzenie i zatwierdzenie pliku śledzącego wdrożenie** — dodanie `worker.refactor.md` do repozytorium.
- [ ] **5.2. Scalenie gałęzi do `develop`** — bezpieczny merge gałęzi `feat/worker-observability-and-queue` do gałęzi integracyjnej `develop`.
- [ ] **5.3. Przeładowanie usług systemd** — restart `mikowhy-teams-dashboard` oraz `mikowhy-teams` (orchestrator).
- [ ] **5.4. Weryfikacja na żywo (Live Health Check)** — weryfikacja endpointu `/api/workers/health`, sprawdzenie logów orkiestratora pod kątem wątków `llm-fast` i `llm-strong`.
- [ ] **5.5. Weryfikacja konsoli na żywo w przeglądarce** — sprawdzenie czy nowe zadania poprawnie generują `.jsonl` i czy są widoczne w dashboardzie na porcie 8765.

---

## 3. Dziennik Zdarzeń i Wykonanych Kroków

| Data i Czas | Krok | Opis i Rezultat | Hash / Ref |
|---|---|---|---|
| 2026-11-24 12:24 | Faza 1 | Przygotowanie pełnego planu architektonicznego audytu workerów i kolejki | `0b7bdf1` |
| 2026-11-24 12:28 | Faza 2.1 | Naprawa odczytu kluczy API z `.env` z pominięciem gnome-keyring (`rc=3`) | `6b67afc` |
| 2026-11-24 12:28 | Faza 2.2 | Wdrożenie dispatcherów per-model (`fast` x 4, `strong` x 2) i `LEASE_S=900` | `cbfdd60` |
| 2026-11-24 12:32 | Faza 2.3-2.4 | Implementacja PTY Console Streamera i podpięcie do cyklu życia workerów | `40031fe` |
| 2026-11-24 12:35 | Faza 2.5 | Implementacja `_stall_watchdog` w orkiestratorze i uruchomienie 2 dispatcherów | `40aa706` |
| 2026-11-24 12:35 | Faza 3 | Wdrożenie API `/api/workers/health`, `/console` oraz UI terminala w dashboardzie | `c50c064` |
| 2026-11-24 12:38 | Faza 4 | Napisanie testów jednostkowych/integracyjnych; 103/103 testy zielone | `28289a8` |

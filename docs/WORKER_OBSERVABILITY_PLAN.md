# WORKER OBSERVABILITY PLAN — Dark Factory v2.8.x
## Architektura Workerów, Kolejkowania LLM i Live Console

> Dokument projektowy — głęboki audyt + specyfikacja zmian (bez kodu produkcyjnego).  
> Data: 2026-11-24 | Wersja silnika: 2.7.29+ → 2.8.1  
> Autor: Główny Architekt / Audyt kodu żywego systemu

---

## 0. Streszczenie Wykonawcze

| Obszar | Problem | Rekomendacja |
|--------|---------|-------------|
| Kolejka LLM | `flock` serializuje do 1 wywołania; ~487s/call → gigantyczny backlog | **Opcja A′**: eliminacja keyringu przez env vars + per-model dispatchery × 2 (fast/strong) równolegle |
| Console visibility | Workerzy piszą tylko do pliku `.log`, dashboard nie widzi co robią live | **Python PTY Streamer** → `.jsonl` per task + SSE endpoint |
| Stalled detection | Zadanie RUNNING może wisieć godzinami bez outputu — nikt tego nie widzi | Watchdog `mtime`-based + UI badge + Telegram alert @10 min + kill @15 min |

---

## 1. Audyt i Werdykt: Kolejka `llm_queue` vs Równoległe Wykonanie

### 1.1 Diagnoza obecnego stanu

```
Pomiar z logów (eksperyment 23–24.11.2026):
  Średni czas jednego wywołania vibe CLI: ~487s (~8 min)
  Dispatcher: dispatch_loop(max_jobs=2, poll_s=0.3)
  LEASE_S = 480s  (mniejszy niż czas wywołania strong! → natychmiastowe re-claim)
  wait_result timeout = 3600s (1h hard-cap)

Skutek: przy 10 workerach równolegle i jednym wątku dispatchera z max_jobs=2:
  Throughput = 2 × (3600 / 487) ≈ 14 jobów/h
  Ale: 10 workerów × 2 LLM-calle/worker/przebiegu = 20 pending jobów
  → Backlog rośnie szybciej niż jest opróżniany → worker timeout 3600s
```

**Pięć przyczyn ~487s i `rc=3` keyringa:**

1. **Systemowy keyring (gnome-keyring / libsecret)** — `vibe` czyta klucze API z keyringa przy każdym uruchomieniu. Równoczesne `subprocess` walczą o D-Bus → rc=3 "Reading API keys from the system keyring". `flock` na `.agents/llm.lock` to jedyna obecna ochrona.

2. **Zewnętrzne procesy nie respektują `flock`** — `visual_qa.py`, `news-video-generator` i inne narzędzia z ekosystemu wibe odpalają `vibe` bez blokady. Każde takie wywołanie może wyrzucić rc=3 środkowemu dispatcherowi.

3. **LEASE_S=480 ≤ średni czas wywołania** — `strong-model` potrafi trwać nawet 500–600s. Gdy dispatcher skończy job po 490s, `_reclaim_expired` właśnie go „reclamuje" → duplikat RUNNING → double-invoke.

4. **max_jobs=2 w `dispatch_loop`** — seryjny dispatcher z limitem 2 job/cykl i poll_s=0.3s to artifical bottleneck. Jedyne co to robi: nie przeciąża rate-limitu API (MIN_INTERVAL_S=0.3s). Ale z jednym wątkiem i tak nie możemy uruchomić 2 jobów równolegle — idą jeden po drugim.

5. **Brak podziału fast/strong** — `zai-glm-5-3-flash` odpowiada w ~30s, `mistral-large-4-0` w ~400s. Jeden dispatcher obsługuje oba typy, więc szybkie joby czekają za wolnymi.

### 1.2 Trzy Opcje — Twarde Porównanie

#### Opcja A: Naprawiona kolejka SQLite (per-model dispatchers)

**Mechanizm:**
- Eliminacja keyringu: `MISTRAL_API_KEY`, `GLM_API_KEY` → `.env` → `_load_dotenv()` → `os.environ` → subprocess dostaje klucz wprost. `vibe` preferuje env vars nad keyringiem gdy `MISTRAL_API_KEY` jest ustawiony.
- Usunięcie `flock` po eliminacji keyringu → 2 dispatcher-wątki równolegle: `dispatch_loop_fast(max_jobs=4)` i `dispatch_loop_strong(max_jobs=2)`.
- `LEASE_S = 900s` (margines 50% ponad max model timeout).

**Przepustowość (brak GPU, CPU-only, 16 GB RAM):**
```
Fast model  (30s/call):  4 × (3600/30)  = 480 jobów/h
Strong model (400s/call): 2 × (3600/400) = 18 jobów/h
Łącznie ≈ 500 jobów/h
```

| Zaleta | Wada |
|--------|------|
| Priorytety P0→P3, dedup, cache TTL, retry z backoff — gotowe | Wymaga działającego env-based auth w vibe |
| Pełna widoczność w dashboardzie (ops ledger) | Dwa wątki → 2 połączenia SQLite (WAL bez problemu) |
| Batching jobów tego samego modelu (max 20k znaków) | Zmiany minimalne w llm_queue.py |
| Łatwe dodanie trzeciego dispatchera (np. `embedding`) | — |
| Zero zombie-sesji | — |

#### Opcja B: Bezpośrednie wywołania z semaforem

**Mechanizm:**
- Worker wywołuje vibe CLI bezpośrednio z `asyncio.Semaphore(4)` lub `threading.BoundedSemaphore(4)`.
- Brak SQLite overhead, brak dispatchera.

| Zaleta | Wada |
|--------|------|
| Prostota kodu | Zero priorytetów — P0 i P3 czekają tak samo |
| Niższe opóźnienie (brak poll 0.25s) | Brak dedup, brak cache, brak retry/backoff |
| — | Brak widoczności (dashboard nie widzi pendingów) |
| — | Race conditions przy restartach (semafor w pamięci, nie trwały) |
| — | Keyring problem NIE jest rozwiązany |

#### Opcja C: Architektura hybrydowa

Dispatching przez SQLite (trwałość, priorytety) + bezpośrednie wywołania z workerów dla `fast-model` gdy kolejka pusta (shortcut).

**Rzeczywistość:** dodaje skomplikowanie bez istotnego zysku przepustowości. Uzasadnione tylko gdy latencja fast-model jest krytyczna (<5s).

---

### 1.3 ✅ WERDYKT: Opcja A′ (Naprawiona kolejka + env auth)

```
Opcja A′ = Opcja A + eliminacja keyringu + per-model dispatchers
```

**Uzasadnienie:**

1. **Keyring to główna przyczyna rc=3** — env var jest prostą, bezpieczną naprawą. `vibe` szanuje `MISTRAL_API_KEY` / `GLM_API_KEY` z env, nie wywołuje keyringu gdy klucz jest dostępny.

2. **Kolejka SQLite jest właśnie tym czego potrzebujemy** — trwałość przez restart, priorytety P0→P3, dedup, cache, retry/backoff, widoczność w dashboardzie. Opcja B traci to wszystko.

3. **Dwa dispatchery fast+strong** → bez wzajemnego blokowania. Fast model (30s) nie czeka za strong (400s).

4. **`LEASE_S = 900s`** → koniec re-claim race'ów.

5. **Wymagana zmiana w `.env` i `llm_gateway.py`:** usunąć `_llm_process_lock()` gdy klucze są w env (albo zredukować do no-op locka).

**Schemat nowej architektury kolejkowania:**

```
orchestrator.py run():
  ├── threading.Thread(target=dispatch_loop_fast,  kwargs={max_jobs:4, model:"fast"})
  └── threading.Thread(target=dispatch_loop_strong, kwargs={max_jobs:2, model:"strong"})

worker_lifecycle.py:
  _llm_step() → llm_queue.enqueue() → wait_result()
  (bez zmian — kolejka obsługuje routing per model automatycznie przez claim_batch)

llm_gateway.py:
  _llm_process_lock() → zastąpić per-model semaforem (lub usunąć gdy env-auth pewny)
  _load_dotenv() → wymusić MISTRAL_API_KEY / GLM_API_KEY do os.environ (dziś jest setdefault)
```

---

## 2. Analiza Porównawcza: GNU `screen` vs Python PTY Streamer

### 2.1 Wymagania funkcjonalne

Dla każdego workera chcemy:
- 📺 **Live stream** w dashboardzie (przeglądarka + terminal)
- 📜 **Pełna historia** z timestampami per komenda
- 🔎 **Detekcja aktywności** (kiedy ostatni bajt outputu)
- 🛠️ **Ręczna interwencja** człowieka w razie awarii
- 🧹 **Brak zombie-sesji** po zakończeniu zadania

### 2.2 Tabela porównawcza

| Kryterium | GNU `screen` | Python PTY Streamer |
|-----------|-------------|---------------------|
| **Przechwytywanie każdego bajtu** | ✅ `-L -Logfile` loguje wszystko | ✅ `pty.openpty()` — każdy bajt przez pipe |
| **Timestamps per bajt** | ❌ `screen` loguje bez TS; potrzeba wrapper-skripta | ✅ Python dodaje `time.time()` przy każdym `os.read()` |
| **Format wyjścia** | Binarny log z ANSI codes — parsowanie trudne | ✅ `.jsonl` — każde zdarzenie to JSON z `ts`, `stream`, `data` |
| **Live w przeglądarce (SSE)** | ⚠️ Wymaga tail-daemona czytającego plik | ✅ Natywny: PTY reader thread → SSE endpoint bezpośrednio |
| **Ręczna interwencja terminala** | ✅ `screen -r TASK-0022` — natywne | ⚠️ `socat` lub dedykowany `/api/task/<id>/attach` WebSocket |
| **Zombie-sesje** | 🔴 `screen -ls` może zostawić sesje po crashu orchestratora | ✅ PTY fd zamykany przez Python w `finally` |
| **Detekcja aktywności (`mtime`)** | ⚠️ Plik .log aktualizowany (screen -L), ale mtime nietrwałe | ✅ `.jsonl` aktualizowany przy każdym bajcie → `Path.stat().st_mtime` |
| **Debugowanie crashu**| ✅ Sesja żyje po crashu workera | ⚠️ `.jsonl` jest ostatnim zapisem przed crashem |
| **Złożoność wdrożenia** | 🔴 Wymaga `screen` zainstalowany; osobne PID/sesja per task | ✅ stdlib `pty`, `os`, `select` — zero zależności zewnętrznych |
| **Interaktywne wejście (stdin)** | ✅ `screen -S TASK -X stuff "..."` | ✅ Zapis do PTY master — WebSocket → stdin |
| **Konfigurowalność filtru** | ❌ screen loguje wszystko w całości | ✅ Per-event filtering w dashboardzie (komendy vs stdout vs stderr) |
| **Obsługa kolizji nazw sesji** | 🔴 `screen -S CHAT-0022` — konflikty przy retry | ✅ fd per process — brak globalnych nazw |
| **System units** | Każda sesja = osobny socket screen | PTY fd w pamięci procesu workera |

### 2.3 ✅ WERDYKT: Python PTY Streamer

**Uzasadnienie:**

1. **Timestamps są kluczowe** — `screen -L` nie dodaje TS do logfile. Wrapper-skrypt logujący co sekundę traci sub-sekundową precyzję. PTY reader zapisuje `ts` przy każdym `os.read()` (granularność ~ms).

2. **Parsowanie przez dashboard** — `.jsonl` z polami `{ts, stream, step, cmd, output, rc}` jest natywnie JSON. `screen` log zawiera ANSI escape codes, `\r\n` mieszanie, komendy screen's control chars.

3. **Zombie-sesje** — `screen` może zostawić martwe sesje przy SIGKILL orchestratora. PTY fd jest zamykany przez GC Pythona.

4. **Zero zewnętrznych zależności** — `pty` jest w stdlib od Python 2.7.

**Kompromis dla ręcznej interwencji:**
- WebSocket endpoint `/api/task/<id>/attach` → zapis do PTY master fd (stdin workera).
- Alternatywa niskopriorytetowa: opcjonalny `screen` wrapper uruchamiany tylko gdy `WORKER_SCREEN=1` w `.env`.

---

## 3. Specyfikacja Mechanizmu „10-minutowej Żywotności" (Console Activity Heartbeat)

### 3.1 Definicje formalne

```
ACTIVE   := task.status == RUNNING
           AND (now() - last_console_activity_at) < STALL_THRESHOLD_S
           
STALLED  := task.status == RUNNING
           AND (now() - last_console_activity_at) >= STALL_THRESHOLD_S
           AND STALL_THRESHOLD_S = 600  # 10 min (konfigurowalne: kv stall_threshold_s)
```

**`last_console_activity_at`** = max(mtime `.agents/logs/console/<TASK>.jsonl`, mtime `.agents/logs/worker_lifecycle.<TASK>.log`)

### 3.2 Pomiar bez przeciążania SQLite

**Strategia: `mtime` + rzadki zapis do DB**

```python
# PTY Reader Thread (w procesie workera):
while True:
    data = os.read(master_fd, 4096)   # blokuje do danych
    now = time.time()
    # Zapis do .jsonl (każdy chunk) → mtime aktualizowane automatycznie
    jsonl_file.write(json.dumps({...}) + "\n")
    jsonl_file.flush()
    # DB: aktualizuj `last_activity_at` max co 30s (nie co bajt!)
    if now - _last_db_write > 30:
        state._conn().execute(
            "UPDATE tasks SET last_activity_at=? WHERE id=?", (now, task_id))
        _last_db_write = now

# Dashboard API: nie czyta DB — sprawdza mtime pliku (filesystem stat)
def _console_last_activity(task_id: str) -> float:
    jsonl = CONSOLE_DIR / f"{task_id}.jsonl"
    log   = LOG_DIR / f"worker_lifecycle.{task_id}.log"
    mtimes = []
    for p in (jsonl, log):
        try: mtimes.append(p.stat().st_mtime)
        except FileNotFoundError: pass
    return max(mtimes) if mtimes else 0.0
```

**Dlaczego `mtime`, nie SQL?**
- `stat()` = jeden syscall, brak lock contention na SQLite WAL.
- Dashboard odpytywany co 2s przez ~20 klientów jednocześnie — `mtime` jest bezpieczne.
- SQLite `last_activity_at` to fallback gdy plik nie istnieje (crash / stary task).

### 3.3 Algorytm detekcji STALLED

```python
# Co tick orchestratora (co 1s) — wbudowany w _escalate_stuck_tasks():

for task in state.list_tasks(status=RUNNING):
    last_act = _console_last_activity(task["id"])   # mtime
    idle_s   = now - last_act
    
    if idle_s >= stall_threshold_s:  # 10 min
        # Oznacz w kv (unikaj spamu per tick)
        kv_key = f"stall_notified:{task['id']}"
        last_notif_bucket = int(state.get_kv(kv_key, 0) or 0)
        cur_bucket = round(idle_s / 60)
        if last_notif_bucket != cur_bucket:
            state.set_kv(kv_key, cur_bucket)
            state.add_notification("stall", f"⏳ {task['id']} bez outputu {idle_s//60:.0f}m", task["id"])
            # Telegram alert jeśli TELEGRAM_PUSH=1
        
        if idle_s >= stall_kill_threshold_s and state.get_kv("auto_stall_kill"):
            os.kill(running_proc.pid, signal.SIGTERM)
            state.transition(task["id"], COOLING, review_notes="auto-kill: stall 15 min")
            state.record_metric("stall_kill", {"idle_s": idle_s}, task["id"])
```

### 3.4 Ścieżka eskalacji

```
t=0     Worker START  → plik .jsonl tworzony, mtime = now
t=0..9  Normal output → mtime aktualizowany co chunk PTY
t=10m   BRAK outputu  → 🟡 STALL DETECTED (idle_s >= 600)
        ├── Dashboard: badge "🔴 10m 00s bez outputu" na karcie agenta
        ├── Bell 🔔: powiadomienie "⏳ CHAT-0022 nie reaguje od 10 min"
        └── Telegram: "⚠️ CHAT-0022 stall 10 min — sprawdź konsolę"

t=15m   NADAL brak     → 🔴 AUTO-KILL (jeśli kv auto_stall_kill=1)
        ├── SIGTERM → proc
        ├── COOLING (65 min freeze)
        ├── Zrzut ostatnich 500 wierszy .jsonl → .agents/stall_dumps/<TASK>.<ts>.jsonl
        └── Telegram: "🔴 CHAT-0022 ubity (stall 15 min). Dump: .agents/stall_dumps/"

t=15m   (jeśli auto_stall_kill=0 — tryb bezpieczny):
        └── Kolejne alerty co 5 min + badge migający w UI

Human action buttons:
  🔍 Zbadaj log konsoli → otwiera /api/task/<TASK>/console?tail=200
  🔄 Zabij i wznów     → POST /api/task/<TASK>/kill_and_retry
  ⏸️ Zostaw alive      → POST /api/task/<TASK>/dismiss_stall (wycisza alerty na 30 min)
```

> **Uwaga: fałszywe alarmy przy LLM wait**  
> Gdy task ma aktywny job w `llm_jobs` z `status=RUNNING` — idle_s z konsoli jest normalny (vibe nie pisze do PTY podczas przetwarzania). Stall watchdog musi sprawdzać: `idle_s >= stall_threshold AND NOT (task_id in llm_jobs WHERE status='RUNNING')` → prawdziwy stall.

---

## 4. Specyfikacja Zmian w Kodzie i API

### 4.1 Format danych logu konsoli

**Plik:** `.agents/logs/console/<TASK_ID>.jsonl`

Każda linia = jedno zdarzenie JSON (UTF-8, `\n`-delimited):

```jsonc
// Zdarzenie: wejście (komenda uruchomiona przez worker)
{
  "ts": 1732450800.123,       // float Unix timestamp (ms precyzja)
  "stream": "cmd",            // "cmd" | "stdout" | "stderr" | "meta"
  "step": "2/5_IMPL",        // aktualny substep workera
  "cmd": "vibe --model fast-model ...",  // uruchamiana komenda (tylko gdy stream=cmd)
  "data": null,               // surowy output (gdy stream=stdout/stderr)
  "rc": null                  // exit code (gdy stream=meta, zdarzenie "exit")
}

// Zdarzenie: output
{
  "ts": 1732450802.456,
  "stream": "stdout",
  "step": "2/5_IMPL",
  "cmd": null,
  "data": "Writing engine/worker_lifecycle.py...\n",
  "rc": null
}

// Zdarzenie: zakończenie komendy
{
  "ts": 1732451290.789,
  "stream": "meta",
  "step": "2/5_IMPL",
  "cmd": null,
  "data": "process exit",
  "rc": 0
}
```

**Rotacja:** plik tworzony przy starcie workera (overwrite), nie rośnie ponad 50 MB (PTY reader: `maxbytes=50_000_000`, po przekroczeniu: `truncate + nagłówek "--- rotated ---"`).

**Lokalizacja globalna:** `ROOT / ".agents" / "logs" / "console" / f"{task_id}.jsonl"`

---

### 4.2 Nowe endpointy w `engine/dashboard.py`

#### `GET /api/task/<id>/console`

Parametry query string:
- `tail=N` — ostatnie N linii (default 200, max 2000)
- `since_ts=FLOAT` — tylko zdarzenia z `ts > since_ts` (do pollingowego SSE)
- `stream=cmd|stdout|stderr|meta` — filtrowanie po typie zdarzenia

Odpowiedź:
```json
{
  "task_id": "CHAT-0022",
  "file_path": ".agents/logs/console/CHAT-0022.jsonl",
  "total_lines": 1842,
  "last_activity_at": 1732451290.789,
  "idle_s": 47.3,
  "stall": false,
  "events": [
    {"ts": 1732450800.123, "stream": "cmd", "step": "2/5_IMPL", "cmd": "vibe ...", "data": null, "rc": null},
    {"ts": 1732450802.456, "stream": "stdout", "step": "2/5_IMPL", "cmd": null, "data": "Writing...\n", "rc": null}
  ]
}
```

#### `GET /api/workers/health`

```json
{
  "workers": [
    {
      "task_id": "CHAT-0022",
      "status": "RUNNING",
      "substep": "2/5_IMPL",
      "pid": 12345,
      "alive": true,
      "last_activity_at": 1732451290.789,
      "idle_s": 47.3,
      "stall": false,
      "llm_job_running": true,
      "stall_threshold_s": 600,
      "console_lines": 1842
    }
  ],
  "stalled": ["CHAT-0019", "BRAND-001"],
  "stall_threshold_s": 600
}
```

#### `POST /api/task/<id>/kill_and_retry`

Body: `{}` lub `{"reason": "manual stall kill"}`

Akcja: `os.kill(pid, SIGTERM)` → `state.transition(id, COOLING, ...)` → `state.record_metric("manual_kill", ...)`

Odpowiedź: `{"ok": true, "task_id": "...", "new_status": "COOLING"}`

#### `POST /api/task/<id>/dismiss_stall`

Body: `{"minutes": 30}`

Akcja: `state.set_kv(f"stall_dismissed:{task_id}", time.time() + minutes*60)`

#### `GET /api/task/<id>/console/stream` (SSE)

```
Content-Type: text/event-stream

data: {"ts":1732450800.123,"stream":"stdout","step":"2/5_IMPL","data":"Writing..."}
data: {"ts":1732450802.456,"stream":"meta","data":"process exit","rc":0}
```

Implementacja: polling `mtime + seek` co 0.5s lub Linux `inotify`.

---

### 4.3 Projekt widoku konsoli w dashboardzie UI

#### Panel szczegółów zadania (prawy drawer `#detail`) — nowa zakładka 🖥️ Konsola

```
┌─────────────────────────────────────────────────────┐
│ CHAT-0022 — 🔄 RUNNING  2/5_IMPL                  │
│ 🟢 ostatni output: 47s temu  [🔍 Zbadaj][🔄 Kill] │
├─────────────────────────────────────────────────────┤
│ [filtr: ▣cmd  ▣stdout  ▣stderr]  [tail: 200 ▼]    │
├─────────────────────────────────────────────────────┤
│ ┌── TERMINAL ────────────────────────────────────┐  │
│ │ 12:14:03 $ [2/5_IMPL] vibe --model fast ...   │  │
│ │ 12:14:35 · Writing engine/worker_lifecycle.py  │  │
│ │ 12:14:35 · done.                               │  │
│ │ 12:14:35 ✓ rc=0                                │  │
│ │ 12:15:01 $ [4/5_QG] ruff check .              │  │
│ │ 12:15:02 · engine/worker_lifecycle.py:42:5 ... │  │
│ │ 12:15:02 ✗ rc=1                                │  │
│ │ ■ [2/5_IMPL] vibe CLI … ← spinner gdy RUNNING │  │
│ └────────────────────────────────────────────────┘  │
│ 🔴 STALLED: 10m 22s bez outputu                    │
└─────────────────────────────────────────────────────┘
```

**Kolorystyka linii:**
- `cmd` → `color: #58a6ff` (niebieskie — komendy)
- `stdout` → `color: #c9d1d9` (białe — normalny output)
- `stderr` → `color: #f85149` (czerwone — błędy)
- `meta rc=0` → `color: #3fb950` (zielone — sukces)
- `meta rc!=0` → `color: #f85149` (czerwone — porażka)

#### Karta workera `#now` (sekcja „Teraz pracują") — rozszerzenie

```
┌──────────────────────────────────┐
│ 🔄 CHAT-0022  2/5_IMPL           │
│ solutions-architect               │
│ 🕐 12:14:03  ⏱ 47m 22s         │
│ 📡 Teraz: vibe CLI (fast) …      │  ← ostatnie zdarzenie z .jsonl
│ 🟢 output: 47s temu              │  ← idle_s < stall_threshold_s
└──────────────────────────────────┘

(gdy stall):
│ 🔴 BEZ OUTPUTU: 10m 22s          │
│ [🔍 Zbadaj log] [🔄 Kill & Retry] │
└──────────────────────────────────┘
```

**Badge logic (konsola + llm_job):**
- `idle_s < 120` → 🟢 AKTYWNY
- `120 <= idle_s < 600 AND llm_job_running` → 🟡 Czeka na LLM (normalny stan)
- `120 <= idle_s < 600 AND NOT llm_job_running` → 🟡 Pauza (sprawdź)
- `idle_s >= 600` → 🔴 STALLED

#### Nowa pod-zakładka w `🗂️ Kolejka & Logi`: `⚙️ Workers Health`

Tabela wszystkich running workerów:

| Task ID | Substep | PID | Last output | LLM job | Status | Akcje |
|---------|---------|-----|-------------|---------|--------|-------|
| CHAT-0022 | 2/5_IMPL | 12345 🟢 | 47s temu | 🤖 RUNNING | 🟡 Czeka LLM | — |
| BRAND-001 | 4/5_QG | 11111 🟢 | 10m 22s temu | — | 🔴 STALL | 🔍 🔄 |

---

## 5. Zmiany w `engine/state.py` — Nowe pola tabeli `tasks`

```sql
-- Migracja addytywna (ALTER TABLE IF COLUMN NOT EXISTS):
ALTER TABLE tasks ADD COLUMN last_activity_at REAL;
-- Zapis: worker PTY reader co 30s; dashboard_api odczytuje (lub mtime)
```

---

## 6. Plan Wdrożenia — Atomowe Kroki

### Priorytet 1: Keyring fix

```
Cel: eliminacja rc=3 — bez restartu orchestratora możliwe

Krok 1A: .env
  Dodaj: MISTRAL_API_KEY=sk-...
         GLM_API_KEY=...

Krok 1B: llm_gateway.py::_load_dotenv()
  Zmień linię setdefault dla API_KEY na bezwarunkowe przypisanie:
    if key.endswith("_API_KEY") or key.endswith("_KEY"):
        os.environ[key] = value  # wygrywa nad keyringiem
    else:
        os.environ.setdefault(key, value)

Krok 1C: llm_gateway.py::_llm_process_lock()
  LEASE_S: 480 → 900 (margines 50% nad max call time)
  Dodaj log gdy lock oczekuje > 60s

Commit: fix(llm): use env vars for API keys, bypass system keyring rc=3
```

### Priorytet 2: Per-model dispatchers

```
Cel: 2× throughput bez zmian w workersach

Krok 2A: llm_queue.py
  dispatch_loop_model(state, model_prefix: str, max_jobs: int, poll_s: float):
    = dispatch_loop, ale claim_batch filtruje model LIKE 'fast%' / 'strong%'
    Fallback: gdy brak jobów danego modelu → claim_next() (jakikolwiek job)

Krok 2B: orchestrator.py::run()
  threading.Thread(target=llm_queue.dispatch_loop_model,
                   args=(state, "fast", 4, 0.3), daemon=True).start()
  threading.Thread(target=llm_queue.dispatch_loop_model,
                   args=(state, "strong", 2, 0.5), daemon=True).start()
  # Stary dispatch_loop pozostaje jako fallback przez 1 tydzień, potem usunąć

Commit: feat(llm-queue): per-model dispatchers fast×4 strong×2, LEASE_S=900s
```

### Priorytet 3: PTY Console Streamer

```
Cel: pełna historia konsoli per task w .jsonl

Krok 3A: Nowy moduł engine/console_streamer.py
  class ConsoleStreamer:
    __init__(task_id, state)   # otwiera jsonl w trybie append
    wrap_popen(args, cwd, env, step, cmd_label) -> Popen
      (pty.openpty() → reader_thread → jsonl + db co 30s)
    log_meta(step, event, rc=None)   # zdarzenia bez TTY (np. zmiana substep)
    close()

Krok 3B: worker_lifecycle.py
  Inicjalizacja: streamer = ConsoleStreamer(task_id, state) po claim
  Każde subprocess.run() w _llm_agent_loop → streamer.wrap_popen()
  Komendy step1–5: streamer.log_meta(step, "starting")
  Cleanup w finally: streamer.close()

Krok 3C: Migracja DB
  state.py::ensure_schema(): ALTER TABLE tasks ADD COLUMN last_activity_at REAL

Commit: feat(console): PTY streamer per-task .jsonl console log with timestamps
```

### Priorytet 4: Dashboard API + UI

```
Krok 4A: dashboard.py — backend
  GET /api/task/<id>/console
  GET /api/workers/health
  POST /api/task/<id>/kill_and_retry
  POST /api/task/<id>/dismiss_stall

Commit: feat(dashboard): console log and workers health API endpoints

Krok 4B: dashboard.py — frontend JS
  Panel szczegółów: nowa zakładka "🖥️ Konsola"
  Polling /api/task/<id>/console?since_ts=X co 2s
  Renderowanie kolorowe + spinner RUNNING

Commit: feat(dashboard-ui): live console tab in task detail panel

Krok 4C: dashboard.py — frontend JS
  Karty #now: pole "last output: Xs temu" + badge 🟢/🟡/🔴
  Nowa pod-zakładka "⚙️ Workers Health" z tabelą + akcje Kill/Dismiss

Commit: feat(dashboard-ui): stall badges and workers health tab with kill actions
```

### Priorytet 5: Stall Watchdog + Telegram

```
Krok 5A: orchestrator.py
  def _stall_watchdog():
    dla każdego RUNNING task:
      idle_s = now - _console_last_activity(task_id)
      llm_busy = llm_jobs_running(state, task_id)
      if idle_s >= stall_threshold and not llm_busy:
        → notification + Telegram
      if idle_s >= kill_threshold and auto_kill and not llm_busy:
        → SIGTERM + COOLING + stall_dump

  Wywołanie w tick() po _reap()

Krok 5B: kv-params (konfigurowalne z dashboardu lub przez kv set):
  stall_threshold_s       = 600  (10 min)
  stall_kill_threshold_s  = 900  (15 min)
  auto_stall_kill         = 0    (bezpieczny tryb — domyślnie wyłączony)

Commit: feat(orchestrator): stall watchdog, Telegram alert @10m, optional auto-kill @15m
```

---

## 7. Mapa zależności kroków

```
Krok 1 (Keyring) ──► Krok 2 (Dispatchers) ──► (opcjonalnie) Krok 3 (PTY)
                                                      │
                                              Krok 4A (API) ──► Krok 4B (UI Tab)
                                                      │               │
                                              Krok 5 (Watchdog) ──► Krok 4C (Badges)
```

**Punkty kontrolne (gate'y):**

| Po kroku | Test akceptacyjny |
|----------|-------------------|
| 1 | `vibe --prompt-file /dev/null` → rc=0 bez rc=3; `dispatch_loop` bez lock timeout |
| 2 | 2 wątki widoczne w `ps aux`; `llm_jobs` QUEUED opróżniany 2× szybciej |
| 3 | `.agents/logs/console/TEST.jsonl` istnieje po uruchomieniu workera; `mtime` aktualizowany |
| 4A | `curl localhost:8765/api/workers/health` → valid JSON z running tasks |
| 4B+4C | Zakładka Konsola + badges widoczne; logi ładują się; filtrowanie działa |
| 5 | Po ręcznym `sleep 11m` w workerze → badge 🔴 i notyfikacja Telegram |

---

## 8. Uwagi Implementacyjne i Ryzyka

> **⚠️ Keyring migration** — jednorazowa, nieodwracalna. Przed krokiem 1A zapisz klucze:
> `python -c "import keyring; print(keyring.get_password('vibe', 'MISTRAL_API_KEY'))"`

> **⚠️ PTY i ANSI** — `pty.openpty()` tworzy TTY; programy mogą zachowywać się inaczej (tqdm, pytest progress bar). Strip ANSI codes przy zapisie do `.jsonl`: `re.sub(r'\x1b\[[0-9;]*m', '', data)`.

> **ℹ️ SQLite WAL + wiele wątków** — `dispatch_loop_fast` i `dispatch_loop_strong` używają `State()` każdy z osobna (osobne połączenia w WAL mode). Upewnij się że `PRAGMA journal_mode=WAL` jest ustawione w `state.py::ensure_schema()`.

> **💡 Console `.jsonl` a duże zadania** — przy 500+ turach LLM plik może rosnąć do 50+ MB. Rotacja (truncate + nagłówek) jest wystarczająca — dashboard pobiera tylko `tail=200`.

> **ℹ️ Conventional Commits** — zgodnie z `AGENTS.md`: single-line, format `type(scope): description` (max 72 znaki), bez stopek, bez `Co-Authored-By`. Committy z tego planu są już sformatowane.

---

## 9. Metryki sukcesu (KPI po wdrożeniu)

| Metryka | Przed | Cel |
|---------|-------|-----|
| Throughput kolejki LLM | ~14 job/h (1 dispatcher) | ≥ 400 job/h (2 dispatchers) |
| rc=3 keyring errors | sporadyczne przy >3 workerach | 0 (env auth) |
| Czas od job QUEUED do RUNNING | 0–3600s (backlog) | ≤ 60s (p95) |
| Stall detection latency | — (brak) | ≤ 10 min + 30s poll |
| Dashboard console lag | — (brak live) | ≤ 2s (polling) / <100ms (SSE) |
| Zombie-task recovery | zombie-recovery przy restarcie | real-time stall kill @15 min |

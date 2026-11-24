# flow.md — pełny przepływ danych w mikowhy-teams (Dark Factory)

> Wygenerowane 2026-11-24 po audycie eksperymentu nr 1 (zadania kręcące się w RUNNING, nie dochodzące do merge).
> Źródła: `engine/orchestrator.py`, `engine/worker_lifecycle.py`, `engine/llm_queue.py`, `engine/llm_gateway.py`, `engine/quality_gates.py`, `engine/git_guard.py`, `.agents/state.db`.

## 1. Procesy systemowe (co stale chodzi na maszynie)

```
systemd --user
├── mikowhy-teams.service          → python3 -m engine.orchestrator run --poll 1
├── mikowhy-teams-dashboard.service → python3 -m engine.dashboard --port 8765 --host 0.0.0.0
└── (procesy potomne uruchamiane przez orchestratora — patrz niżej)
```

| Proces | Kto go stawia | Rola |
|---|---|---|
| `engine.orchestrator run --poll 1` | systemd (Restart=always) | tick co 1s, wszystkie fazy, dispatcher LLM w osobnym wątku, `telegram-poll` w tym samym ticku |
| `engine.dashboard` | systemd | serwer HTTP + UI; tylko czyta `state.db` (i akcje sponsor może mutować przez API) |
| `engine.telegram-poll` | ręcznie/człowiek (osobny proces) | alternatywny odbiór wiadomości Telegram |
| `engine.worker_lifecycle <TASK>` | orchestrator, `_spawn()` co tick | jeden proces Pythona na jedno zadanie; wykonuje plan→impl→testy→QG→commit→push; log: `.agents/logs/worker_lifecycle.<TASK>.log` |
| `engine.code_review <TASK>` | orchestrator, `_spawn()` | niezależny Code Review zadania w IN_REVIEW |
| `vibe-rs` (LLM CLI) | worker / dispatcher / gateway | pod **`_llm_process_lock()`** (flock na `.agents/llm.lock`) — keyring nie jest process-safe |
| `visual_qa.py` i inne zewn. | news-video-generator itd. | **NIE** respektują flocka silnika — to realne źródło race'ów rc=3 |

## 2. Statusy zadania (state machine, `engine/state.py`)

```
AWAITING_APPROVAL ──(auto-approve, approval_policy=auto)──▶ READY
READY ──claim──▶ RUNNING ──┬─(QG OK + commit + push + PR)──▶ IN_REVIEW
                           ├─(QG fail, 3 próby)──▶ FAILED ──(retry<3 / audyt)──▶ READY
                           ├─(wyjątek w workerze)─▶ COOLING ──(sleep minie)──▶ READY
                           ├─(konflikt origin / pusty diff)─▶ READY
                           └─(retry>=3, circuit)──▶ AWAITING_OWNER / koniec kolejki
IN_REVIEW ──(CR: approve)──▶ READY_TO_MERGE ──(rebase + regresja + merge + push)──▶ MERGED
IN_REVIEW ──(CR: reject)──▶ CHANGES_REQUESTED ──▶ RUNNING (pętla poprawek)
```

Stan magazynowany w **`.agents/state.db`** (SQLite WAL, połączenie per wątek). Snapshot czytelny dla człowieka: `.agents/pipeline.json`.

## 3. Tick orchestratora — dokładna kolejność (co 1s)

`engine/orchestrator.py → tick()`:

1. `_reap()` — zbieranie zakończonych procesów workerów (`process_exit rc`).
2. `_escalate_stuck_tasks()` — zadania utknięte w RUNNING/przekroczone eta.
3. `_auto_approve()` — AWAITING_APPROVAL → READY (patrz §uruchamianie).
4. `_auto_retry_failed()` — FAILED/BLOCKED/ESCALATED → READY (audyt + retry).
5. `_workload_guardian_tick()` — People Ops: obciążenie ról, hourly.
6. `_launch_recurring()` — seeding zadań cyklicznych (recurring_tasks).
7. `_dispatch_llm_queue()` — awaryjny `dispatch_once(max_jobs=1)` co 30 ticków (normalnie robi to wątek dispatch_loop).
8. `_launch_workers()` — chwytanie zadań do pracy (patrz §uruchamianie).
9. `_launch_reviews()` — sekwencje `engine.code_review` dla IN_REVIEW.
10. `_circuit_breakers()` — po max retrejs → koniec kolejki / AWAITING_OWNER.
11. `_stale_queue_watchdog()` — READY >8h → P1; BLOCKED >48h → auto-recovery.
12. `_handle_merges()` — **merge READY_TO_MERGE → master** (patrz §merge).
13. `_cleanup_merged_branches()` — usuwanie `feature/*` zmergowanych do mastera.
14. `_handle_milestones()` — QG-3 na branchu milestonu → release merge → master.
15. `_notify_approvals()` — pakiety decyzji dla Sponsora.
16. `_watchdog()` / `_maybe_health()` / `_poll_telegram()`.

Główna pętla działa pod `flock` na `.agents/orchestrator.lock` (jedna instancja).

## 4. Życie pojedynczego zadania — krok po kroku

Każdy krok wykonywany jest przez **jeden proces** `python3 -m engine.worker_lifecycle TASK` (cwd = repo ROOT; wychodzi po jednym przebiegu — orchestrator szykuje kolejny przy retry):

```
run(task_id)
0. claim_task (READY→RUNNING, claimed_by=worker-<pid>) + temat Telegram
0b. step0_prepare: utwórz git worktree .agents/worktrees/<projekt>/<TASK> z base (milestone branch albo master)
1. step1_plan    [LLM]   → plan podzadań → substep 1/5_PLAN
2. step2_implement[LLM]  → kod wg JSON {"files": {path: content}} → substep 2/5_IMPL
3. step3_tests   [LLM]   → pliki testów → substep 3/5_TEST
4. step4_quality_gate    → substep 4/5_QG:
     4a. PRE-FLIGHT [LLM] — samokontrola przed QG (mniej iteracji)
     4b. local_gates(worktree): ruff, mypy, phpstan (jeśli obecne), pytest tests (300s)
         → przy błędzie maks. 3 próby fix (LLM) na miejscu
4.5 step45_simplify [LLM] (opcjonalny) — upraszczanie; QG padło → revert uproszczenia
5. step5_commit:
     enforce_scope (revert poza scope_paths) → assert_diff_limit → commit
     → git sync_with_origin (fetch/rebase z origin — konflikt → READY)
     → push → PR na GitHubie (github_bridge) → status IN_REVIEW
6. Code Review (osobny proces, LLM): approve → READY_TO_MERGE | reject → CHANGES_REQUESTED
7. orchestrator._handle_merges(): rebase na _integration worktree → FULL REGRESSION pytest (900s)
     → merge --no-ff / --squash (subtask) → push origin master → status MERGED
     → close_topic Telegram + sprzątanie worktree + branch
```

Każde wywołanie git (mutujące) i każde wywołanie LLM/QG/TG ląduje w **rejestrze ops** (`ops`): `QUEUED → RUNNING → DONE/FAILED` — to widok „⚙️ Operacje" na dashboardzie.

## 5. Przepływ: „pytanie do LLM" (kolejność dokładna)

```
worker (proces)                        orchestrator (wątek dispatch_loop)          maszyna
────────────────                       ──────────────────────────────────          ───────
step2_implement(...)
  → llm_gateway.complete_json()
      → llm_queue.enqueue()    [llm_jobs: QUEUED; steel guard dedup (task,kind,step)]
      → llm_queue.wait_result(timeout=3600, poll 0.25s)  ... czeka ...
                                     claim_next() / claim_batch()  → RUNNING (lease)
                                     _rate_limit(min_interval_s) (fichier llm_last_start)
                                     _run_job()
                                       → llm_gateway.complete(_direct=True)
                                           → _llm_process_lock()  flock .agents/llm.lock
                                           → subprocess vibe-rs (model alias fast/strong)
                                           → stdout (raw do .agents/runtime/raw-*.txt)
                                       → extract_json() [z naprawą autoclose przy ucięciu]
                                     finish(): status=DONE, result zapisany
  ← wait_result widzi DONE → zwraca tekst
  → json dict → pliki zapisane w worktree
```

Ratownicze ścieżki kolejki: retry z backoffem `4s·2^n` (max 3 próby) → FAILED; reclaim wygasłych lease'ów; BATCH (≥2 `complete` tego samego modelu, razem <20k znaków = 1 wywołanie CLI); cache odpowiedzi (TTL 1h).

## 6. Track merge do mastera — dokładne komendy i kolejność

Dla zadania w READY_TO_MERGE (projekt `approval_policy=auto` → AUTO-MERGE do master):

```
git fetch origin --prune                        (repo główne; op kind=git RUNNING w ledgerze)
git branch -f _auto/<TASK> origin/master       (temp branch integracyjny)
git worktree prune
worktree remove _integration (jeśli był)
git worktree add --detach .agents/worktrees/<ns>/_integration _auto/<TASK>
rebase_onto(wt, feature/<TASK>)                (konflikt → zadanie wraca do READY, mikro-bot)
pytest -q --tb=short  w _integration  (FULL REGRESSION, 900s)   ← bramka przed merge
git merge --no-ff feature/<TASK>   (lub --squash dla subtasku; commit msg wg szablonu)
git push origin HEAD:refs/heads/master          (z detached worktree)
status = MERGED; snapshot pipeline.json; lessons_gardener co 10 mergy; skill_curator prune
git worktree remove _integration; git branch -D _auto/<TASK>
=> _cleanup_merged_branches: branch -D feature/<TASK> + push origin --delete
```

Milestone-end pipeline (faza 7): `qg3_integration_e2e` (pytest e2e + schemathesis/playwright jeśli są) → checkoout master w wt → `pull --ff-only origin master` → `merge_no_ff` → `push origin master`.

## 7. Jak uruchamiane są zadania (do optymalizacji)

Uruchamianie = **faza 4 ticka**: `_launch_workers()`:

1. `claimable_tasks()` — statusy READY (+ wygaszony COOLING), sort: priorytet P0→P3 → postęp (max_substep) → wiek.
2. Filtr aktywności projektu (opiekun może wstrzymać projekt).
3. `cap = _max_workers_now()`: **v2.7.28 pełny paralelizm** — `min(32, max(4, wolny_RAM/0.6))` + override `kv max_workers`. Każde zadanie chwytane natychmiast (RUNNING = „czeka na zasoby" w Operacjach); rzeczywistą wspólność LLM pilnuje kolejka.
4. Dla każdego taska: `subprocess.Popen([python3, -m, engine.worker_lifecycle, task_id], cwd=ROOT)` + log-append `.agents/logs/worker_lifecycle.<TASK>.log`; `self.running[task_id] = proc`.
5. Watchdogi wpływające na uruchamianie:
   - `_auto_retry_failed` (FAILED→READY, audyt FAILED→READY dzienny),
   - COOLING = zamrożenie ~65 min po wyjątku workera (auto-sleep zamiast eskalacji),
   - `_stale_queue_watchdog` (READY>8h → P1),
   - `_launch_recurring` (zadania cykliczne P3),
   - przerwa etapu `_stage_stop` („⏹️ zakończ po etapie" — pauza na granicy etapu bez utraty postępu).
6. Reap: proces skończony → `process_exit rc` w metrics; rc=0 = przebieg ok (task w IN_REVIEW/IN_PROGRESS), rc=2 = wyjątek → COOLING.

Punkty tarcia / kandydaci do optymalizacji (zdiagnozowane 23-24.11):

| Problem | Efekt | Gdzie |
|---|---|---|
| Atomy JSON od LLM niepasujące do parsera (`LLM nie zwrócił czystego JSON`) | zadanie wybucha w 2/5 lub 3/5 → FAILED → pętla z COOLINGiem | `llm_gateway.extract_json` + retry pętli worker_lifecycle |
| `vibe CLI rc=3` (race keyring) | retrye w kolejce, worker czeka | `_llm_process_lock` nie chroni przed proce­sam zewn. (np. visual_qa) |
| **Zagłodzenie retryów**: `dispatch_once` biletuje tylko batch z attempts=0 | job po retry wisiał w QUEUED do timeoutu 3600s workera | `llm_queue.dispatch_once` — **naprawione v2.7.29 (`claim_retry` przed batchem)** |
| Ucięte odpowiedzi (time limit) → brak zamknięcia JSON | cała odpowiedź do śmieci, pełny restart kroku | **naprawione v2.7.29: `_autoclose_json` fallback** |
| `wait_result` floor 3600s | przy zatykaniu kolejki worker trzyma slot ~1h zamiast failować szybciej | `llm_gateway.complete` (celowe „backlog-safe" z 31a3451) |
| Registry errors typu `16 values for 14 columns` | pojedyncze crashe zadań (kod generowany przez zadania z własnymi INSERT-ami) | do wyłapania przez QG; nie błąd silnika |

## 8. Eksperyment nr 1 — wniosek weryfikacji (23.11 12:14–12:17)

Co się stało: o 12:14–12:17:41 wykonano serię bezpośrednich mergy do mastera (`chat-0004/0006/0008/0012`, „sponsor: direct merge, no PR"), a commit `f7102da` (12:32) scalił projekty mikowhy-teams + mikowhy-teams-self w jeden i ustawił `approval_policy=auto` (AUTO-MERGE do master). Sam content merge'ów był poprawny (`git branch --contains` ok; testy na masterze przechodzą 98/98).

Co się zepsuło PÓŹNIEJ (nie tamta zmiana!): eksplozja feature'ów 15:00–01:15 (kolejka LLM v2-v4, steel guard, COOLING, pełny paralelizm) wprowadziła:
1. zagłodzenie retryów w `dispatch_once` (batch tylko attempts=0) → timeouty 3600s workera — **naprawione**,
2. brak odporności parsera JSON na ucięte odpowiedzi — **naprawione**,
3. rc=3 keyring przy dużej równoległości (procesy zewnętrzne nie traderują flocka) — do dalszego nadzoru (workload guardian).

Stan na dziś Ocena: pętla RUNNING→FAILED→COOLING→READY→RUNNING przy zadaniach kręcących się w QG — to one nie dochodziły do IN_REVIEW/merge; nie dlatego, że merge jest zepsuty (merge działał do 22:33 23.11), tylko dlatego, że QG/namiar JSON wybuchał przed commitem.

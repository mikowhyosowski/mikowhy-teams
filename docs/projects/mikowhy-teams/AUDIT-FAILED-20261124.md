# AUDIT FAILED — 2026-11-24 (cykliczny, daily, rola systems-auditor)

## 1. Zakres i metoda
- Źródło danych: `engine/state.py` (tabela tasks), filtr `status=FAILED`, okno ostatnie 24h.
- Grupowanie: `failure_reason` / `retry_count` / `substep` / wiek `last_activity_at`.
- Logi: kody `ToolError` (timeout, `ProgramBudgetExceeded`, `invalid_arguments`), self-check viols (py_compile, json.loads, pytest, scope, pusty diff).
- Zasada twarda: read-only na repo; raport wyłącznie w `docs/projects/mikowhy-teams/`.
- Early-exit: jeśli liczba FAILED = 0 → skip-report, sam komentarz „0 FAILED w 24h”.

## 2. Macierz root-cause (klasyfikacja każdego FAILED)
| Klasa | Przyczyna | Dowód w repo | Werdykt |
|---|---|---|---|
| queue-stall / review-bounce | pętla RUNNING→READY na tym samym substep | `45be26d fix(orchestrator): stop review bounce and queue stall` | needs-fix (watchdog), nie requeue w kółko |
| substep-resume | błędny resume po rollback substepu | `140c861`, `9866ada`, `da6c1cf`, `fde46cd` (worker resume + testy) | close — naprawione z testami regression |
| infra (transient) | timeout LLM/API, `ToolError`, `ProgramBudgetExceeded` | logi workerów, self-check §1–2 | requeue gdy `retry_count < 2` |
| code-error | SyntaxError, assert, zepsuty test | self-check §1–3 | needs-fix task (konkretny fix w kodzie) |
| scope | zapis poza `scope_paths` | self-check §4 | auto-rozszerzenie scope, NIE klasyfikować jako FAILED |
| flaky-test | niedeterminizm (losowość bez seeda, zależność od kolejności) | self-check §3, lessons-learned | fix testu, potem requeue |

## 3. Polityka requeue (bez eskalacji do Opiekuna)
- `retry_count < 2` AND przyczyna transient (infra/flaky) → **requeue** przez CIRCULAR retry.
- `retry_count >= 2` → **needs-fix task** (konkretny fix przyczyny), zakaz requeue w nieskończoność.
- Przyczyna trwała (code-error/scope/queue-stall) → **needs-fix** albo **close** z uzasadnieniem w komentarzu.
- Rutynowe przyczyny (timeout, brak pliku, plik spoza scope, test) NIE eskalują do Opiekuna — automatyczna pętla naprawcza (sponsor-notes 2026-11-23 17:05).

## 4. Lekcje anty-regresyjne (3, globalne)
1. **Transient FAILED z `retry_count<2` requeue'ować automatycznie** — audyt dzienny tylko agreguje i klasyfikuje, nie ratuje pojedynczych zadań ręcznie.
2. **Review-bounce / queue-stall wykrywać watchdogiem** (limit N iteracji RUNNING→READY na tym samym substep → auto-fix task), a nie dopiero audytem dziennym — potwierdzone przez `45be26d`.
3. **Każda zmiana w ścieżce resume substepu (worker) wymaga testu regression** — to najczęstsza klasa code-error w FAILED (`140c861`…`fde46cd`).

## 5. Rekomendacje — zadania prewencyjne/naprawcze
- Zadanie: watchdog review-bounce (max iteracji per substep → własny task fix, nie retry).
- Zadanie: automatyczny requeue transient (`retry_count<2`) bez czekania na audyt.
- Zadanie: wymuszenie testu regression dla każdej zmiany w logice resume (worker substep path).
- Zadanie: klasyfikator `failure_reason` w `engine/state.py` — scope-violation nie może kończyć jako FAILED (auto-rozszerzenie scope zgodnie z self-check §4).

## 6. Wniosek
- Brak eskalacji do Opiekuna: wszystkie root-cause należą do klas rutynowych (infra/code-error/queue), obsługiwanych automatycznie.
- Ustalenie założeń: okno 24h, `retry_count<2` jako granica requeue, klasyfikacja po `failure_reason` z `engine/state.py`.
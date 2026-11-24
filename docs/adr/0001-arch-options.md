# ADR-0001: Opcje architektoniczne rdzenia mikowhy-teams (Faza 1.5)

- **Status:** PROPOSED (przed zamrożeniem kontraktów)
- **Data:** 2026-11-23
- **Autor:** ideation-challenger (ARCH_OPTIONS)
- **Wejście:** docs/PRD.md + cele biznesowe Sponsora (kontekst repo: kolejka zadań P0–P3, jedna rola = jedno zadanie, deterministyczny silnik jako jedyny writer gita, kontrakty FROZEN, brama E2E przed master)

## Wymagania (REQ) brane pod uwagę

- REQ-1 Determinizm: boty LLM nigdy nie wywołują git — robi to wyłącznie engine/git_guard.py.
- REQ-2 Cykl zadania: 0/5_PREPARE → 1/5_PLAN → 2/5_IMPL → 3/5_TEST → 4/5_REVIEW → 5/5_QG, z resume z podkroku i retry z backoffem.
- REQ-3 Kolejka priorytetów P0–P3; orchestrator wymusza max 1 RUNNING na rolę.
- REQ-4 Webhooki płatnicze: weryfikacja podpisu + idempotencja (klucz idempotencji).
- REQ-5 Walidacja danych z zewnątrz na brzegu (Pydantic / JSON Schema).
- REQ-6 contracts/ FROZEN — tylko odczyt; zmiana wyłącznie przez eskalację do Architekta.
- REQ-7 Brama E2E (docker-compose.e2e.yml) przed merge milestone/* → master.
- REQ-8 Rutynowe błędy rozwiązuje automatyczna pętla naprawcza — bez eskalacji do Opiekuna.
- REQ-9 Obserwowalność: step_time per podkrok, ETA, załączniki zadań, wizualna historia.
- REQ-10 Wiele ról równolegle; izolacja przez git worktree (.agents/worktrees/<TASK-ID>/).

## Wariant A — Boring & Bulletproof: Postgres jako jedyne źródło prawdy

**Podejście:** jedna baza PostgreSQL = kolejka i stan. Claim zadania przez `SELECT ... FOR UPDATE SKIP LOCKED` + lease; tabela events jako append-only log zgodny z events.schema.json; advisory lock wymusza max 1 RUNNING na rolę; dispatch przez LISTEN/NOTIFY; workery bezstanowe; API (FastAPI, bearer JWT per openapi.yaml) tylko czyta stan.

**Mocne strony:**
1. Jedna ruchoma część — ACID daje atomowy claim i lease za darmo; SKIP LOCKED to natywna kolejka.
2. REQ-2 (resume z podkroku) i REQ-3 (max 1 na rolę) sprowadzają się do kolumn wiersza + advisory lock.
3. Backup = pg_dump, dev lokalny = postgres:17-alpine już w docker-compose.e2e.yml; najniższe obciążenie operacyjne (REQ-8).

**Słabe strony / ryzyka:**
1. Latency dispatchu rzędu ms–s (nie µs) — akceptowalne przy SLA rzędu sekund.
2. Orchestrator może stać się wąskim gardłem przy >20 równoległych rolach.
3. Pojedynczy punkt awarii DB — mitigacja: replika + backup PITR.

**Koszt złożoności:** ~4k LOC, 5 komponentów (db, orchestrator, worker, api, git_guard). **TTM:** MVP ~2–3 tyg. **Infra:** 1 DB + N replik app — najniższy koszt przy małej skali.

## Wariant B — Performance / Edge-First: event-driven na NATS JetStream + Redis

**Podejście:** Postgres zostaje źródłem prawdy, ale zmiany strumieniowane przez logical replication (pgoutput) do NATS JetStream; workery subskrybują strumienie (consumer groups = wymuszenie 1 RUNNING na rolę); edge API z cache Redis dla odczytów stanu; event sourcing stanu zadania.

**Mocne strony:**
1. Sub-sekundowy dispatch; workery skalują horyzontalnie bez limitu.
2. Replayowalny event log = darmowy audit i time-travel debug (REQ-9).
3. Cache odciąża DB przy odczytach stanu (dashboard, wizualna historia).

**Słabe strony / ryzyka:**
1. 3 dodatkowe komponenty infra (broker, cache, replicator) — więcej awarii i runbooków (ryzyko dla REQ-8).
2. Eventual consistency — wyścig przy claim może złamać atomowość REQ-3.
3. Wolniejszy TTM, trudniejszy dev lokalny; over-engineering przy ~10 rolach.

**Koszt złożoności:** ~12k LOC, 9 komponentów. **TTM:** ~6–8 tyg. **Infra:** DB + JetStream + Redis — wyższy koszt stały, wygrywa przy >50 rolach i twardym SLA.

## Wariant C — Wildcard / UX-Delight: git-native control plane

**Podejście:** kolejka zadań = repozytorium git (zadania jako issues/milestony + etykiety stanu), maszyna stanów w commitowanym state.json; workery już pracują w worktree'ach; PR = natywny code review Sponsora; zero własnej kolejki DB.

**Mocne strony:**
1. Zero infra kolejki — git jest kolejką, audytem i RBAC w jednym; DR = git clone.
2. Natywny UX Sponsora: PR/review/załączniki w UI git hosta + linki w Telegramie.
3. Najmniej kodu (~3k LOC, 3 komponenty: git-host API, engine, worker).

**Słabe strony / ryzyka:**
1. Latency webhook→claim rzędu sekund i bloat repo przy częstych zmianach stanu (potrzebna kompaktacja).
2. REQ-3 trudny do atomowego wymuszenia (wyścig na etykietach; zewnętrzny mutex podkopuje „zero infra”).
3. Rate limity API git hosta; słabe metryki step_time (REQ-9) bez własnego logu.

**Koszt złożoności:** ~3k LOC, 3 komponenty. **TTM:** MVP ~2 tyg., ale niski sufit skalowalności. **Infra:** sam git host.

## Kontrakt wyjściowy (JSON)

```json
{
  "options": [
    {
      "name": "Boring & Bulletproof",
      "approach": "Postgres jako jedyne źródło prawdy; kolejka przez SELECT ... FOR UPDATE SKIP LOCKED + lease; advisory lock = max 1 RUNNING na rolę; LISTEN/NOTIFY dla dispatchu; workery bezstanowe.",
      "strengths": ["Jedna ruchoma część — atomowy claim i lease z ACID", "REQ-2/REQ-3 to kolumny wiersza i jeden lock", "Trywialny backup i dev (pg_dump, postgres:17 w compose)"],
      "weaknesses": ["Latency dispatchu ms–s", "Orchestrator wąskim gardłem przy >20 rolach", "Pojedynczy punkt awarii DB"],
      "complexity": {"loc": 4000, "components": 5},
      "ttm_weeks": 3,
      "infra_cost": "najniższy przy małej skali: 1 DB + N replik app"
    },
    {
      "name": "Performance / Edge-First",
      "approach": "Event-driven: logical replication z Postgresa do NATS JetStream; consumer groups wymuszają 1 RUNNING na rolę; Redis cache odczytów; event sourcing stanu zadań.",
      "strengths": ["Sub-sekundowy dispatch i horyzontalna skala workerów", "Replayowalny event log = audit (REQ-9)", "Cache odciąża DB przy odczytach stanu"],
      "weaknesses": ["3 ekstra komponenty infra i runbooki", "Eventual consistency zagraża atomowości claim (REQ-3)", "Wolniejszy TTM, trudniejszy dev"],
      "complexity": {"loc": 12000, "components": 9},
      "ttm_weeks": 7,
      "infra_cost": "wyższy koszt stały: DB + JetStream + Redis; wygrywa przy >50 rolach"
    },
    {
      "name": "Wildcard / UX-Delight",
      "approach": "Git-native control plane: kolejka = repo tasks (issues + etykiety), stan w commitowanym state.json, worktree'y jako izolacja, PR jako natywny review Sponsora.",
      "strengths": ["Zero infra kolejki — git = kolejka + audit + RBAC", "Natywny UX Sponsora przez PR/review", "Najmniej kodu (~3k LOC)"],
      "weaknesses": ["Latency sekundy i bloat repo", "REQ-3 trudny atomowo bez zewnętrznego mutexu", "Rate limity API git; słabe metryki step_time"],
      "complexity": {"loc": 3000, "components": 3},
      "ttm_weeks": 2,
      "infra_cost": "sam git host, niski sufit skalowalności"
    }
  ],
  "recommendation": "Boring & Bulletproof — REQ-1/REQ-2/REQ-3 (determinizm, resume z podkroku, max 1 RUNNING na rolę) są trywialnie poprawne w Postgresie przez FOR UPDATE SKIP LOCKED + advisory lock, a obecna skala (jedno zadanie na rolę, P0–P3) nie uzasadnia brokera. Tabela events projektowana od razu pod FROZEN events.schema.json, więc migracja do Edge-First to dodanie replikacji, nie przepisanie. Wildcard odrzucamy: REQ-3 i REQ-9 są w git-native zawodne. Rewizja przy >20 współbieżnych rolach lub gdy latency dispatchu stanie się wąskim gardłem."
}
```

## Rekomendacja

**Boring & Bulletproof.** Kluczowe REQ-1, REQ-2, REQ-3 i REQ-8 (niska obsługa operacyjna, brak eskalacji rutynowych błędów) są najtańsze i najpewniejsze w wariancie A. Kontrakty FROZEN (openapi.yaml, events.schema.json, docker-compose.e2e.yml) pozostają bez zmian — wariant A tylko je produkuje/konsumuje.

**Założenia:** PRD nie wymaga sub-sekundowego SLA dispatchu; liczba ról < 20; akceptowalna pojedyncza instancja Postgresa z backupem PITR.
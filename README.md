# Dark Factory SDLC — autonomiczna fabryka oprogramowania

Rola człowieka (Product Sponsor / Board): wrzucasz cel biznesowy i odbierasz gotowe
wdrożenia na kamieniach milowych. Reszta dzieje się deterministycznie pod spodem.

```
Cel biznesowy → Faza 1 PRD → Faza 2 ADR + FROZEN kontrakty → Faza 2.5 Scaffolding
→ Faza 3 dekompozycja DAG → Faza 4 workery (worktree) → Faza 5 Code Review
→ Faza 6 merge do milestone/* → QG-3 E2E → main → 7a staging → 7b [Deploy Prod] Telegram
```

## Uruchomienie
```bash
python -m engine.orchestrator bootstrap "Dodaj moduł subskrypcji Stripe z webhookami" --milestone M1
python -m engine.orchestrator run --poll 10          # daemon schedulera DAG
python -m engine.orchestrator telegram-poll          # long-poll przycisków 7b
python -m engine.orchestrator export                 # snapshot pipeline.json
python -m engine.orchestrator doctor                 # pre-flight check
python -m engine.orchestrator run --once             # pojedynczy dry-run
```

## Jak używać jako Product Sponsor
```bash
python -m engine.orchestrator status                          # stan projektów i zadań (ikony statusów)
python -m engine.orchestrator approve NVG-010                 # AWAITING_APPROVAL -> READY (fabryka rusza)
python -m engine.orchestrator decline NVG-010 "zły kierunek"  # -> BLOCKED z uzasadnieniem
python -m engine.orchestrator note ALL "zawsze zachowaj visual_history"  # rada do KAŻDEGO promptu (sponsor-notes.md)
python -m engine.orchestrator note NVG-010 "nie ruszaj GRAVEYARD.md"      # rada tylko dla zadania (review_notes)
```
- **Telegram (priv 8762039205 lub forum -100…)**: każde zadanie dostaje kartę statusu (przypięta, edytowana na żywo);
  zadania `AWAITING_APPROVAL` dostają wiadomość z przyciskami **✅ Akceptuj → READY** / **🛑 Odrzuć → BLOCKED**
  (odbiera `telegram-poll`); deploy prod (Faza 7b) też przez przyciski **[🚀 Deploy Prod] / [🛑 Rollback]**
- **Gdzie patrzeć**: `status` | logi `.agents/logs/*.log` | artefakty `.agents/runtime/` | snapshot `.agents/projects/<projekt>/pipeline.json` | raporty `docs/projects/<projekt>/`
- **NVG-001..003** (audyt news-video-generator) są READY i ruszą pierwsze — target repo STRICT READ-ONLY;
  zadania migracyjne NVG-010+ wyjdą z NVG-003 jako AWAITING_APPROVAL i czekają na Ciebie (CLI lub Telegram)

## Provider LLM (Vibe CLI)
Domyślnie silnik używa lokalnego **Mistral Vibe CLI** w trybie headless (`engine/llm_gateway.py::VibeCliGateway`):
`vibe --trust --max-turns 1 --enabled-tools __none__ --system-prompt-file … --prompt-file …`
— bot nie ma żadnych narzędzi, odpowiada wyłącznie tekstem (JSON), a całą resztą zajmuje się silnik.

| Rola | Model | Zmienna środowiskowa |
|------|-------|---------------------|
| fast (testy, boilerplate, plan) | `zai-glm-5-3-flash` (fast SWE) | `VIBE_FAST_MODEL` |
| strong (architektura, CR, debugger) | `mistral-large-4-0` | `VIBE_STRONG_MODEL` |

Inny provider: `LLM_PROVIDER=stub python -m engine.orchestrator run` lub `register_gateway("nazwa", klasa)`.
Test bramki: `python -c "from engine.llm_gateway import get_gateway; print(get_gateway().complete_json(system='Odpowiadaj tylko JSON', user='{\"ok\": true}', step='SMOKE', model='fast-model'))"`

## Struktura
```
repo-root/
├── .agents/
│   ├── state.db                 # stan operacyjny SQLite WAL (gitignored)  [zmiana #8]
│   ├── pipeline.json            # snapshot DAG eksportowany po mergu/milestonie
│   ├── registry.json            # role + modele + status PROBATION/ACTIVE     [zmiana #5]
│   ├── metrics.jsonl            # telemetria
│   ├── skills/{_global,roles,stacks}/   # anti-slop, git-contract, lekcje per-stack [zmiana #2]
│   ├── worktrees/               # fizyczna izolacja workerów (gitignored)    [zmiana #1]
│   ├── runtime/ logs/           # gitignored
├── docs/PRD.md, docs/adr/       # Fazy 1–2
├── contracts/                   # FROZEN po QG-2: openapi.yaml, events.schema.json, docker-compose.e2e.yml
└── engine/                      # deterministyczny silnik (0 tokenów LLM)
    ├── state.py                 # SQLite WAL, atomowe przejęcia zadań         [zmiana #8]
    ├── orchestrator.py          # scheduler DAG, fazy 1–7b, circuit breaker
    ├── worker_lifecycle.py      # mikro-etapy 0–5                              [zmiana #7]
    ├── code_review.py           # czysty kontekst CR + lessons-learned
    ├── skill_compiler.py        # szablony ról + walidator + PROBATION         [zmiana #5]
    ├── git_guard.py             # worktree, scope_paths, MAX_DIFF_LINES=250    [zmiana #1, #7]
    ├── quality_gates.py         # QG-1 PRD / QG-2 kontrakty / QG-3 E2E milestone [zmiana #3]
    ├── scaffolding.py           # Faza 2.5                                     [zmiana #4]
    ├── lessons_gardener.py      # rotacja lekcji per-stack, limit 30           [zmiana #2]
    ├── telegram_gate.py         # 7b Human-in-the-Loop                         [zmiana #6]
    └── llm_gateway.py           # jedyny punkt styku z LLM (poza silnikiem)
```

## Wdrożone usprawnienia (8/8)
| # | Problem | Rozwiązanie | Gdzie |
|---|---------|-------------|-------|
| 1 | Race condition równoległych workerów w jednym katalogu | `git worktree` per zadanie w `.agents/worktrees/` | `engine/git_guard.py` |
| 2 | Context rot globalnego lessons-learned | Lekcje per-stack/per-rola + ogrodnik (dedup, limit 30, co 10 mergy) | `engine/lessons_gardener.py` |
| 3 | Brak semantycznej integracji; niedokończone funkcje na main | Gałąź `milestone/*` + QG-3 (Docker Compose + E2E z Gherkin PRD) przed main | `engine/quality_gates.py::qg3_integration_e2e`, `orchestrator.py` |
| 4 | scope_paths blokuje zależności/migracje/routery | Faza 2.5 Scaffolding (sekwencyjna, na gałęzi milestonu) | `engine/scaffolding.py` |
| 5 | Halucynujące role generowane w locie | Szablon roli + deterministyczny walidator + PROBATION→ACTIVE | `engine/skill_compiler.py` |
| 6 | Auto-deploy na prod bez człowieka | 7a staging auto → 7b przyciski Telegram [🚀 Deploy Prod]/[🛑 Rollback] | `engine/telegram_gate.py`, `orchestrator.py::_phase7` |
| 7 | SPLIT_TASK fork-bomba / przepełnione zadania | `max_split_depth=1` + `MAX_DIFF_LINES=250` odrzucany przed CR | `engine/state.py::split_task`, `engine/git_guard.py` |
| 8 | fcntl na płaskim JSON — deadlocki, uszkodzenia | SQLite WAL `.agents/state.db`, atomowe `UPDATE ... RETURNING`, snapshot `pipeline.json` do repo | `engine/state.py` |

## Warstwa skilli — 3 mechanizmy

### 1. Skill Harvester & Curated Registry (`engine/skill_compiler.py::ensure_stack_skills`)
- Skan zależności (`pyproject.toml` / `package.json` / `composer.json`) → Priorytet 1: pobranie gotowych reguł
  z zaufanych repo (`PatrickJS/awesome-cursorrules`, `anthropics/skills`) przez GitHub API → `.agents/skills/stacks/<dep>-harvested.md`
  z nagłówkiem proweniencji (źródło + sha256).
- **Kwarantanna**: każdy zewnętrzny skill ląduje najpierw w `.agents/skills/_staging/`, gdzie deterministyczny
  `security_scan_skill()` szuka prompt injection (ignore previous instructions, ukryte komentarze HTML, base64 bloby,
  socjotechnika). QUARANTINED nie trafia do produkcji — jest w `registry.json` z ustaleniami.
- Priorytet 2: brak gotowego skilla → synteza LLM (`synthesize_skill`, max 150 linii „Rób X / Nigdy nie rób Y”), status PROBATION.

### 2. Wymuszona kreatywność (osobno od rygoru)
- **ARCH_OPTIONS (Faza 1.5)**: zadanie `ideation-challenger` przed Fazą 2 generuje 3 skrajnie różne warianty
  (Boring & Bulletproof / Performance Edge-First / Wildcard UX-Delight) do `docs/adr/0001-arch-options.md`;
  dopiero `solutions-architect` wybiera wariant i zamraża kontrakty.
- **Cognitive diversity**: `registry.json` ma persony per rola — `creator` (product-owner, frontend-dev, ideation-challenger)
  vs `skeptic` (code-reviewer, security-auditor, principal-debugger) vs `builder`.
- **Simplification Pass** (`worker_lifecycle.py::step45_simplify`): po zielonym QG, dla diffów > 80 linii —
  „ta sama logika, ~30% mniej linii”; jeśli QG po uproszczeniu pada → automatyczny revert.

### 3. Skill Fitness Score (`engine/skill_curator.py` + `metrics.jsonl`)
- Każdy werdykt CR dopisuje do `.agents/metrics.jsonl`: które pliki skilli były wstrzyknięte + `first_pass` (czy CR za 1. razem).
- `skill_fitness_report()` = per-skill fitness (first_pass / uses).
- Auto-prune: gdy plik `*-lessons.md` przekroczy 50 reguł, curator promuje top-5 (po powtórzeniach)
  do odpowiedniego `stacks/*.md` / `roles/*.md` i przycina lekcje do limitu 30.

## Telegram — Forum Topics (1 zadanie = 1 temat)
`engine/telegram_reporter.py` (Bot API: `createForumTopic` / `editMessageText` / `pinChatMessage` / `editForumTopic` / `closeForumTopic`):

- **Temat = zadanie**: nazwa żyje ze statusem (`⏳ [TASK-002] … (2/5_IMPL)` → `🔍 …` → `✅ …`)
- **Przypięta Karta Stanu**: pierwsza wiadomość w wątku, aktualizowana tylko przez `editMessageText`
  (pasek `[■■■□□] 3/5`, rola/model, branch, lista podzadań `↳`) — zero spamu powiadomień
- **Eventy w wątku** tylko przy kamieniach milowych: SPLIT_TASK, CR REJECTED (z regułą), circuit breaker, ESCALATED, MERGED z shortstatem
- **close_topic**: `✅` + `closeForumTopic` po mergu (wątek archiwizowany, wciąż czytelny)
- Milestony (QG-3, 7a/7b) → wiadomość na główny temat grupy; bramka 7b ([🚀 Deploy Prod]) nadal w `telegram_gate.py`

Konfiguracja: supergrupa z włączonymi **Topics**, bot jako admin (Manage Topics / Send / Edit / Pin).
Sekrety w `.env` (gitignored): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (`-100…`). Bez nich reporter to no-op.
Stan per zadanie: kolumny `telegram_thread_id`, `telegram_summary_msg_id` w `state.db` (eksportowane do `pipeline.json`).

## Multi-project workspace
```
.agents/active_project.json                  # {"active": "<project_id>"}
.agents/projects/<project_id>/project.json   # {"target_repo_path": "/abs/path", "read_only": bool, ...}
.agents/projects/<project_id>/pipeline.json  # snapshot DAG per projekt
```
- `python -m engine.orchestrator project list|add|use` — zarządzanie projektami
- `python -m engine.orchestrator audit-project <id> <target_repo_path> "cel"` — rejestruje projekt jako **STRICT READ-ONLY** i tworzy DAG audytu (NVG-001→003, `repo='factory'` = raporty tylko w katalogu silnika)
- `engine/projects.py::repo_for_task(task)` — worktree zadania żyje w `target_repo_path` projektu (chyba że `task.repo='factory'`); `git_guard.assert_writable()` blokuje każdą mutację (worktree/commit/rebase/merge/push) w repo read-only
- Status `AWAITING_APPROVAL` — orchestrator uruchamia tylko `READY`; zadania migracyjne czekają na ręczną zmianę statusu przez Product Sponsora
- `state.db`: kolumny `project_id` + `repo` na `tasks`/`milestones`; snapshot per aktywny projekt

## Zasady bezpieczeństwa
- Boty LLM nie wywołują git ani nie ruszają stanu — robi to silnik (Python).
- `scope_paths` egzekwowane fizycznie przed każdym commitem; diff > 250 linii odrzucony.
- Code Review w osobnym procesie z czystym kontekstem; circuit breaker przy retry >= 3
  (principal-debugger), potem ESCALATED + alert na Telegram.
- Kontrakty FROZEN po QG-2; produkcja tylko po akceptacji człowieka (7b).

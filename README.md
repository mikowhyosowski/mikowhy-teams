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
```

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

## Zasady bezpieczeństwa
- Boty LLM nie wywołują git ani nie ruszają stanu — robi to silnik (Python).
- `scope_paths` egzekwowane fizycznie przed każdym commitem; diff > 250 linii odrzucony.
- Code Review w osobnym procesie z czystym kontekstem; circuit breaker przy retry >= 3
  (principal-debugger), potem ESCALATED + alert na Telegram.
- Kontrakty FROZEN po QG-2; produkcja tylko po akceptacji człowieka (7b).

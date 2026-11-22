# git-contract.md — zasady pracy z repozytorium
Boty LLM NIGDY nie wywołują git. Robi to wyłącznie deterministyczny silnik (engine/git_guard.py).

## Gałęzie
- `master` — tylko pełne, zintegrowane kamienie milowe (merge `milestone/*` po QG-3).
- `milestone/<ID>` — gałąź integracyjna kamienia milowego (zmiana #3).
- `feature/<task-id>` — gałąź zadania, tworzona w osobnym `git worktree` (zmiana #1).

## Worktree (zmiana #1)
- Każdy worker pracuje fizycznie w `.agents/worktrees/<TASK-ID>/` — własny index, współdzielona baza obiektów `.git`.
- Zakaz pracy w katalogu głównym repo przy równoległych grupach.

## Commit
- Atomowe commity: 1 commit = 1 logiczna zmiana.
- Konwencja: `feat(task-id): ...`, `fix(task-id): ...`, `chore(scaffold): ...`.
- Przed commitem silnik wymusza `scope_paths` (zmiany spoza = `git checkout --`) i limit diffu `MAX_DIFF_LINES = 250` (zmiana #7).

## Kontrakty i pliki współdzielone
- `contracts/` FROZEN po QG-2 — zmiana tylko przez eskalację do Solutions Architecta.
- `pyproject.toml` / `package.json` / lockfile / migracje / routery / DI — tylko Faza 2.5 (zmiana #4).

## Merge
- Zawsze rebase na aktualną bazę przed mergem; konflikt = mikro-bot + kontrakty.
- Merge tylko `--no-ff`, tylko do gałęzi milestonu; do `master` wyłącznie `milestone/*` po QG-3.

"""Dark Factory SDLC — deterministyczny silnik (0 tokenów LLM w samym silniku).

Moduły:
    state              — stan operacyjny w SQLite WAL (.agents/state.db)   [zmiana #8]
    git_guard          — izolacja worktree, scope_paths, rebase/merge      [zmiana #1, #7]
    worker_lifecycle   — mikro-etapy 0–5 w osobnym procesie                [zmiana #7]
    code_review        — niezależny CR w czystym kontekście + circuit breaker
    skill_compiler     — szablony ról, walidator, PROBATION               [zmiana #5]
    lessons_gardener   — rotacja/kompresja lessons-learned per stack       [zmiana #2]
    quality_gates      — QG-1 (PRD), QG-2 (kontrakty), QG-3 (E2E milestone)[zmiana #3]
    scaffolding        — Faza 2.5: zależności, migracje, szkielety        [zmiana #4]
    telegram_gate      — Faza 7b: Human-in-the-Loop [Deploy Prod]/[Rollback][zmiana #6]
    orchestrator       — daemon schedulera DAG, fazy 1–7b
    llm_gateway        — jedyny punkt styku z LLM (poza silnikiem)
"""

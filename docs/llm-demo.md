# Jak działa komenda `llm`

1. `llm` uruchamia agenta LLM w worktree zadania — agent dostaje opis zadania od Sponsora i samodzielnie wykonuje kolejne kroki (bash, read_file, write_file, list), aż uzna zadanie za skończone.
2. Każda odpowiedź agenta to dokładnie jeden obiekt JSON z akcją (`bash`, `write_file`, `read_file`, `list`, `ask`, `done`) — deterministyczny silnik wykonuje tę akcję w worktree i odsyła wynik do agenta.
3. Na końcu agent zapisuje raport drużyny w `docs/llm-reports/CHAT-0007.md` i kończy odpowiedzią `done`; wtedy silnik commituje zmiany na branchu `feature/chat-0007`.

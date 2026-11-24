# lessons-learned — rola code-reviewer

Lekcje specyficzne dla roli code-reviewer, wypracowane w cyklu przeglądu tygodniowego (REC-REC-CODE-WEE-20261123).

- Odrzut przez CR z powodu diffu 14688 linii (treść spoza scope zadania) — przed commitem zawsze sprawdzaj `git diff --stat` względem base i cofaj wszystko spoza scope_paths; limit 250 linii diffu to twardy limit, nie formalność.
- Bounce RUNNING→READY×3 w jednym cyklu — lekcja: jedno zadanie na raz i zamykanie go jak najszybciej; przy powrocie z READY kontynuuj od wypracowanego sub-etapu, nie od 1/5_PLAN.
- Kontrakty FROZEN (`contracts/`) — zero tolerancji: każda zmiana bez eskalacji do Solutions Architecta = REJECTED.
- Przy REJECTED zawsze podawaj uogólnioną regułę w `notes[].rule` — system uczy się na regułach, nie na pojedynczych przypadkach.
- Zakres review: tylko scope_paths zadania, semantyka kontraktów, anti-slop (zero TODO/mocków/sekretów w diffie) i testy vs acceptance_criteria.
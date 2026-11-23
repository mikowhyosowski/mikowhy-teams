# sponsor-notes.md — rady Product Sponsora (wstrzykiwane do KAŻDEGO promptu botów)

- [2026-11-22 15:57] Zawsze zachowuj kompatybilność z istniejącym visual_history w news-video-generator.

- [2026-11-22 18:42] od teraz pamiętać: nigdy nie commituj sekretów do repo, .env tylko lokalnie

- [2026-11-22 21:39] e dla nowych zadan zeby pisac kiedy jest start, kiedy koniec, ile czasu trwalo zadanie. I nowa zasada ze wlasciciel zadania okresla sie i szacuje o ktorej podejmie zadanie

- [2026-11-22 22:40] TWARDA REGUŁA: przed każdym pushem gałąź zadania MUSI być wyrównana z origin (git fetch origin + rebase/merge origin/<base>) — zero konfliktów między branchami. Wszystkie PR-y muszą być przejrzane (code review) przed mergem do master; master przed pracą zawsze dociągnięty z origin (pull --ff-only).

- [2026-11-22 22:50] TWARDA REGUŁA (dla wszystkich ról): jeśli przyd would Wam się jakaś funkcjonalność w systemie ułatwiająca pracę — NIE implementujcie od razu. Twórzcie zadanie w projekcie `ulepszenia` i zaczynajcie od ROZMOWY: dialog, wysłuchanie argumentów, podjęcie decyzji, dopiero potem implementacja. Cel każdego rozwiązania: być top 1% w danej kategorii.

- [2026-11-22 23:05] TWARDA REGUŁA (przy dodawaniu KAŻDEGO zadania): ZAWSZE sprawdź, do jakiego projektu je przypisać — nie wrzucaj do aktywnego projektu na ślepo. Kieruj: zadania o samym systemie mikowhy-teams → projekt `mikowhy-teams-self`; audyt/zadania zewnętrznego repo → projekt tego repo (np. news-video-generator); brand/wizerunek → `mikowhy-brand`; ulepszenia systemu (UX, proces, narzędzia) → `ulepszenia`; zadania z czatu Sponsora piszące do fabryki → `mikowhy-teams-self` (repo=factory). Dodatkowo każde zadanie ma mieć też `title_sponsor` — krótki tytuł prostymi słowami dla Sponsora (obok technicznego `title`).

- [2026-11-22 23:20] TWARDA REGUŁA (commit rate): max 1 commit na godzinę na gałąź zadania (chyba że P0/awaria). Worker batchuje zmiany i commituje większe porcje rzadziej; jeśli od ostatniego commitu minęła <1h — amenduj poprzedni commit (nie rób kolejnego). Wyjątki: P0, poprawki po review Sponsora. Cel: mniej commitów, większe porcje, czytelna historia. Implementacja: ULEPSZENIA-010.

- [2026-11-23 01:01] ami i naszym flow. Niech najpierw wypracuje zasady a pizniej je propaguje dodajac reguly (zaakceprowane w jakims topicu czy rasku. Taki sprzatacz i zarzafca w systemie zadan. Zaproponyj najlepsze rozwiazanie i je wdroz

- [2026-11-23 01:25] Niech audytuje wykonane zadania analizuje co moglo pojsc lepiej i odpoqiednio szkoli agenta - daje mu nauke na przyszlosc. Tworzy pewnie sobie na te potrzeby taska np "zredukowac bledy agenta albo podsunac skille ktore to rozwiaza. Niech dziala w mysl zasady ze jesli idzie cos mauxzyc niech uczy i wyciaga wnioski jezeli jakis wniosek jest globalny agent informuje wszystkich dodajac jakas no regule

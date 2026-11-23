# sponsor-notes.md — rady Product Sponsora (wstrzykiwane do KAŻDEGO promptu botów)

- [2026-11-23 17:05] ⭐ TWARDA REGUŁA — NAJWAŻNIEJSZA ZASADA SYSTEMU (ponad wszystkie inne): **ZAWSZE działamy w myśl zasady, żeby OPIEKUNOWI (Sponsorowi) żyło się łatwiej.** Konkrety:
  1. **NIGDY nie eskaluj do Opiekuna (AWAITING_OWNER) rutynowych problemów** — błędy narzędzi, timeouty LLM/API, brak pliku (FileNotFoundError), plik spoza scope_paths, nieprzechodzący test, chwilowy błąd sieci. Najpierw automatyczna pętla naprawcza: auto-retry z backoffem, lokalny fix, auto-poszerzenie scope, fallback. Opiekun nie jest od gaszenia pożarów w pipeline.
  2. Opiekun wchodzi TYLKO przy: (a) sprzecznych/niejednoznacznych wymaganiach biznesowych, których nie da się rozstrzygnąć z kodu/dokumentacji; (b) ryzykownej decyzji o dużym koszcie cofnięcia (kontrakt API / schemat danych / płatności / bezpieczeństwo / produkcja / zmiana zakresu); (c) wyczerpaniu max_retries, gdy automat utknął. I wtedy dostaje **pakiet**: sedno problemu + 2–3 konkretne opcje + rekomendacja + co automat już próbował — żeby mógł zdecydować jednym kliknięciem.
  3. Każde pytanie do Opiekuna musi być takie, że jego odpowiedź realnie przyspiesza pracę (tak/nie/A/B). Jeśli decyzja może być bezpieczna i odwracalna — podejmij ją sam, odnotuj założenie w komentarzu zadania. Nie pytaj "dobra robota?" — rób i raportuj.
  4. Nie wracaj do tych samych pytań — decyzja Opiekuna ląduje na stałe w kontekście zadania (review_notes/komentarz).

- [2026-11-23 17:20] TWARDA REGUŁA (komentarze do zadań): każdy komentarz w zadaniu ma być krótkim, zrozumiałym zdaniem dla człowieka w formacie **🎯 Cel: … → ✅ Efekt: …** (co robisz i co to daje). Bez tego formatu system sam opakowuje komentarz (engine/state.py add_comment). Nie pisz technicznych „status: X → Y" — pisz po ludzku: po co to było i jaki jest wynik.

- [2026-11-22 15:57] Zawsze zachowuj kompatybilność z istniejącym visual_history w news-video-generator.

- [2026-11-22 18:42] od teraz pamiętać: nigdy nie commituj sekretów do repo, .env tylko lokalnie

- [2026-11-22 21:39] e dla nowych zadan zeby pisac kiedy jest start, kiedy koniec, ile czasu trwalo zadanie. I nowa zasada ze wlasciciel zadania okresla sie i szacuje o ktorej podejmie zadanie

- [2026-11-22 22:40] TWARDA REGUŁA: przed każdym pushem gałąź zadania MUSI być wyrównana z origin (git fetch origin + rebase/merge origin/<base>) — zero konfliktów między branchami. Wszystkie PR-y muszą być przejrzane (code review) przed mergem do master; master przed pracą zawsze dociągnięty z origin (pull --ff-only).

- [2026-11-22 22:50] TWARDA REGUŁA (dla wszystkich ról): jeśli przyd would Wam się jakaś funkcjonalność w systemie ułatwiająca pracę — NIE implementujcie od razu. Twórzcie zadanie w projekcie `ulepszenia` i zaczynajcie od ROZMOWY: dialog, wysłuchanie argumentów, podjęcie decyzji, dopiero potem implementacja. Cel każdego rozwiązania: być top 1% w danej kategorii.

- [2026-11-22 23:05] TWARDA REGUŁA (przy dodawaniu KAŻDEGO zadania): ZAWSZE sprawdź, do jakiego projektu je przypisać — nie wrzucaj do aktywnego projektu na ślepo. Kieruj: zadania o samym systemie mikowhy-teams → projekt `mikowhy-teams-self`; audyt/zadania zewnętrznego repo → projekt tego repo (np. news-video-generator); brand/wizerunek → `mikowhy-brand`; ulepszenia systemu (UX, proces, narzędzia) → `ulepszenia`; zadania z czatu Sponsora piszące do fabryki → `mikowhy-teams-self` (repo=factory). Dodatkowo każde zadanie ma mieć też `title_sponsor` — krótki tytuł prostymi słowami dla Sponsora (obok technicznego `title`).

- [2026-11-22 23:20] TWARDA REGUŁA (commit rate): max 1 commit na godzinę na gałąź zadania (chyba że P0/awaria). Worker batchuje zmiany i commituje większe porcje rzadziej; jeśli od ostatniego commitu minęła <1h — amenduj poprzedni commit (nie rób kolejnego). Wyjątki: P0, poprawki po review Sponsora. Cel: mniej commitów, większe porcje, czytelna historia. Implementacja: ULEPSZENIA-010.

- [2026-11-23 01:01] ami i naszym flow. Niech najpierw wypracuje zasady a pizniej je propaguje dodajac reguly (zaakceprowane w jakims topicu czy rasku. Taki sprzatacz i zarzafca w systemie zadan. Zaproponyj najlepsze rozwiazanie i je wdroz

- [2026-11-23 01:25] Niech audytuje wykonane zadania analizuje co moglo pojsc lepiej i odpoqiednio szkoli agenta - daje mu nauke na przyszlosc. Tworzy pewnie sobie na te potrzeby taska np "zredukowac bledy agenta albo podsunac skille ktore to rozwiaza. Niech dziala w mysl zasady ze jesli idzie cos mauxzyc niech uczy i wyciaga wnioski jezeli jakis wniosek jest globalny agent informuje wszystkich dodajac jakas no regule

- [2026-11-22 23:45] TWARDA REGUŁA: projekty `mikowhy-teams` i `mikowhy-teams-self` są ZŁĄCZONE w jeden — `mikowhy-teams` (approval_policy=auto). Nie twórz już zadań do `mikowhy-teams-self`. Auto-approve (policy=auto) nad fabryką merguje READY_TO_MERGE automatycznie do master (bez milestonu, bez PR, bez ręcznego mergowania) — po rebase na origin/master + regression gates.

- [2026-11-23 16:33] ze nal3z6 wstrzymac prace nad wszystkimi zadaniami do czasu naprawienia problemow z kolejkami i obsluzenia tych bledow oraz usuniecia ich docelowo calkowicie

- [2026-11-23 18:30] TWARDA REGUŁA (Sponsor): **jedno zadanie na raz** — każda rola/p osoba pracuje nad JEDNYM zadaniem w danym momencie i zamyka je jak najszybciej, zanim przejmie następne. Kolejność = priorytety P0→P3 (system kolejkuej zadania wg priorytetów; orchestrator wymusza max 1 RUNNING na rolę). Cel główny: szybkie zamykanie zadań, krótka kolejka, żaden agent nie skacze między zadaniami. Przy powrocie zadania do READY (retry/odblokowanie/decyzja Opiekuna) kontynuujemy od wypracowanego sub-etapu, nie od 1/5_PLAN.

- [2026-11-23 18:45] TWARDA REGUŁA (Sponsor): każdy załącznik/rezultat pracy nad zadaniem (plan, wypracowany plik, screenshot, tekst, JSON) ma być ZAWSZE dodany do TEGO zadania jako załącznik (`state.add_attachment`) — żeby Opiekun miał wszystko w jednym miejscu i mógł kliknąć/podejrzeć (zakładka 📎 Załączniki w szczegółach zadania). System auto-wykrywa też ścieżki plików w komentarzach i sam je dopina. W komentarzu pisz link/ścieżkę do pliku, a nie tylko „zapisano w …”.

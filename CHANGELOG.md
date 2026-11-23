# CHANGELOG — mikowhy-teams dashboard

## [2.6.0] — 2026-11-23
- 🤖 **Kolejka LLM** (`engine/llm_queue.py`, tabela `llm_jobs`): wszystkie wywołania `complete` idą przez kolejkę
  (QUEUED→RUNNING→DONE/FAILED, **lease** z auto-requeue po crashu, **retry z backoff** 8s·2^n do 5 prób,
  **rate limit** 0.8s między startami). Dispatcher w ticku orchestratora wykonuje SERYJNIE — keyring/vibe CLI
  dotyka tylko jeden proces → **koniec rc=3 „Reading API keys from the system keyring”** (przyczyna ESCALATED CHAT-0021/0012b)
- 🔒 `llm_gateway`: cross-process file-lock `.agents/llm.lock` wokół każdego wywołania `vibe` (także agent-mode);
  queue-mode włączany kv `llm_queue` / env `LLM_QUEUE=0` wyłącza
- 📊 Zakładka Live: sekcja „🤖 Kolejka LLM” (API /api/llm/jobs — stats per status + ostatnie joby z błędami)
- 🐛 FIX worktree: `create_worktree` usuwa stary worktree trzymający ten sam branch („already checked out” → GitError ESCALATED)
- 🧹 QG odblokowane: martwe testy (create_app, sre_deployer, task_worker, staging) przeniesione do tests/legacy + conftest
- 💤 Miganie: karty decyzji/alertów pulsują tylko ~10s (pulseIco zamiast infinite) — „odczekaj chwilę i przestań migać”
- 📋 Zadanie SELF-011 (P1, tech-lead-planner): szczegółowy plan pełnej migracji procesów na kolejki z ponawianiem

## [2.5.2] — 2026-11-23
- 🐛 FIX widok Firma: biuro „czasami całkowicie znikało” po kliku — przyczyna: wyścig współbieżnych renderFirma
  (klik biuro = toggleProj wołał refresh() + renderFirma(), a refreshInner też wołał renderFirma przy aktywnej zakładce)
  — przestarzały render (starszy fetch) nadpisywał nowy. Naprawa: token sekwencji (ostatni render wygrywa),
  pozycje biur/pokojów (_ox/_y) ustawiane zawsze (przy cache'owanej warstwie były undefined → NaN),
  key warstwy statycznej bez load1 (stabilny cache), bez dubli renderu w toggleProj/toggleSt
- 🫥 gdy wszystkie projekty ukryte filtrem — czytelny komunikat na mapie zamiast pustej planszy

## [2.5.1] — 2026-11-23
- ⚡ Przycisk **„szybka odp: TAK”** w wątku rozmowy z agentem (obok „Wyślij ✈”) — jednym klikiem wysyła
  „Zgadzam się — realizuj. O nic już nie pytaj i nic nie potwierdzaj…” przez POST /api/thread/<id>/post
  (agent dostaje odpowiedź jak na Telegramie). Tekst konfigurowalny: `meta.quick_yes` w flow config
- ⚡ Przycisk **„TAK — realizuj, bez pytań”** w komentarzach zadania gdy substep=ASK_SPONSOR (POST /api/task/<id>/comment)

## [2.5.0] — 2026-11-23
- 🎯 Zakładka **Tematy** (widok opiekuna, badge 🚨N w nav gdy czekają decyzje): czytelny podgląd „o co chodzi” —
  każdy temat (milestone) z celem, gałęzią, paskiem postępu (% zmergowanych), chipsami per status i listą blokerów;
  klik temat = szczegóły (cel, AC, wszystkie zadania)
- 🚨 Sekcja „Wymaga Twojej decyzji” (AWAITING_APPROVAL/ESCALATED) z przyciskami akcji prosto z flow configa
  (✅ approve / 🛑 decline / 🔁 retry / 💬 / 🎚️) — POST /api/flow/action
- ⏳ Oczekujące zgody (approvals DEPLOY_PROD) + 📥 backlog READY (priorytety) + ➕ nowy temat (POST /api/milestone)
- API: GET /api/sponsor (milestones+progress+decisions+approvals+backlog+actions_def), POST /api/milestone

## [2.4.0] — 2026-11-23
- 🏷️ Globalny filtr STATUSÓW nad zakładkami (kolorowe chipsy jak projekty, localStorage) — **MERGED domyślnie
  odznaczony (ukryty) na starcie**; działa na wszystkich zakładkach (Przegląd/Zadania/Flow) i na mapie Firma
- 🌊 Zakładka Flow: filtr etapów (chipsy z kolorami z configa) — klik = ukryj/pokaż etap, dwuklik = SOLO
  (tylko ten etap); defaultowo ukryte etapy w pełni pokryte ukrytymi statusami (czyli „Zmergowane” przy MERGED off);
  strzałki do ukrytych etapów znikają

## [2.3.0] — 2026-11-23
- 🎨 Każdy pracownik ma własny kolor + ikonę „charakteru” (deterministycznie z roli, personas.color/icon) — na kartach
  zespołu, pokojach na mapie Firma i dymkach rozmów; zespół sortowany wg STAŻU (najstarsi pierwsi)
- 📊 Karta pracownika: liczba zadań PER STATUS (kolorowe chipsy) zamiast „zadania: X zmergowane: Y”
- 📋 Zakładka **Obowiązki** per pracownik (📋 na karcie): zadania cykliczne (recurring_tasks, domyślnie P3 — niżej niż
  bieżące sprawy), dodawanie/włączanie/wyłączanie; API /api/recurring
- 🏢 Firma: agregacja artefaktów — gdy w strefie więcej niż capacity, pokazuje zbiorczy kafelek `[+N]` (klik rozwija wszystkie)
- ✨ Firma animacje live: pracujący agent „miga” (blink avataru), aktywne wątki = dymki w kolorze rozmówcy + animowany
  łuk do rozmówcy (Sponsor = brama recepcji), strefa alarmu mocno miga gdy są FAILED/ESCALATED
- ⛶ Fullscreen widoku biura (przycisk ⛶ + requestFullscreen)
- 🌿 Firma redesign: płaska CIEPŁA paleta (krem/terakota, stage2 gradients/shadows off), roślinki tylko poza biurami
  (pasy góra/dół + wzdłuż dróg) — zadanie FIRMA-010 (P0) z rozbiciem na etapy
- 🔀 NAPRAWA licznika merge'ów: czyta też `git log --merges` (direct merge Sponsora „no PR” nie przechodziły przez
  metryki flow) — teraz widać wszystkie merge z git log
- ♻️ Ciągłość przepływu: watchdog kolejki (READY >8h → P1; BLOCKED/ESCALATED >48h → auto-recovery do READY)
  + więcej równoległych workerów (cap 8) i 2 lekkie sloty dla zadań read_only/llm

## [2.2.0] — 2026-11-23
- 🌊 Zakładka **Flow**: diagram przepływu pracy zespołu (SVG, 3 tory: decyzje Sponsora / pipeline / wyjątki,
  strzałki przejść z configu, kafelki zadań z akcjami, SLA/overdue, hot-pulsowanie) — klik kafelek = szczegóły,
  klik nagłówek etapu = opis + wszystkie zadania
- ⚙️ Zarządzanie flow z poziomu UI przez config: `engine/flow/default_flow.json` + override
  `.agents/flow/flow.config.json` (edytor JSON w dashboardzie, walidacja przeciw VALID_TRANSITIONS, zapis/reset)
- 🖱️ Akcje Sponsora prosto z diagramu (POST /api/flow/action): approve / decline / retry / comment / set_priority
- API: GET /api/flow, GET+POST /api/flow/config, POST /api/flow/config/reset

## [2.1.0] — 2026-11-22
- 💬 Przy każdej zmianie statusu agent dopisuje jedno zdanie „co się dzieje” (komentarz w historii zadania)
- 🛑 Projekt news-video-generator oznaczony NIEAKTYWNY (wstrzymany przez opiekuna): nie dostaje workerów ani auto-approve;
  zadania CHAT-* błędnie do niego przypisane przeniesione do mikowhy-teams; zadania tylko o repo NVG (NVG-*, FIX-NEWS-…)
  → BLOCKED z komentarzem „wstrzymane przez opiekuna”
- 🕘 Historia i komentarze w widoku zadania: najświeższe NA GÓRZE

## [2.0.0] — 2026-11-22
- 🏢 Widok **Firma**: kreskówkowa mapka osiedla (ogrodzenie, brama z szyldem, drogi z liniami,
  rośliny, biura z dachami/drzwiami/oknami) — bez animacji przejazdu (szybciej), rzadkie auto-refresh
  na mapie (co 10s + przycisk 🔄), interaktywna legenda (klik = podświetl status), klik: biurko=zadanie,
  pokój=wątek z osobą, biuro=filtr projektu, 🏆=milestone
- 💬 **Wątki rozmów z agentami** w zakładce Zespół (Telegram ↔ dashboard, kontynuacja wątku, agent odpowiada jako persona)
- 🏅 Badge stażu na kartach zespołu: „w firmie X · przepracowane Y"
- 📋 Szczegóły zadania w zakładkach (Przegląd / Historia / Komentarze) + kolory świeżości + scroll
- 🔀 Badge merge'ów w statystykach (ile w 24h, kiedy ostatni) + podgląd listy
- 🎨 Filtry projektów nad zakładkami (globalne), lżejsze renderowanie
- 🏷️ Badge wersji dashboardu w nagłówku + changelog (ten plik)
- 🌐 Dashboard na LAN: http://<lan_ip>:8765, auto-redirect z 127.0.0.1
- 🤖 Auto-merge: projekt mikowhy-teams (policy=auto) merguje READY_TO_MERGE sam do master

## [1.3.0] — 2026-11-22
- Zakładki dashboardu (Przegląd/Zadania/Zespół/Czat/Live), URL hash
- Wskaźniki zasobów: CPU/RAM per worker + load/RAM laptopa
- Pauza auto-refresh podczas pisania w polu tekstowym
- Filtry projektów (kolorowe chips, localStorage)

## [1.2.0] — 2026-11-22
- Adres IP hosta w LAN w nagłówku + /api/state.host
- Widok Firma etap 1 (SVG top-down, animacje przepływu biurek)

## [1.1.0] — 2026-11-22
- Auto-approve wg approval_policy projektu, auto-merge do master
- Projekty mikowhy-teams + mikowhy-teams-self złączone w jeden
- tytuł_sponsor dla zadań, priorytety P0–P3, persony z historiami

# CHANGELOG — mikowhy-teams dashboard

## [2.7.4] — 2026-11-23 — porządki OPS: zamknięcie OPS-LLM-ROOTCAUSE, utknięte zadania → AWAITING_OWNER (nie cały flow w kółko)
- 🧑‍💼 **Utknięte zadania nie przechodzą już całego etapu w kółko**: nowa faza `_escalate_stuck_tasks` w ticku —
  zadanie z `retry_count >= STUCK_RETRY (2)` w READY/RUNNING/FAILED → `AWAITING_OWNER` + `substep=OWNER_PACKET`
  + pakiet (co utknęło + opcje A/B/C); BLOCKED/MERGED/AWAITING_OWNER nie są ruszane. `READY→AWAITING_OWNER` dodane do VALID_TRANSITIONS
- 🚫 **Fix auto-approve**: `_auto_approve` NIGDY nie wypuszcza pakietu Opiekuna (`OWNER_PACKET`/`ASK_OWNER`/`PAKIET dla Opiekuna`)
  do READY — tylko człowiek odpowiada (wcześniej polityka `auto` projektu wypuszczała je z powrotem do kolejki)
- ✅ **Zamknięte `OPS-LLM-ROOTCAUSE`** → BLOCKED z komentarzem Sponsora (Mikołaj Osowski): problem LLM rozwiązany przez
  optymalizację kolejki v2/v3 (6a892af, 971bb52)
- 📦 **Migracja ręczna utkniętych zadań** do AWAITING_OWNER z komentarzem „Sponsor — Mikołaj Osowski”: BRAND-001 (P1, retry 3),
  FIRMA-010 (P0, retry 3), FIRMA-010c (retry 3), CHAT-0022a (retry 2)
- ⏸️ **Zatrzymane aktywne sesje agentów** (workery zabite; orchestrator zombie-recovery → READY, potem ruszyły od P0)
- ✅ Test `test_escalate_stuck_tasks_phase` — 58 passed

## [2.7.3] — 2026-11-23 — LLM batch (wiele pytań = 1 request) + cache odpowiedzi; kafelki flow z licznikiem i deltem ±5 min
- 📦 **Batchowanie requestów LLM (v3)**: gdy ≥2 joby tego samego modelu czekają (pierwsze próby),
  dispatcher wysyła je JEDNYM requestem (`_run_batch`: `{"items": [{step, system, user}...]}` →
  `{"answers": [...]}` w tej samej kolejności, wyniki rozdzielane per job). Mniej requestów do CLI,
  mniej overheadu procesu na pytanie. Batch tylko dla `attempts=0` (retry idą pojedynczo — bez pętli),
  rozmiar batcha z kv `llm_batch_size` (default 3), batch per model (fast/strong osobno), priorytet P0 pierwszy.
  Błąd parsowania/liczby odpowiedzi → fail każdego joba osobno (backoff, potem pojedynczo)
- ♻️ **Cache odpowiedzi LLM (v3)**: `enqueue` przy DONE jobie z tym samym `dedup_key` w ostatniej godzinie
  (`CACHE_TTL_S`) zwraca od razu wynik — zero nowych requestów na identyczne powtórki (retry tego samego kroku)
- 🧱 Kolumna `model` w llm_jobs (migracja v3) + indeks batch `(kind, model, status, attempts)`
- 📊 **Kafelki flow z licznikiem i deltem**: większe kółko z liczbą zadań w boksie (font 13 bold)
  + linia `+X −Y (5 min)` pod tytułem etapu — z historii metryk `task_event` (ostatnie 300s, `stage_deltas` w flow.py);
  tak samo w chipach filtrów etapów i w panelu szczegółów etapu. Zielone + / czerwone −, szare gdy 0
- ✅ Testy v3: batch claim (prio, tylko attempts=0), cache hit/TTL, `_run_batch` (1 request = 3 odpowiedzi,
  rozdział per job), batch mismatch → fail osobno — 57 passed

## [2.7.2] — 2026-11-23 — kolejka LLM v2: osobny wątek dispatchera, priorytety, dedup, per-model timeout, housekeeping
- 🧵 **Dispatcher kolejki LLM w osobnym wątku** (`dispatch_loop` startowany w `Orchestrator.run`) — tick orchestratora
  NIE blokuje się już na czas wywołania LLM (wcześniej cały tick czekał na subprocess `vibe`); w ticku zostaje
  tylko awaryjny dispatch co 30 cykli
- 🏷️ **Kolejka priorytetowa**: kolumna `priority` w llm_jobs + indeks claim `(status, delay_until, priority, created_at)`;
  `claim_next` bierze P0→P3, potem najstarsze. Priorytet zadania leci z workera przez `complete(priority=...)`
  (CR = P1, OPS-LLM-ROOTCAUSE P0 idzie pierwsze)
- 🚫 **Deduplikacja jobów** (`dedup_key` = hash payloadu): ten sam payload w QUEUED/RUNNING → jeden job (brak dubli przy retry)
- ⏱️ **Per-model timeout joba**: fast=120s, strong=300s (payload `timeout_s`, dispatcher ustawia `gw.timeout`);
  `wait_result` czeka `timeout*MAX_ATTEMPTS+240`. Szybciej failuje → backoff zamiast wiszenia 600s
- 🔒 **LEASE_S=480 > timeout subprocessu (360s)** — koniec re-claim/duplikatów RUNNING jobów (było 240s → podwójne wykonanie)
- 🔁 **MAX_ATTEMPTS=3, backoff 4s·2ⁿ** (było 5 prób/8s) — szybciej do FAILED, dalej auto-retry zadania; `wait_result` poll 0.25s
- 🧹 **Housekeeping**: `cleanup()` usuwa DONE/FAILED starsze niż 24h (co ~12s w dispatch_loop) — baza nie puchnie
- 🗄️ Migracja tabeli llm_jobs (priority, dedup_key, nowy indeks) przy starcie; testy v2 (dedup, prio claim, lease, cleanup) — 53 passed

## [2.7.1] — 2026-11-23 — zadania OPS: audyt LLM (P0), dzienny audyt FAILED→READY, nowa rola workload-guardian (People Ops)
- 🚨 **Zadanie P0 `OPS-LLM-ROOTCAUSE`** (rola `principal-debugger`, READY): zbadanie ostatnich problemów
  z odpowiedziami LLM (llm_queue timeouty 600s w QUEUED/RUNNING, rc=3 keyring, puste/nieJSON odpowiedzi)
  + wdrożenie rozwiązań prewencyjnych (limity, retry/backoff, fallback, walidacja, monitoring) + testy regresyjne
- 🔁 **Obowiązki cykliczne egzekwowane przez orchestratora** (nowa faza `_launch_recurring` w tick):
  `recurring_due()` → zadanie READY `REC-<id>-<yyyymmdd>` dla roli, idempotentnie (max 1 otwarte na obowiązek)
- 📋 **Dzienny audyt `AUDIT-FAILED-2-READY`** (daily, P2, rola `health-monitor`): przejścia FAILED/BLOCKED/ESCALATED→READY
  + kolejka LLM + retry — czy potrzebna pilna naprawa (P0)?
- 🧑‍⚖️ **Nowa rola `workload-guardian` (People Ops — „zatrudnienie”)**: wpis w `.agents/registry.json` + skill
  `.agents/skills/roles/workload-guardian.md` + węzeł w org chart (`default_org.json`, reports_to: sponsor).
  Główny obowiązek: monitoruje czas pracy agentów — aktywuje się gdy „Teraz pracują” (zegarki RUNNING na żywo);
  rola bez zadania > 30 min → tworzy dla niej zadanie wg jej odpowiedzialności w organizacji (org chart);
  pozostałe obowiązki sama wybiera (najlepsze dla organizacji). Zadanie P1 `OPS-GUARDIAN-SETUP` (diagnoza:
  kolejka READY pełna, a nikt nie pracuje — przyczyna: timeouty llm_queue) + recurring `GUARDIAN-IDLE-CHECK` (hourly, P2)
- ✅ Testy `tests/test_ops_tasks.py` (4): rola w registry+skill+org, zadania OPS w kolejce, recurring zarejestrowane,
  `_launch_recurring` idempotentny — 50 passed

## [2.7.0] — 2026-11-23 — docelowy model przepływu: samoregulująca się linia produkcyjna, Opiekun jako ostatnia instancja
- 🔄 **Nowy zestaw statusów** (`engine/state.py`): `READY → RUNNING → IN_REVIEW → READY_TO_MERGE → MERGED` + pętla
  `IN_REVIEW → CHANGES_REQUESTED → RUNNING` (poprawki po CR bez Opiekuna) + `AWAITING_OWNER` (tylko graniczne decyzje)
  + `BLOCKED` (odblokowanie WYŁĄCZNIE do READY — pełny cykl). `AWAITING_APPROVAL`/`ESCALATED` = legacy alias/migracja do AWAITING_OWNER
- 🤖 **Auto-retry zamiast eskalacji**: błędy przejściowe (FAILED: timeout/LLM/infra) lecą auto-retry z backoffem do READY;
  dopiero po wyczerpaniu max_retries (circuit breaker, też po stronie CR) → `AWAITING_OWNER` z **pakietem dla Opiekuna**
  (sedno problemu + opcje A/B/odrzuć + rekomendcja), nie rutynowy alert
- 🔍 **Niezależny CR przed merge**: REJECTED → `CHANGES_REQUESTED` (lista poprawek w review_notes, worker przejmuje jak READY
  przez `claimable_tasks()`), APPROVED → READY_TO_MERGE; kod nie przechodzi na słowo autora
- 🧑‍💼 **ASK_OWNER** (`worker_lifecycle._ask_sponsor`): agent przy niejednoznaczności/ryzyku pakuje pytanie + 2-3 opcje
  i czeka w AWAITING_OWNER; decyzja Opiekuna ląduje na stałe w kontekście zadania, po approve → READY (pełny cykl)
- 🌊 Zakładka Flow = 3 tory: góra **Opiekun (ostatnia instancja, hot)**, środek **linia produkcyjna**
  (Kolejka → Realizacja → Poprawki po review → Code review → Do merge'a → Zmergowane), dół **Blokady (BLOCKED/FAILED)**;
  `default_flow.json` v2 + przejścia wg modelu (16), akcje Opiekuna tylko z AWAITING_OWNER (approve→READY, decline→BLOCKED, retry→READY)
- 🗄️ Migracja przy starcie State: `AWAITING_APPROVAL`/`ESCALATED` → `AWAITING_OWNER`; fix race `ALTER TABLE ... duplicate column`
  przy równoczesnych State()
- ✅ Testy: `test_model_valid_transitions`, `test_legacy_status_migration`, `test_claim_changes_requested`,
  `test_flow_config_v2_model`, `test_apply_action_owner_lifecycle` — 46 passed

## [2.6.5] — 2026-11-23
- 🏗️ Zakładka 🌊 Flow ma teraz **dwa pod-widoki** (przełącznik, stan w localStorage + hash `fv=org`): ⚙️ **Flow pracy** (bez zmian)
  i 🏗️ **Flow stanowisk** — org chart hierarchii zespołu: kto jest czyim szefem (`reports_to`), każda rola = osoba (persona: imię,
  stanowisko, ikona, kolor) z żywymi danymi (bieżące zadanie RUNNING, liczniki per status, kropka zielona = pracuje teraz)
- 🌳 Hierarchia w configie `engine/flow/default_org.json` + override `.agents/flow/org.config.json` (deep-merge jak flow);
  root = 👑 Sponsor (human), dalej tech-lead-planner → solutions-architect → backend-dev/frontend-dev/sre-deployer → health-monitor,
  tech-lead-planner → code-reviewer/principal-debugger/systems-auditor, product-owner → ideation-challenger, security-auditor → sponsor
- ✅ Walidacja hierarchii (`engine/flow/org.py`): dokładnie jeden root, brak cykli, `reports_to` istnieje, role znane w rejestrze,
  węzły dosięgalne z root; zły override blokowany przy zapisie
- 🔌 API: GET `/api/org`, GET+POST `/api/org/config`, POST `/api/org/config/reset`; edytor JSON hierarchii w UI (jak flow);
  klik osoba = karta pracownika (szef, podwładni, bieżące zadanie, 💬 wątek, 📋 obowiązki); testy `tests/test_org.py` (7)

## [2.6.4] — 2026-11-23
- 🐛 FIX deadlock kolejki LLM: dispatch_once wołał gw.complete, która sama enqueue'owała do kolejki i czekała
  sama na siebie. Teraz dispatcher woła `gw.complete(..., _direct=True)` — wykonuje od razu, bez kolejki
- 🧹 Mass-retry utkniętych zadań przez Sponsora: wszystkie ESCALATED/BLOCKED/FAILED -> READY przez flow retry
  (0 utkniętych po akcji; READY 25, RUNNING rośnie); llm_queue=1; kolejka żyje (DONE rośnie, 0 błędów rc=3 keyring)

## [2.6.3] — 2026-11-23
- 🔄 Banner „Nowa wersja dashboardu”: karta porównuje `version|last_commit` z /api/state z zapamiętanym w localStorage
  (`dashVer`) — gdy serwer (proces dashboardu) ma nowszy kod, na górze pojawia się banner „odśwież Ctrl+R”
  z przyciskiem „🔄 Odśwież teraz” (ustawia dashVer + reload). Przy pierwszym load tylko zapisuje wersję.
  + sprawdzanie stanu przy powrocie do karty (visibilitychange)

## [2.6.2] — 2026-11-23
- 🎉 **Nowa osoba w firmie**: wykrywanie pierwszej świeżej roli (tenure < 10 min, kv seen_roles) → banner nad statystykami
  (awatar + kolor osoby + „Nowa osoba w firmie: …”) + toast (12s, pokazuje się raz) + event `new_hire` w Live feed;
  klik banner → zakładka Zespół
- 📋 Zadanie **HIRE-UX-001 (P0, FIRMA)**: zatrudnienie world-class UI/UX + frontend i redesign widoku Firma
  → „Enchanted Campus” (pełny opis wymagań w AC: design system, soft-isometric campus, stylizowane awatary
  z aura/breathing, GSAP/CSS motion, zachowanie wszystkich interakcji)

## [2.6.1] — 2026-11-23
- 🐛 FIX filtry znikały po odświeżeniu/zmianie adresu: były tylko w localStorage (per-origin) i showTab nadpisywał hash.
  Teraz filtry są w **3 miejscach**: URL hash (`#flow?hs=MERGED&hp=…`), localStorage i serwerze (kv `filters:*`,
  API GET+POST /api/filters — współdzielone między przeglądarkami/urządzeniami). Przy starcie: hash → localStorage → serwer → default
- 🗺️ Pan/zoom/scroll na mapie Firma NIE mają wpływu na filtry (tylko viewBox w pamięci) — zweryfikowane testem

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

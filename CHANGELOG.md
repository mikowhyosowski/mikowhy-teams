# CHANGELOG — mikowhy-teams dashboard

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

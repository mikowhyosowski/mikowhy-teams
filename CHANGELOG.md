# CHANGELOG — mikowhy-teams dashboard

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

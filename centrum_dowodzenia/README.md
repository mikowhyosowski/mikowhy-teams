# 🏭 Centrum Dowodzenia

Nowy dashboard fabryki — React Native (Expo + React Native Web + TypeScript).
Dziś działa jako webowy dashboard pod API silnika (`engine/dashboard.py`),
w przyszłości kompiluje się do natywnej apki iOS/Android bez przepisywania kodu.

## Ekrany
1. **Centrum Operacyjne** — żywe procesy `sponsor-shell` (polling 2s), liczniki, zacięcia (stall), 1-klik „⛔ Ubierz proces”.
2. **Agenci & Execution Pipeline** — karta agenta → pionowy pipeline zadań z runami `process_runs` i rozwijanym logiem `stdout/stderr`.
3. **Decyzje Sponsora** — karty `AWAITING_OWNER` z akcjami Zatwierdź / Odrzuć.

## Uruchomienie
```bash
npm install
npm run web          # dev web (Expo) — API przez EXPO_PUBLIC_API_URL lub CORS do :8765
npm run build:web    # produkcyjny build do dist/
npm run typecheck    # tsc --noEmit
```

API bazowe: `EXPO_PUBLIC_API_URL=http://127.0.0.1:8765 npm run web`
(domyślnie backend na `http://127.0.0.1:8765`, CORS włączony w `engine/dashboard.py`).

## Endpointy backendu
- `GET /api/pipeline/runs?task_id=&agent_role=&status=&limit=`
- `GET /api/pipeline/runs/<run_id>/log?tail=`
- `POST /api/pipeline/runs/<run_id>/kill`
- `GET /api/agents/<role>/pipeline`

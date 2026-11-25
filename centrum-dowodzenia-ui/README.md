# centrum-dowodzenia-ui

Frontend React Native/Expo dla systemu mikowhy-teams. Komunikuje się **wyłącznie** przez REST API (`:8765`) i WebSocket (`:8767`).

## Architektura

```
┌─────────────────┐     REST :8765      ┌──────────────────┐
│   Frontend      │ ◄─────────────────► │   API Gateway    │
│   (Expo Web)    │                     │   (FastAPI)      │
└────────┬────────┘                     └────────┬─────────┘
         │                                       │
         │           WebSocket :8767             │
         └───────────────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    ▼                   ▼
              ┌───────────┐       ┌───────────┐
              │  Engine   │       │  Redis    │
              │  Core     │       │  Pub/Sub  │
              └───────────┘       └───────────┘
```

## Wymagania

- Node.js 18+
- npm 9+
- Backend API dostępny pod `EXPO_PUBLIC_API_URL`
- WebSocket gateway pod `EXPO_PUBLIC_WS_PORT`

## Konfiguracja (Environment Variables)

| Zmienna | Opis | Domyślna |
|---------|------|----------|
| `EXPO_PUBLIC_API_URL` | Bazowy URL REST API (np. `http://localhost:8765`) | `""` (relatywne `/api`) |
| `EXPO_PUBLIC_WS_PORT` | Port WebSocket gateway | `"8767"` |

## Uruchomienie lokalne

```bash
# Zainstaluj zależności
npm install

# Uruchom w trybie deweloperskim (Expo)
npm start

# Uruchom wersję web
npm run web

# Type-check
npm run typecheck

# Lint
npm run lint

# Build produkcyjny (web export)
npm run build:web
```

## Docker

```bash
# Zbuduj i uruchom z docker-compose (wymaga backend API + WS gateway)
docker-compose up --build

# Lub sam frontend (z proxy do backend)
docker build -t centrum-dowodzenia-ui .
docker run -p 8081:8081 \
  -e EXPO_PUBLIC_API_URL=http://host.docker.internal:8765 \
  -e EXPO_PUBLIC_WS_PORT=8767 \
  centrum-dowodzenia-ui
```

## API Endpoints (oczekiwane od backendu)

| Endpoint | Metoda | Opis |
|----------|--------|------|
| `/api/pipeline/runs` | GET | Lista runów procesów (filtry: status, task_id, agent_role) |
| `/api/pipeline/runs/:id/log` | GET | Pełny log runu (opcjonalnie `?tail=N`) |
| `/api/pipeline/runs/:id/kill` | POST | Zabicie runu przez Sponsora |
| `/api/agents/:role/pipeline` | GET | Pipeline agenta |
| `/api/workers/health` | GET | Zdrowie workerów (stall detection) |
| `/api/sponsor` | GET | Widok Sponsora (decyzje AWAITING_OWNER) |
| `/api/task/:id/:action` | POST | Akcje na zadaniu (approve/decline/force_retry) |
| `/api/flow/action` | POST | Akcja z flow config |

## WebSocket Messages

| Type | Direction | Payload |
|------|-----------|---------|
| `pulse` | Server → Client | `{ runs: ProcessRun[], timestamp: string }` |
| `log` | Server → Client | `{ run_id: string, line: string }` |
| `task_status` | Server → Client | `{ task_id: string, status: string }` |
| `worker_health` | Server → Client | `{ workers: WorkerHealth[] }` |
| `sponsor_decision` | Server → Client | `{ task_id: string, decision: string }` |

## Struktura projektu

```
centrum-dowodzenia-ui/
├── src/
│   ├── api.ts           # REST API client
│   ├── ws.ts            # WebSocket client (singleton, auto-reconnect)
│   ├── types.ts         # TypeScript types shared with backend
│   ├── screens/         # Ekrany (CommandCenter, AgentsPipeline, Approvals, World)
│   ├── components/      # Komponenty UI (StatusPill, RunRow, LogViewer, etc.)
│   ├── world/           # 3D worlds (Three.js + React Three Fiber)
│   └── theme.ts         # Motyw
├── App.tsx              # Entry point
├── package.json
├── tsconfig.json
├── babel.config.js
├── metro.config.js
├── nginx.conf           # Nginx config for production
├── Dockerfile           # Multi-stage build
├── docker-compose.yml   # Full stack (frontend + api-gateway + ws-gateway + redis)
└── .github/workflows/   # CI/CD
```

## Deployment

### Production (Docker Compose)

```bash
# Na serwerze z backendem
docker-compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

### Kubernetes

```yaml
# Przykładowy deployment
apiVersion: apps/v1
kind: Deployment
metadata:
  name: centrum-dowodzenia-ui
spec:
  replicas: 2
  selector:
    matchLabels:
      app: centrum-dowodzenia-ui
  template:
    metadata:
      labels:
        app: centrum-dowodzenia-ui
    spec:
      containers:
      - name: frontend
        image: your-registry/centrum-dowodzenia-ui:latest
        ports:
        - containerPort: 8081
        env:
        - name: EXPO_PUBLIC_API_URL
          value: "https://api.yourdomain.com"
        - name: EXPO_PUBLIC_WS_PORT
          value: "8767"
```

## Zależności od backendu

Frontend **nie importuje** żadnego kodu z `engine/` ani `state.py`. Wszystka komunikacja:
- REST: `fetch` do `EXPO_PUBLIC_API_URL/api/*`
- WebSocket: `ws://<api-host>:EXPO_PUBLIC_WS_PORT/`

Backend musi dostarczać:
1. **API Gateway** (FastAPI/Starlette) na porcie 8765
2. **WS Gateway** na porcie 8767 (pub/sub przez Redis)
3. **Redis** dla komunikacji API ↔ WS

## Licencja

MIT
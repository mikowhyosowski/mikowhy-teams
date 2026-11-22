# Health Report — 2026-11-22 16:35:13

OK: **False** | anomalie: 4

## mikowhy-teams (target=/home/miko/Projekty/M+/mikowhy-teams, read_only=False)
- checks: `{"target_exists": true, "target_dirty_files": 0, "tasks_by_status": {"AWAITING_APPROVAL": 1}, "awaiting_approval": 1}`
## mikowhy-teams-self (target=/home/miko/Projekty/M+/mikowhy-teams, read_only=False)
- checks: `{"target_exists": true, "target_dirty_files": 0, "tasks_by_status": {"READY": 1, "AWAITING_APPROVAL": 3}, "awaiting_approval": 3}`
## news-video-generator (target=/home/miko/Projekty/M+/news-video-generator, read_only=True)
- checks: `{"target_exists": true, "target_dirty_files": 2, "tasks_by_status": {"READY": 3, "AWAITING_APPROVAL": 1}, "awaiting_approval": 1}`

## Anomalie
- [mikowhy-teams] **awaiting_approval**: 1 zadań czeka na akceptację Sponsora
- [mikowhy-teams-self] **awaiting_approval**: 3 zadań czeka na akceptację Sponsora
- [news-video-generator] **target_dirty**: 2 niezacommitowanych zmian w target repo
- [news-video-generator] **awaiting_approval**: 1 zadań czeka na akceptację Sponsora

# MIGRATION.md — przejście na architekturę 3-warstwową (Workspace / Staging / Prod)

> Cel: rozdzielenie kodu obsługiwanego przez agentów (Workspace), weryfikacji (Staging, port 8766)
> i produkcji (Prod, port 8765) — wg specyfikacji architekta. Zero-downtime, z backupem SQLite WAL.

## Stan docelowy

```
~/Projekty/M+/mikowhy-teams          # WARSTWA 1 — Workspace (fabryka: orchestrator, workery, worktrees)
├── branch develop                   # branch integracyjny (jq merged automatycznie przez orchestratora)
├── branch master                    # branch wydawniczy — TYLKO release pipeline
└── .agents/state.db                 # stan operacyjny fabryki

/srv/mikowhy-teams/                  # WARSTWA 2+3 (wymaga sudo raz)
├── staging/                         # WARSTWA 2: kopia develop + KOPIA bazy + dashboard :8766
├── prod/
│   ├── releases/<ts>-<sha>/         # niemutowalne wydania (git archive z origin/master)
│   ├── current -> releases/<…>      # atomowy symlink
│   └── backups/state-*.db           # backupy SQLite .backup (przy każdym deployu/rollbacks)
├── shared/
│   ├── .env                         # wspólne sekrety (symlinkowane do wydań)
│   └── .agents/state.db, logs/      # WSPÓLNY stan produkcyjny (symlinkowane do wydań)
└── logs/                            # logi serwisów staging/prod
```

## Kroki migracji (Zero-Downtime)

### Krok 0 — co już jest zrobione (zaimplementowane w repo, commit v2.8.0)
- `develop` utworzony i wypchnięty na origin.
- `engine/orchestrator.py`: merge workerów → `develop` (konfiguracja `integration_branch`),
  po merge do develop: **deploy staging** + **RELEASE CANDIDATE** (notyfikacja + kv
  `release_candidates`), akceptacja `[🚀 Deploy Prod]` = `kv release_approved=<sha>`,
  `[🛑 Rollback]` = `kv release_rollback=1` → `deploy.sh promote/rollback`.
- `engine/git_guard.py`: `ensure_branch()`, `worker_push_guard()` (worker NIEpushuje master/develop).
- `engine/worker_lifecycle.py`: worktree od `develop` (W-1), patch-mode Search/Replace >150 linii (W-2),
  pusty diff + QG zielone + retry>0 → IN_REVIEW (probe do CR) — koniec pętli „pusty diff".
- `scripts/deploy.sh` (staging | prod | promote | rollback | status).

### Krok 1 — jednorazowo utwórz strukturę (wymaga sudo)
```bash
sudo mkdir -p /srv/mikowhy-teams/{staging,prod/releases,shared/.agents/logs,logs}
sudo chown -R $USER:$USER /srv/mikowhy-teams
```
Jeśli /srv niedostępny, ustaw własne ścieżki:
```bash
DEPLOY_ROOT=$HOME/mikowhy-teams-deploy
export DEPLOY_ROOT
```
i dostosuj `staging_path`/`prod_path` w `.agents/projects/mikowhy-teams/project.json`.

### Krok 2 — pierwszy deploy staging
```bash
scripts/deploy.sh staging        # eksport origin/develop -> staging + kopia bazy + regresja + smoke :8766
```

### Krok 3 — pierwszy deploy prod (przełączenie systemd na /srv)
```bash
scripts/deploy.sh prod           # release z origin/master + backup SQLite WAL + healthcheck + auto-rollback
```
Podmień jednostki systemd --user na wersje z `deploy/` (wskazują na `/srv/mikowhy-teams/prod/current`):
```bash
cp deploy/mikowhy-teams.service          ~/.config/systemd/user/
cp deploy/mikowhy-teams-dashboard.service ~/.config/systemd/user/   # port 8765
cp deploy/mikowhy-teams-staging.service   ~/.config/systemd/user/   # port 8766
systemctl --user daemon-reload
```

### Krok 4 — release pipeline (od teraz)
1. Worker kończy zadanie → merge do `develop` → automatycznie `deploy.sh staging`
   (kopia bazy + regresja + smoke `:8766/api/state`).
2. Sponsor akceptuje: **[🚀 Deploy Prod]** (kv `release_approved=<sha>`) → orchestrator woła
   `deploy.sh promote` → `develop -> master` (ff-only) + tag `vX.Y.Z` + deploy prod (backup WAL
   przed restartem + healthcheck + **auto-rollback** gdy healthcheck padnie).
3. Opcjonalnie **[🛑 Rollback]** (kv `release_rollback=1`) → `deploy.sh rollback`.

### Krok 5 — sanity
```bash
scripts/deploy.sh status
systemctl --user status mikowhy-teams mikowhy-teams-dashboard mikowhy-teams-staging
```

## Rollback awaryjny (ręcznie)
```bash
ln -sfn <poprzedni-release> /srv/mikowhy-teams/prod/current   # atomowo przez current.new + mv -T
systemctl --user restart mikowhy-teams mikowhy-teams-dashboard
```
Baza stanu wspólne (/srv/.../shared/.agents/state.db) — rollback NIE cofa bazy; backupy: `/srv/.../prod/backups/state-*.db`.

#!/usr/bin/env bash
# deploy.sh — 3-warstwowe wdrożenie mikowhy-teams (Workspace / Staging / Prod)
# Wzór Capistrano: niemutowalne releases + shared (env+state) + atomowy symlink `current`.
#
# Użycie:
#   deploy.sh staging            # deploy origin/develop -> staging + migracje + regresja + smoke /api/state
#   deploy.sh prod [sha]         # release z origin/<DEPLOY_BRANCH> (+backup SQLite WAL) + healthcheck + auto-rollback
#   deploy.sh promote [sha]      # RELEASE CANDIDATE: gate staging OK -> develop -> <DEPLOY_BRANCH> (ff-only) + tag vX.Y.Z
#   deploy.sh rollback [release] # atomowy powrót current -> poprzedni release + restart + healthcheck
#   deploy.sh status             # stan wydań i usług
#
# Bezpieczniki:
#   - set -euo pipefail; wszystkie mutacje przez funkcje z logiem
#   - backup SQLite (sqlite3 .backup, WAL-safe) PRZED każdym restartem prod
#   - healthcheck po deployu; failure => automatyczny rollback do poprzedniego release
set -euo pipefail

SRC_REPO="${SRC_REPO:-/home/miko/Projekty/M+/mikowhy-teams}"
DEPLOY_ROOT="${DEPLOY_ROOT:-$HOME/apps/mikowhy-teams/deploy}"
STAGING="$DEPLOY_ROOT/staging"
PROD="$DEPLOY_ROOT/prod"
SHARED="$DEPLOY_ROOT/shared"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-master}"   # branch wydawniczy (release)
INTEGRATION_BRANCH="${INTEGRATION_BRANCH:-develop}"
STAGING_PORT="${STAGING_PORT:-8766}"
PROD_PORT="${PROD_PORT:-8765}"
KEEP_RELEASES="${KEEP_RELEASES:-5}"

log()  { printf '[deploy %s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die()  { printf '[deploy %s] ❌ %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

wal_backup() {  # WAL-safe online backup (sqlite3 .backup — działa przy żywej bazie)
  local dest_dir="$1" ts; ts="$(date +%Y%m%d-%H%M%S)"
  mkdir -p "$dest_dir"
  local src="${2:-$SHARED/.agents/state.db}"
  [ -f "$src" ] || die "brak bazy źródłowej: $src"
  sqlite3 "$src" ".backup '$dest_dir/state-$ts.db'" || die "backup SQLite nie powiódł się"
  log "💾 backup WAL: $dest_dir/state-$ts.db"
}

svc_restart() {
  for svc in "$@"; do
    if systemctl --user cat "$svc" >/dev/null 2>&1; then
      log "🔁 systemctl --user restart $svc"
      systemctl --user restart "$svc"
    else
      log "⚠️  brak unitu $svc — pomijam restart (doinstaluj jednostkę z deploy/)"
    fi
  done
}

svc_active() {
  local svc="$1"
  [ "$(systemctl --user is-active "$svc" 2>/dev/null || true)" = "active" ]
}

healthcheck() {  # healthcheck <port> [tries]  (bez pipefail-pułapki: curl → plik → grep)
  local port="$1" tries="${2:-10}" i tmp
  tmp="$(mktemp)"
  for ((i=1; i<=tries; i++)); do
    if curl -fsS --max-time 5 "http://127.0.0.1:${port}/api/state" -o "$tmp" 2>/dev/null \
       && head -c 1 "$tmp" | grep -q '{'; then
      rm -f "$tmp"
      log "✅ healthcheck /api/state :${port} OK (próba $i)"
      return 0
    fi
    sleep 2
  done
  rm -f "$tmp"
  log "❌ healthcheck /api/state :${port} FAILED po ${tries} próbach"
  return 1
}

latest_release() { ls -1d "$PROD"/releases/* 2>/dev/null | sort | tail -1 || true; }
prev_release()   { ls -1d "$PROD"/releases/* 2>/dev/null | sort | tail -2 | head -1 || true; }
current_release(){ readlink "$PROD/current" 2>/dev/null || true; }

# --- staging ------------------------------------------------------------------
cmd_staging() {
  mkdir -p "$STAGING"
  log "🌿 staging: eksport origin/${INTEGRATION_BRANCH} -> $STAGING"
  git -C "$SRC_REPO" fetch origin "$INTEGRATION_BRANCH" --prune
  local tmp; tmp="$(mktemp -d)"
  git -C "$SRC_REPO" archive "origin/${INTEGRATION_BRANCH}" | tar -x -C "$tmp"
  # zachowaj lokalne artefakty stagingu (.agents z kopią bazy), podmień kod
  if [ -d "$STAGING/.agents" ]; then mv "$STAGING/.agents" "$tmp/.agents"; else mkdir -p "$tmp/.agents"; fi
  [ -f "$STAGING/.env" ] && mv "$STAGING/.env" "$tmp/.env" || cp "$SRC_REPO/.env" "$tmp/.env"
  rsync -a --delete --exclude '.agents' --exclude '.env' "$tmp/" "$STAGING/" || cp -a "$tmp/." "$STAGING/"
  rm -rf "$tmp"
  # migracje na KOPII bazy (nigdy na prod!): sqlite .backup z shared -> staging/.agents/state.db
  mkdir -p "$STAGING/.agents"
  for db_src in "$SHARED/.agents/state.db" "$SRC_REPO/.agents/state.db"; do
    if [ -f "$db_src" ]; then
      sqlite3 "$db_src" ".backup '$STAGING/.agents/state.db'" || die "kopia bazy do staging FAILED"
      break
    fi
  done
  [ -f "$STAGING/.agents/state.db" ] || log "⚠️  brak bazy do skopiowania — staging dostanie świeży empty state"
  svc_restart mikowhy-teams-staging
  if systemctl --user cat mikowhy-teams-staging >/dev/null 2>&1; then
    healthcheck "$STAGING_PORT" 10 || die "staging smoke /api/state FAILED"
  else
    log "⚠️  brak unitu mikowhy-teams-staging — pomijam smoke /api/state (regresja i tak leci)"
  fi
  # regresja: na kopii roboczej develop (ten sam commit co eksport) — testy zależą od
  # git log i stanu fabryki, których eksport archive nie ma; staging dostaje smoke /api/state
  log "🧪 staging regresja: pytest (maks 120s, kopia robocza develop)"
  # Pomijaj testy live które wymagają połączenia z dashboard (port 8765)
  ( cd "$SRC_REPO" && timeout 120 python3 -m pytest tests -q --tb=line -k "not (test_endpoints_live or test_endpoint_live)" ) || die "staging regresja FAILED"
  log "✅ staging OK -> kandydat do wydania gotowy ($(git -C "$SRC_REPO" rev-parse --short origin/${INTEGRATION_BRANCH}))"
}

# --- prod (release-based) -----------------------------------------------------
cmd_prod() {
  local sha="${1:-}"
  mkdir -p "$SHARED/.agents/logs" "$PROD/releases"
  [ -f "$SHARED/.env" ] || { log "seed shared/.env"; cp "$SRC_REPO/.env" "$SHARED/.env"; }
  if [ ! -f "$SHARED/.agents/state.db" ]; then
    log "seed shared/.agents/state.db z bieżącego ROOT (migracja pierwszego wdrożenia)"
    mkdir -p "$SHARED/.agents"
    sqlite3 "$SRC_REPO/.agents/state.db" ".backup '$SHARED/.agents/state.db'"
  fi
  wal_backup "$PROD/backups"
  git -C "$SRC_REPO" fetch origin
  local target="$sha"; [ -z "$target" ] && target="origin/${DEPLOY_BRANCH}"
  local short; short="$(git -C "$SRC_REPO" rev-parse --short "$target")"
  local ts rel; ts="$(date +%Y%m%d-%H%M%S)"; rel="$PROD/releases/${ts}-${short}"
  mkdir -p "$rel"
  git -C "$SRC_REPO" archive "$target" | tar -x -C "$rel"
  ln -sfn "$SHARED/.env"    "$rel/.env"
  mkdir -p "$SHARED/.agents/logs"
  ln -sfn "$SHARED/.agents" "$rel/.agents"
  # atomowy switch current
  ln -sfn "$rel" "$PROD/current.new" && mv -T "$PROD/current.new" "$PROD/current"
  log "📦 release: $rel -> current"
  svc_restart mikowhy-teams mikowhy-teams-dashboard
  # healthcheck + usługi muszą żyć (jeśli unit istnieje — przy braku unitu decyduje sam healthcheck)
  if healthcheck "$PROD_PORT" 10 \
     && { ! systemctl --user cat mikowhy-teams >/dev/null 2>&1 || svc_active mikowhy-teams; } \
     && { ! systemctl --user cat mikowhy-teams-dashboard >/dev/null 2>&1 || svc_active mikowhy-teams-dashboard; }; then
    log "✅ prod deploy OK ($rel)"
    # housekeeping: zostaw ostatnie KEEP_RELEASES wydań
    ls -1d "$PROD"/releases/* | sort | head -n -"$KEEP_RELEASES" | xargs -r rm -rf
  else
    log "❌ healthcheck prod FAILED -> AUTO-ROLLBACK"
    cmd_rollback || true
    die "prod deploy FAILED, wykonano auto-rollback"
  fi
}

# --- rollback -----------------------------------------------------------------
cmd_rollback() {
  local target="${1:-$(prev_release)}"
  [ -n "$target" ] || die "brak poprzedniego release'u do rollbacku"
  wal_backup "$PROD/backups"
  ln -sfn "$target" "$PROD/current.new" && mv -T "$PROD/current.new" "$PROD/current"
  log "⏪ rollback: current -> $(basename "$target")"
  svc_restart mikowhy-teams mikowhy-teams-dashboard
  healthcheck "$PROD_PORT" 10 || die "rollback: healthcheck FAILED (sprawdź reclaim bazy)"
  log "✅ rollback OK"
}

# ---RELEASE CANDIDATE: develop -> deploy_branch (ff-only) + tag ---
cmd_promote() {
  local sha="${1:-origin/${INTEGRATION_BRANCH}}"
  # gate: staging musi być zdrowy (gdy środowisko staging istnieje)
  if systemctl --user cat mikowhy-teams-staging >/dev/null 2>&1; then
    curl -fsS --max-time 5 "http://127.0.0.1:${STAGING_PORT}/api/state" >/dev/null || die "staging DOWN — najpierw deploy staging"
  fi
  git -C "$SRC_REPO" fetch origin
  git -C "$SRC_REPO" push origin "refs/heads/${INTEGRATION_BRANCH}:refs/heads/${DEPLOY_BRANCH}" \
    || die "fast-forward ${INTEGRATION_BRANCH} -> ${DEPLOY_BRANCH} niemożliwy (rozjazd historii) — ręczny merge"
  local ver="v$(date +%Y.%m.%d-%H%M)"
  git -C "$SRC_REPO" tag -f "$ver" "$sha" >/dev/null 2>&1 || true
  git -C "$SRC_REPO" push origin "refs/tags/${ver}" >/dev/null 2>&1 || true
  log "🚀 release candidate: ${DEPLOY_BRANCH} = ${INTEGRATION_BRANCH} ($(git -C "$SRC_REPO" rev-parse --short origin/${INTEGRATION_BRANCH})), tag $ver"
  cmd_prod "${sha}"
}

cmd_status() {
  log "current prod: $(current_release || echo '(brak)')"
  log "poprzedni:    $(prev_release || echo '(brak)')"
  for svc in mikowhy-teams mikowhy-teams-dashboard mikowhy-teams-staging; do
    log "$svc: $(systemctl --user is-active "$svc" 2>/dev/null || echo brak)"
  done
  healthcheck "$PROD_PORT" 2 || true
  healthcheck "$STAGING_PORT" 2 || true
}

case "${1:-status}" in
  staging)  cmd_staging ;;
  prod)     shift; cmd_prod "${1:-}" ;;
  promote)  shift; cmd_promote "${1:-}" ;;
  rollback) shift; cmd_rollback "${1:-}" ;;
  status)   cmd_status ;;
  *) die "użycie: deploy.sh {staging|prod [sha]|promote [sha]|rollback [release]|status}" ;;
esac

#!/usr/bin/env bash
# control.sh — System Control CLI dla mikowhy-teams
# 
# Zapewnia aliasy i komendy do zarządzania całym systemem:
#   - status: podgląd stanu usług i procesów
#   - info: szczegółowe informacje o systemie
#   - start: uruchomienie wszystkich usług
#   - stop: zatrzymanie wszystkich usług
#   - restart: restart systemu (stop + start)
#   - logs: podgląd logów
#
# Użycie:
#   ./scripts/control.sh [status|info|start|stop|restart|logs|help]
#
# Zmienne środowiskowe:
#   DEPLOY_ROOT:_main deploy directory (default: /srv/mikowhy-teams)
#   PROD_PORT: port produkcji (default: 8765)
#   STAGING_PORT: port staging (default: 8766)
#
set -euo pipefail
shopt -s nullglob  # Pozwala na ignorowanie braku plików w globbingu

# Kolory
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Konfiguracja
SRC_REPO="${SRC_REPO:-/home/miko/Projekty/M+/mikowhy-teams}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/srv/mikowhy-teams}"
PROD="$DEPLOY_ROOT/prod"
STAGING="$DEPLOY_ROOT/staging"
PROD_PORT="${PROD_PORT:-8765}"
STAGING_PORT="${STAGING_PORT:-8766}"

# Serwisy (z rozróżnieniem user vs system)
SERVICES_USER=(
  "mikowhy-teams"
  "mikowhy-teams-dashboard"
  "mikowhy-teams-staging"
  "mikowhy-llm-queue"
  "mikowhy-agents"
)

SERVICES_SYSTEM=(
  "nginx"
  "postgresql"
  "redis"
  "sqlite"
)

# Log functions
log() {
  printf "[${BLUE}control${NC} %s] %s\n" "$(date +%H:%M:%S)" "$*"
}

success() {
  printf "[${GREEN}control${NC} %s] ✅ %s\n" "$(date +%H:%M:%S)" "$*"
}

warning() {
  printf "[${YELLOW}control${NC} %s] ⚠️  %s\n" "$(date +%H:%M:%S)" "$*" >&2
}

error() {
  printf "[${RED}control${NC} %s] ❌ %s\n" "$(date +%H:%M:%S)" "$*" >&2
  exit 1
}

# --- Core Functions ---

# Sprawdź, czy systemctl działa w trybie user
has_user_service() {
  systemctl --user status >/dev/null 2>&1
}

# Sprawdź, czy systemctl działa w trybie system
has_system_service() {
  sudo systemctl status >/dev/null 2>&1
}

# Uruchom usługę (user mode)
start_user_service() {
  local service="$1"
  if has_user_service; then
    if systemctl --user is-active "$service" >/dev/null 2>&1; then
      log "$service już działa"
    else
      log "🚀 Uruchamianie $service (user mode)..."
      systemctl --user start "$service" || warning "Nie udało się uruchomić $service"
    fi
  else
    warning "systemctl --user niedostępny - pomijam $service"
  fi
}

# Zatrzymaj usługę (user mode)
stop_user_service() {
  local service="$1"
  if has_user_service; then
    if systemctl --user is-active "$service" >/dev/null 2>&1; then
      log "🛑 Zatrzymywanie $service (user mode)..."
      systemctl --user stop "$service" || warning "Nie udało się zatrzymać $service"
    else
      log "$service nie działa"
    fi
  else
    warning "systemctl --user niedostępny - pomijam $service"
  fi
}

# Sprawdź status usługi
service_status() {
  local service="$1"
  local status
  
  if systemctl --user is-active "$service" >/dev/null 2>&1; then
    status="${GREEN}active${NC}"
  elif systemctl --user is-enabled "$service" >/dev/null 2>&1; then
    status="${YELLOW}enabled (inactive)${NC}"
  else
    status="${RED}disabled${NC}"
  fi
  
  echo "$status"
}

# --- Komendy główne ---

cmd_status() {
  log " === ${BLUE}STATUS SYSTEMU${NC} ==="
  
  # Pokaż aktualne release
  if [ -d "$PROD" ]; then
    local current
    current=$(readlink "$PROD/current" 2>/dev/null || echo "(brak)")
    log "📦 Aktualny release produkcji: $current"
    
    if [ -d "$PROD/current" ]; then
      local last_modified
      last_modified=$(stat -c %y "$PROD/current" 2>/dev/null | cut -d' ' -f1-2 || echo "nieznany")
      log "📅 Data wdrożenia: $last_modified"
    fi
  fi
  
  log ""
  log " === ${BLUE}SERWISY UŻYTKOWNIKA${NC} ==="
  
  for svc in "${SERVICES_USER[@]}"; do
    if systemctl --user list-unit-files "$svc.service" >/dev/null 2>&1; then
      local status=$(service_status "$svc")
      log "  $svc: $status"
    else
      log "  $svc: ${RED}nie zainstalowany${NC}"
    fi
  done
  
  if has_system_service; then
    log ""
    log " === ${BLUE}SERWISY SYSTEMOWE${NC} ==="
    
    for svc in "${SERVICES_SYSTEM[@]}"; do
      if sudo systemctl list-unit-files "$svc.service" >/dev/null 2>&1; then
        local status
        if sudo systemctl is-active "$svc" >/dev/null 2>&1; then
          status="${GREEN}active${NC}"
        else
          status="${RED}inactive${NC}"
        fi
        log "  $svc: $status"
      else
        log "  $svc: ${RED}nie zainstalowany${NC}"
      fi
    done
  fi
  
  # Healthcheck
  log ""
  log " === ${BLUE}HEALTHCHECK${NC} ==="
  
  if curl -fsS --max-time 3 "http://127.0.0.1:${PROD_PORT}/api/state" >/dev/null 2>&1; then
    success "Produkcja (port ${PROD_PORT}): ✅ OK"
  else
    error "Produkcja (port ${PROD_PORT}): ❌ NIE DOSTĘPNA"
  fi
  
  if curl -fsS --max-time 3 "http://127.0.0.1:${STAGING_PORT}/api/state" >/dev/null 2>&1; then
    success "Staging (port ${STAGING_PORT}): ✅ OK"
  else
    log "Staging (port ${STAGING_PORT}): ${YELLOW}nie dostępny${NC} (może nie działa)"
  fi
  
  # Info o procesach
  log ""
  log " === ${BLUE}PROCESY WORKERÓW${NC} ==="
  
  local worker_count
  worker_count=$(pgrep -f "worker_lifecycle" 2>/dev/null | wc -l)
  log "  Liczba procesów worker_lifecycle: $worker_count"
  
  if [ "$worker_count" -gt 0 ]; then
    local pids
    pids=$(pgrep -f "worker_lifecycle" 2>/dev/null)
    log "  PID-y: $pids"
  fi
  
  local dashboard_count
  dashboard_count=$(pgrep -f "dashboard" 2>/dev/null | wc -l)
  log "  Liczba procesów dashboard: $dashboard_count"
}

cmd_info() {
  log " === ${BLUE}SZCZEGÓŁOWE INFORMACJE${NC} ==="
  
  # Informacje o systemie
  log ""
  log "System:"
  log "  Hostname: $(hostname)"
  log "  System: $(uname -srm)"
  log "  Uptime: $(uptime -p)"
  log "  CPU: $(nproc) rdzeni, Load: $(cat /proc/loadavg)"
  
  # Pamięć
  if command -v free >/dev/null 2>&1; then
    log ""
    log "Pamięć:"
    free -h | while read line; do
      log "  $line"
    done
  fi
  
  # Dysk
  log ""
  log "Dysk:"
  df -h / | awk 'NR==1 || /\/$/ {print "  " $0}'
  
  # Repozytorium
  log ""
  log "Repozytorium:"
  log "  Lokalizacja: $SRC_REPO"
  log "  Aktualna gałąź: $(git -C "$SRC_REPO" branch --show-current 2>/dev/null || echo "nieznana")"
  log "  Ostatni commit: $(git -C "$SRC_REPO" log --oneline -1 2>/dev/null || echo "nieznany")"
  
  # Wersja systemu
  if [ -f "$SRC_REPO/VERSION" ]; then
    log ""
    log "Wersja systemu:"
    cat "$SRC_REPO/VERSION" | while read line; do
      log "  $line"
    done
  fi
  
  # Porty
  log ""
  log "Konfiguracja portów:"
  log "  Produkcja: $PROD_PORT"
  log "  Staging: $STAGING_PORT"
  
  # Środowisko
  log ""
  log "Zmienne środowiskowe:"
  log "  DEPLOY_ROOT: $DEPLOY_ROOT"
  log "  SRC_REPO: $SRC_REPO"
  
  # Uruchomione procesy
  log ""
  log "Uruchomione procesy systemu:"
  ps aux | grep -E "mikowhy|python.*engine|dashboard" | grep -v grep | head -10 | while read line; do
    log "  $line"
  done
}

cmd_start() {
  log " === ${BLUE}URUCHAMIANIE SYSTEMU${NC} ==="
  
  # Sprawdź, czy jesteśmy w repozytorium
  if [ ! -d "$SRC_REPO" ]; then
    error "Repozytorium $SRC_REPO nie istnieje!"
  fi
  
  # Przejdź do repozytorium
  cd "$SRC_REPO"
  
  # Uruchom orkiestrator (jeśli nie działa)
  if ! pgrep -f "orchestrator" >/dev/null 2>&1; then
    log "🚀 Uruchamianie orkiestratora..."
    nohup python3 -m engine.orchestrator run --poll 10 > "$SRC_REPO/.agents/logs/orchestrator.log" 2>&1 &
    success "Orkiestrator uruchomiony (PID: $!)"
  else
    log "Orkiestrator już działa"
  fi
  
  # Uruchom dashboard (jeśli nie działa)
  if ! pgrep -f "dashboard" >/dev/null 2>&1; then
    log "🚀 Uruchamianie dashboard..."
    nohup python3 -m engine.dashboard --port "$PROD_PORT" --host 0.0.0.0 > "$SRC_REPO/.agents/logs/dashboard.log" 2>&1 &
    success "Dashboard uruchomiony (PID: $!) na porcie $PROD_PORT"
  else
    log "Dashboard już działa"
  fi
  
  # Uruchom serwisy systemd (user mode)
  log ""
  log "Uruchamianie serwisów systemd (user mode)..."
  for svc in "${SERVICES_USER[@]}"; do
    start_user_service "$svc"
  done
  
  # Czekaj na healthcheck
  log ""
  log "Czekanie na działanie serwisów..."
  sleep 3
  
  cmd_status
  success "System został uruchomiony!"
}

cmd_stop() {
  log " === ${BLUE}ZATRZYMYWANIE SYSTEMU${NC} ==="
  
  # Zatrzymaj serwisy systemd (user mode)
  log "Zatrzymywanie serwisów systemd (user mode)..."
  for svc in "${SERVICES_USER[@]}"; do
    stop_user_service "$svc"
  done
  
  # Zatrzymaj procesy Python
  log ""
  log "Zatrzymywanie procesów Python..."
  
  # Orkiestrator
  if pgrep -f "orchestrator" >/dev/null 2>&1; then
    local orch_pids
    orch_pids=$(pgrep -f "orchestrator")
    log "🛑 Zatrzymywanie orkiestratora (PID: $orch_pids)..."
    kill $orch_pids 2>/dev/null || warning "Nie udało się zatrzymać orkiestratora"
  fi
  
  # Dashboard
  if pgrep -f "dashboard" >/dev/null 2>&1; then
    local dash_pids
    dash_pids=$(pgrep -f "engine.dashboard")
    log "🛑 Zatrzymywanie dashboard (PID: $dash_pids)..."
    kill $dash_pids 2>/dev/null || warning "Nie udało się zatrzymać dashboard"
  fi
  
  # Wszystkie workery
  if pgrep -f "worker_lifecycle" >/dev/null 2>&1; then
    local worker_pids
    worker_pids=$(pgrep -f "worker_lifecycle")
    log "🛑 Zatrzymywanie workerów (PID: $worker_pids)..."
    kill $worker_pids 2>/dev/null || warning "Nie udało się zatrzymać workerów"
  fi
  
  # Zatrzymaj LLM queue
  if pgrep -f "llm_queue" >/dev/null 2>&1; then
    local llm_pids
    llm_pids=$(pgrep -f "llm_queue")
    log "🛑 Zatrzymywanie LLM queue (PID: $llm_pids)..."
    kill $llm_pids 2>/dev/null || warning "Nie udało się zatrzymać LLM queue"
  fi
  
  # Czekaj na zakończenie procesów
  log ""
  log "Czekanie na zakończenie procesów..."
  sleep 2
  
  # Force kill jak coś została
  if pgrep -f "orchestrator\|dashboard\|worker_lifecycle\|llm_queue" >/dev/null 2>&1; then
    warning "Niektóre procesy nie zareagowały na SIGTERM, używam SIGKILL..."
    pkill -9 -f "orchestrator\|dashboard\|worker_lifecycle\|llm_queue" 2>/dev/null || true
    sleep 1
  fi
  
  cmd_status
  success "System został zatrzymany!"
}

cmd_restart() {
  log " === ${BLUE}RESTART SYSTEMU${NC} ==="
  cmd_stop
  sleep 2
  cmd_start
}

cmd_logs() {
  local service="${1:-all}"
  local lines="${2:-50}"
  
  log " === ${BLUE}PODGLĄD LOGÓW${NC} ==="
  
  if [ "$service" = "all" ] || [ "$service" = "orchestrator" ]; then
    log ""
    log "--- Orkiestrator ---"
    tail -n "$lines" "$SRC_REPO/.agents/logs/orchestrator.log" 2>/dev/null || echo "Brak logów orchestrator"
  fi
  
  if [ "$service" = "all" ] || [ "$service" = "dashboard" ]; then
    log ""
    log "--- Dashboard ---"
    tail -n "$lines" "$SRC_REPO/.agents/logs/dashboard.log" 2>/dev/null || echo "Brak logów dashboard"
  fi
  
  if [ "$service" = "all" ] || [ "$service" = "workers" ]; then
    log ""
    log "--- Workery ---"
    tail -n "$lines" "$SRC_REPO/.agents/logs/worker*.log" 2>/dev/null || echo "Brak logów workerów"
  fi
  
  if [ "$service" = "all" ] || [ "$service" = "console" ]; then
    log ""
    log "--- Konsola (ostatnie 5 konsoli) ---"
    local console_dir="$SRC_REPO/.agents/logs/console"
    if [ -d "$console_dir" ]; then
      for console_file in "$console_dir"/*.jsonl; do
        [ -f "$console_file" ] && log "File: $(basename "$console_file")" && tail -n 3 "$console_file" && echo ""
      done
    fi
  fi
  
  # systemd logs
  if [ "$service" = "all" ]; then
    log ""
    log "--- Systemd (user) ---"
    for svc in "${SERVICES_USER[@]}"; do
      if systemctl --user status "$svc" >/dev/null 2>&1; then
        log "\n  [$svc]:"
        journalctl --user -u "$svc" -n 5 --no-pager 2>/dev/null || echo "    Brak logów systemd"
      fi
    done
  fi
}

cmd_help() {
  cat <<EOF
${BLUE}control.sh — System Control CLI dla mikowhy-teams${NC}

Użycie:
  ./scripts/control.sh [KOMENDA] [OPCJE]

Komendy:
  ${GREEN}status${NC}     - Pokaż stan usług, procesów i healthcheck
  ${GREEN}info${NC}      - Szczegółowe informacje o systemie
  ${GREEN}start${NC}     - Uruchom wszystkie usługi systemu
  ${GREEN}stop${NC}      - Zatrzymaj wszystkie usługi systemu
  ${GREEN}restart${NC}   - Restart systemu (stop + start)
  ${GREEN}logs${NC} [SERWIS] [LINIE] - Pokaż logi
                       SERWIS: orchestrator, dashboard, workers, console, all (domyślnie)
                       LINIE: liczba linii (domyślnie 50)
  ${GREEN}help${NC}      - Pokaż tą pomoc

Przykłady:
  ./scripts/control.sh status
  ./scripts/control.sh start
  ./scripts/control.sh stop
  ./scripts/control.sh restart
  ./scripts/control.sh logs orchestrator 100
  ./scripts/control.sh info

EOF
}

# --- Main ---

case "${1:-help}" in
  status)   cmd_status ;;
  info)     cmd_info ;;
  start)    cmd_start ;;
  stop)     cmd_stop ;;
  restart)  cmd_restart ;;
  logs)     shift; cmd_logs "$@" ;;
  help)     cmd_help ;;
  *)        error "Nieznana komenda: $1. Użyj: status, info, start, stop, restart, logs, help";; 
esac

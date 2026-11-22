#!/usr/bin/env bash
# Instalacja fabryki jako usług systemd (user): mikowhy-teams + mikowhy-teams-dashboard
set -euo pipefail
REPO=/home/miko/Projekty/M+/mikowhy-teams
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR" "$REPO/.agents/logs"
cp "$REPO/deploy/mikowhy-teams.service" "$REPO/deploy/mikowhy-teams-dashboard.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now mikowhy-teams.service mikowhy-teams-dashboard.service
loginctl enable-linger "$USER" 2>/dev/null || echo "(linger niedostępny — service działa tylko podczas sesji; na 24/7 użyj sudo systemctl)"
sleep 2
systemctl --user --no-pager status mikowhy-teams.service mikowhy-teams-dashboard.service | head -20
echo "Dashboard: http://127.0.0.1:8765"

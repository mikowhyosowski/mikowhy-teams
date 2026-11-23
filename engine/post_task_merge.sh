#!/usr/bin/env bash
# post_task_merge.sh — automatyczne domergowanie zakonczonego zadania do staging.
# Silnik (engine/) wywoluje ten skrypt po kazdym zadaniu, przed push:
#   fetch -> utworzenie staging z master (gdy brak) -> push staging
#   -> walidacja Conventional Commits -> merge --no-ff -> push staging.
# usage: post_task_merge.sh <feature/<task-id>>
set -euo pipefail

REMOTE="${REMOTE:-origin}"
STAGING="${STAGING_BRANCH:-staging}"
BASE="${BASE_BRANCH:-master}"
TASK_BRANCH="${1:?usage: post_task_merge.sh <feature/<task-id>>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

git fetch "$REMOTE" --prune

# (1) Gwarancja istnienia gałęzi staging na remote: utworz z master, gdy brak.
if ! git ls-remote --exit-code --heads "$REMOTE" "$STAGING" >/dev/null 2>&1; then
  git push "$REMOTE" "$REMOTE/$BASE:refs/heads/$STAGING"
  echo "[post_task_merge] utworzono staging na $REMOTE z $BASE"
fi
git fetch "$REMOTE" "$STAGING"
git checkout -B "$STAGING" "refs/remotes/$REMOTE/$STAGING"

# (2) Push staging (idempotentny) — staging istnieje i jest wypchniete do remote.
git push "$REMOTE" "$STAGING"

# (3) Walidacja Conventional Commits + merge --no-ff zadania do staging.
if git rev-parse --verify --quiet "$TASK_BRANCH" >/dev/null 2>&1; then
  while IFS= read -r sha; do
    bash "$SCRIPT_DIR/validate_commit_msg.sh" "$(git log -1 --format=%s "$sha")" "$sha"
  done < <(git rev-list "$STAGING..$TASK_BRANCH")
  task="${TASK_BRANCH#feature/}"
  msg="merge(${task}): domergowanie ${TASK_BRANCH} do ${STAGING}"
  if [ "${#msg}" -gt 72 ]; then
    msg="merge(${task}): merge ${TASK_BRANCH} -> ${STAGING}"
  fi
  git merge --no-ff "$TASK_BRANCH" -m "$msg"
  git push "$REMOTE" "$STAGING"
  echo "[post_task_merge] OK: ${TASK_BRANCH} -> ${STAGING} (${REMOTE})"
else
  echo "[post_task_merge] brak lokalnej gałęzi ${TASK_BRANCH}; staging wypchniete."
fi

#!/usr/bin/env bash
# Walidator formatu Conventional Commits (AGENTS.md + git-contract.md):
#   pojedyncza linia 'type(scope): opis', max 72 znaki.
# Wywolywany przez engine/git_guard.py (pre-commit) i engine/post_task_merge.sh.
# usage: validate_commit_msg.sh <subject> [commit-sha]
set -euo pipefail

subject="${1:?usage: validate_commit_msg.sh <subject> [commit-sha]}"
sha="${2:-unknown}"
line="$(printf '%s' "$subject" | head -n 1)"

if [ "${#line}" -gt 72 ]; then
  echo "commit-format ERROR [$sha]: subject ma ${#line} znakow (max 72): $line" >&2
  exit 1
fi

if ! printf '%s' "$line" | grep -Eq '^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert|merge)(\([a-z0-9][a-z0-9._/-]*\))?: .+$'; then
  echo "commit-format ERROR [$sha]: wymagany format 'type(scope): opis': $line" >&2
  exit 1
fi

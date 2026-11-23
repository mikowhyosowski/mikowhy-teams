import os
import re
import subprocess

import pytest

REPO = os.environ.get("MIKOWHY_TEAMS_REPO", ".")
REMOTE = os.environ.get("MIKOWHY_TEAMS_REMOTE", "origin")

CC_RE = re.compile(r"^(feat|fix|chore|docs|test|refactor|perf|ci|build|revert)(\([a-z0-9\-_/]+\))?: .{1,}$")


def _git(*args: str) -> str:
    # Instrumentacja git przez subprocess; brak mocków, brak network poza git remote.
    result = subprocess.run(
        ["git", "-C", REPO, *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
        )
    return result.stdout.strip()


def test_staging_branch_exists_locally() -> None:
    branches = _git("branch", "--list", "staging")
    assert "staging" in branches, "gałąź staging nie istnieje w repozytorium lokalnym"


def test_staging_branch_pushed_to_remote() -> None:
    _git("fetch", REMOTE, "--quiet")
    remote_ref = _git("rev-parse", "--verify", f"refs/remotes/{REMOTE}/staging")
    local_ref = _git("rev-parse", "--verify", "refs/heads/staging")
    assert remote_ref, f"brak refs/remotes/{REMOTE}/staging — gałąź niepushnięta"
    # Lokalna HEAD stagingu nie może wyprzedzać remote bez pusha.
    ahead = _git("rev-list", "--count", f"{REMOTE}/staging..staging")
    assert ahead == "0", f"staging wyprzedza {REMOTE}/staging o {ahead} commitów — brak pusha"
    assert local_ref == remote_ref


def test_auto_merge_to_staging_configured() -> None:
    # Instrumentalizacja: po każdym zadaniu silnik ma domergować do staging przed push.
    # Weryfikujemy istnienie sprzężenia w konfiguracji engine (bez mocków).
    candidates = [
        os.path.join(REPO, "engine", "git_guard.py"),
        os.path.join(REPO, "engine", "staging_merge.py"),
        os.path.join(REPO, "engine", "config"),
    ]
    found_reference = False
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
            if "staging" in content and ("merge" in content or "post_task" in content or "post-task" in content):
                found_reference = True
                break
        elif os.path.isdir(path):
            for root, _dirs, files in os.walk(path):
                for name in files:
                    with open(os.path.join(root, name), encoding="utf-8") as fh:
                        content = fh.read()
                    if "staging" in content and "merge" in content:
                        found_reference = True
                        break
    assert found_reference, "brak konfiguracji automatycznego domergowania do staging w engine/"


def test_staging_commits_follow_conventional_commits() -> None:
    subjects = _git("log", "staging", "-20", "--pretty=%s").splitlines()
    if not subjects:
        pytest.fail("gałąź staging nie ma żadnych commitów")
    for subject in subjects:
        assert len(subject) <= 72, f"nagłówek dłuższy niż 72 znaki: {subject!r}"
        assert CC_RE.match(subject), f"nagłówek nie jest Conventional Commit: {subject!r}"

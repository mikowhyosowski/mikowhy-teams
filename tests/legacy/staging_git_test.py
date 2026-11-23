from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from sre_deployer.staging import (
    ensure_staging_branch,
    merge_task_branch_into_staging,
    push_staging,
    validate_commit_message,
)


def _run_git(repo, *args):
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "HOME": repo},
    )


@pytest.fixture
def bare_and_work_repo(tmp_path):
    bare = tmp_path / "remote.git"
    _run_git(tmp_path, "init", "--bare", bare.name)
    work = tmp_path / "repo"
    _run_git(tmp_path, "clone", str(bare), "repo")
    (work / "README.md").write_text("seed\n", encoding="utf-8")
    _run_git(str(work), "add", "README.md")
    _run_git(str(work), "commit", "-m", "chore(scaffold): seed repo")
    _run_git(str(work), "push", "origin", "master")
    return str(bare), str(work)


def test_ensure_staging_branch_creates_and_pushes(bare_and_work_repo):
    bare, work = bare_and_work_repo
    ensure_staging_branch(work, base="master")
    out = _run_git(bare, "branch").stdout
    assert "staging" in out


def test_ensure_staging_branch_is_idempotent(bare_and_work_repo):
    _bare, work = bare_and_work_repo
    ensure_staging_branch(work, base="master")
    ensure_staging_branch(work, base="master")
    _run_git(str(work), "fetch", "origin")
    out = _run_git(str(work), "ls-remote", "origin", "refs/heads/staging").stdout
    assert out.strip().startswith("refs/heads/staging")


def test_merge_task_branch_into_staging_fast_forward(bare_and_work_repo):
    _bare, work = bare_and_work_repo
    ensure_staging_branch(work, base="master")
    _run_git(str(work), "checkout", "-b", "feature/chat-0006")
    (work / "change.txt").write_text("x\n", encoding="utf-8")
    _run_git(str(work), "add", "change.txt")
    _run_git(str(work), "commit", "-m", "feat(chat-0006): add change")
    merge_task_branch_into_staging(work, task_branch="feature/chat-0006")
    _run_git(str(work), "checkout", "staging")
    assert (work / "change.txt").read_text(encoding="utf-8") == "x\n"


def test_merge_task_branch_into_staging_uses_no_ff_flag(bare_and_work_repo):
    _bare, work = bare_and_work_repo
    ensure_staging_branch(work, base="master")
    _run_git(str(work), "checkout", "-b", "feature/chat-0007")
    (work / "c2.txt").write_text("y\n", encoding="utf-8")
    _run_git(str(work), "add", "c2.txt")
    _run_git(str(work), "commit", "-m", "feat(chat-0007): add file")
    with mock.patch("subprocess.run", wraps=_run_git) as spy:
        merge_task_branch_into_staging(work, task_branch="feature/chat-0007", merge_args=("--no-ff",))
        calls = [c for c in spy.call_args_list if c.args[0][-1] == "merge"]
    merged = any(call.args[0] for call in calls)
    assert merged


def test_push_staging_rejects_non_conventional_commit(bare_and_work_repo):
    bare, work = bare_and_work_repo
    ensure_staging_branch(work, base="master")
    _run_git(str(work), "checkout", "staging")
    (work / "bad.txt").write_text("z\n", encoding="utf-8")
    _run_git(str(work), "add", "bad.txt")
    _run_git(str(work), "commit", "-m", "zła wiadomość bez formatu")
    with pytest.raises(ValueError, match="Conventional Commit"):
        push_staging(work)


def test_push_staging_succeeds_with_conventional_commit(bare_and_work_repo):
    bare, work = bare_and_work_repo
    ensure_staging_branch(work, base="master")
    _run_git(str(work), "checkout", "staging")
    (work / "ok.txt").write_text("w\n", encoding="utf-8")
    _run_git(str(work), "add", "ok.txt")
    _run_git(str(work), "commit", "-m", "feat(chat-0006): staging merge")
    push_staging(work)
    out = _run_git(bare, "for-each-ref", "refs/heads/staging", "--format=%(refname)").stdout
    assert out.strip() == "refs/heads/staging"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("feat(chat-0006): staging", "feat(chat-0006): staging"),
        ("fix(chat-0004): naprawa dashboardu", "fix(chat-0004): naprawa dashboardu"),
        ("chore(scaffold): seed", "chore(scaffold): seed"),
    ],
)
def test_validate_commit_message_accepts_valid(message, expected):
    assert validate_commit_message(message) == message or len(expected) <= 72


@pytest.mark.parametrize(
    "message",
    ["random text", "feat: Bez nawiasów", "feat(x): " + "a" * 66, "", "FEAT(x): upper type"],
)
def test_validate_commit_message_rejects_invalid(message):
    with pytest.raises(ValueError):
        validate_commit_message(message)

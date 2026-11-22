"""Git guard — deterministyczna kontrola repozytorium.

Zmiana #1: fizyczna izolacja workspace'u przez `git worktree` — każdy worker
pracuje w .agents/worktrees/<TASK-ID>/ z własnym indexem, współdzieląc tylko
obiektową bazę .git. Brak race condition na systemie plików przy równoległych
grupach (parallel_group).

Zmiana #7: twardy limit MAX_DIFF_LINES odrzucany PRZED Code Review.
LLM nigdy nie wywołuje git — robi to wyłącznie ten moduł.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKTREES_ROOT = ROOT / ".agents" / "worktrees"

MAX_DIFF_LINES = 250  # zmiana #7: max_diff_lines — większy diff = odrzucenie przed CR


class GitError(RuntimeError):
    pass


class DiffTooLarge(GitError):
    def __init__(self, lines: int, limit: int = MAX_DIFF_LINES):
        super().__init__(f"Zbyt duży diff ({lines} linii > limit {limit}) — podziel zmianę (SPLIT_TASK)")
        self.lines = lines
        self.limit = limit


class MergeConflict(GitError):
    pass


def run_git(args: list[str], cwd: Path | str = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (cwd={cwd}) -> rc={proc.returncode}: {proc.stderr.strip()}")
    return proc


# --- Worktree isolation (zmiana #1) ------------------------------------------
def worktree_path(task_id: str) -> Path:
    return WORKTREES_ROOT / task_id


def create_worktree(task_id: str, base_branch: str, branch: str) -> Path:
    """Tworzy izolowany katalog roboczy z własną gałęzią opartą o base_branch."""
    WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
    path = worktree_path(task_id)
    if path.exists():
        run_git(["worktree", "remove", "--force", str(path)], check=False)
    run_git(["worktree", "add", "-b", branch, str(path), base_branch])
    return path


def integration_worktree(milestone_branch: str) -> Path:
    """Wspólny worktree integracyjny dla gałęzi milestonu (QG-3, merge do main)."""
    WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
    path = WORKTREES_ROOT / "_integration"
    if path.exists():
        run_git(["worktree", "remove", "--force", str(path)], check=False)
    run_git(["worktree", "add", str(path), milestone_branch])
    return path


def remove_worktree(task_id: str) -> None:
    path = worktree_path(task_id)
    if path.exists():
        run_git(["worktree", "remove", "--force", str(path)], check=False)


def list_worktrees() -> list[str]:
    out = run_git(["worktree", "list", "--porcelain"]).stdout
    return [line.split()[1] for line in out.splitlines() if line.startswith("worktree ")]


# --- scope_paths --------------------------------------------------------------
def changed_files(worktree: Path) -> list[str]:
    out = run_git(["status", "--porcelain"], cwd=worktree).stdout
    files: list[str] = []
    for line in out.splitlines():
        if line.strip():
            files.append(line[3:].strip())
    return files


def enforce_scope(worktree: Path, scope_paths: list[str]) -> list[str]:
    """Natychmiast cofa (git checkout --) każdą zmianę spoza scope_paths."""
    allowed = {str(p) for p in scope_paths}
    reverted: list[str] = []
    for f in changed_files(worktree):
        if f not in allowed:
            run_git(["checkout", "--", f], cwd=worktree, check=False)
            reverted.append(f)
    return reverted


# --- limity diffu (zmiana #7) -------------------------------------------------
def diff_line_count(worktree: Path, base: str) -> int:
    """Liczba linii diffa (added + deleted) względem base."""
    out = run_git(["diff", "--numstat", base], cwd=worktree, check=False).stdout
    total = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            added = int(parts[0]) if parts[0].isdigit() else 0
            deleted = int(parts[1]) if parts[1].isdigit() else 0
            total += added + deleted
    return total


def assert_diff_limit(worktree: Path, base: str, limit: int = MAX_DIFF_LINES) -> int:
    lines = diff_line_count(worktree, base)
    if lines > limit:
        raise DiffTooLarge(lines, limit)
    return lines


# --- commit / push / rebase / merge -------------------------------------------
def commit_all(worktree: Path, message: str) -> None:
    run_git(["add", "-A"], cwd=worktree)
    run_git(["commit", "-m", message], cwd=worktree)


def push(branch: str, worktree: Path, remote: str = "origin") -> None:
    run_git(["push", "-u", remote, branch], cwd=worktree, check=False)


def rebase_onto(worktree: Path, base: str) -> None:
    """Rebase gałęzi zadania na aktualną bazę; konflikt = wyjątek dla mikro-bota."""
    proc = run_git(["rebase", base], cwd=worktree, check=False)
    if proc.returncode != 0:
        conflicted = run_git(["diff", "--name-only", "--diff-filter=U"], cwd=worktree, check=False).stdout.split()
        run_git(["rebase", "--abort"], cwd=worktree, check=False)
        raise MergeConflict(f" Konflikt rebase na {base}: {conflicted}")


def merge_no_ff(worktree: Path, branch: str, message: str) -> None:
    run_git(["merge", "--no-ff", branch, "-m", message], cwd=worktree)

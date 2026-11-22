"""Git guard — deterministyczna kontrola repozytorium.

Zmiana #1: fizyczna izolacja workspace'u przez `git worktree` — każdy worker
pracuje w .agents/worktrees/<TASK-ID>/ z własnym indexem, współdzieląc tylko
obiektową bazę .git. Brak race condition na systemie plików przy równoległych
grupach (parallel_group).

Zmiana #7: twardy limit MAX_DIFF_LINES odrzucany PRZED Code Review.
LLM nigdy nie wywołuje git — robi to wyłącznie ten moduł.
"""
from __future__ import annotations

import shutil
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


def default_branch(repo: Path | str = ROOT) -> str:
    """Domyślna gałąź repo: master (jeśli istnieje) > origin/HEAD > bieżący HEAD.
    NIGDY nie bieżący HEAD jako pierwszy wybór — główny checkout mógł być na feature."""
    for args in (["show-ref", "--verify", "--quiet", "refs/heads/master"],
                 ["symbolic-ref", "refs/remotes/origin/HEAD"],
                 ["rev-parse", "--abbrev-ref", "HEAD"]):
        out = run_git(args, cwd=repo, check=False).stdout.strip()
        if args[0] == "show-ref" and out == "" :
            pass
        if args[0] == "show-ref":
            if run_git(args, cwd=repo, check=False).returncode == 0:
                return "master"
            continue
        if out:
            return out.replace("refs/remotes/origin/", "") if "origin/" in out else out
    return "master"


def run_git(args: list[str], cwd: Path | str = ROOT, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (cwd={cwd}) -> rc={proc.returncode}: {proc.stderr.strip()}")
    return proc


def assert_writable(repo: Path | str) -> None:
    """STRICT READ-ONLY dla target repo projektów (np. news-video-generator w fazie audytu)."""
    from engine import projects
    projects.assert_writable(repo)


# --- Worktree isolation (zmiana #1) ------------------------------------------
def worktree_path(task_id: str, ns: str = "") -> Path:
    """Worktree per zadanie; ns = project_id (multi-project), żeby projekty się nie mieszały."""
    return WORKTREES_ROOT / ns / task_id if ns else WORKTREES_ROOT / task_id


def create_worktree(task_id: str, base_branch: str, branch: str,
                    repo: Path | str = ROOT, ns: str = "") -> Path:
    """Tworzy izolowany katalog roboczy z własną gałęzią opartą o base_branch.
    repo = fizyczne repo projektu (target_repo_path) albo katalog silnika (repo='factory')."""
    assert_writable(repo)  # STRICT READ-ONLY guard
    WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
    path = worktree_path(task_id, ns)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "prune"], cwd=repo, check=False)  # usuń rejestracje zaginionych katalogów
    if path.exists():
        run_git(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    exists = run_git(["rev-parse", "--verify", "--quiet", branch], cwd=repo, check=False).returncode == 0
    if exists:
        run_git(["worktree", "add", str(path), branch], cwd=repo)
    else:
        run_git(["worktree", "add", "-b", branch, str(path), base_branch], cwd=repo)
    return path


def integration_worktree(milestone_branch: str, repo: Path | str = ROOT, ns: str = "_integration") -> Path:
    """Wspólny worktree integracyjny dla gałęzi milestonu (QG-3, merge do main)."""
    assert_writable(repo)
    WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
    path = WORKTREES_ROOT / ns
    run_git(["worktree", "prune"], cwd=repo, check=False)  # usuń rejestracje zaginionych katalogów
    if path.exists():
        run_git(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
    run_git(["worktree", "add", str(path), milestone_branch], cwd=repo)
    return path


def remove_worktree(task_id: str, ns: str = "", repo: Path | str = ROOT) -> None:
    path = worktree_path(task_id, ns)
    if path.exists():
        run_git(["worktree", "remove", "--force", str(path)], cwd=repo, check=False)


def list_worktrees() -> list[str]:
    out = run_git(["worktree", "list", "--porcelain"]).stdout
    return [line.split()[1] for line in out.splitlines() if line.startswith("worktree ")]


# --- scope_paths --------------------------------------------------------------
def changed_files(worktree: Path) -> list[tuple[str, str]]:
    """Lista (kod_statusu, plik) z git status --porcelain ('??' = nieśledzony)."""
    out = run_git(["status", "--porcelain"], cwd=worktree).stdout
    files: list[tuple[str, str]] = []
    for line in out.splitlines():
        if line.strip():
            files.append((line[:2].strip(), line[3:].strip()))
    return files


def enforce_scope(worktree: Path, scope_paths: list[str]) -> list[str]:
    """Natychmiast cofa każdą zmianę spoza scope_paths:
    śledzone -> git checkout --, nieśledzone -> usunięcie pliku."""
    if not scope_paths:  # puste scope = zadanie samo ustala zakres (np. CHAT-*)
        return []
    allowed = {str(p) for p in scope_paths}
    reverted: list[str] = []
    for code, f in changed_files(worktree):
        if f in allowed:
            continue
        if code == "??":
            p = worktree / f
            if p.is_dir():  # nieśledzony katalog (np. docs/projects/<pid>/) — usuń rekurencyjnie
                import shutil
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        else:
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
    assert_writable(worktree)
    run_git(["add", "-A"], cwd=worktree)
    staged = run_git(["status", "--porcelain"], cwd=worktree, check=False).stdout.strip()
    if not staged:
        raise RuntimeError("Brak zmian w scope_paths — LLM nie wyprodukował żadnego pliku ze scope_paths "
                           "(enforce_scope wszystko cofnął albo odpowiedź nie miała klucza files)")
    run_git(["commit", "-m", message], cwd=worktree)


def push(branch: str, worktree: Path, remote: str = "origin") -> None:
    assert_writable(worktree)
    run_git(["push", "-u", remote, branch], cwd=worktree, check=False)


def rebase_onto(worktree: Path, base: str) -> None:
    """Rebase gałęzi zadania na aktualną bazę; konflikt = wyjątek dla mikro-bota."""
    assert_writable(worktree)
    proc = run_git(["rebase", base], cwd=worktree, check=False)
    if proc.returncode != 0:
        conflicted = run_git(["diff", "--name-only", "--diff-filter=U"], cwd=worktree, check=False).stdout.split()
        run_git(["rebase", "--abort"], cwd=worktree, check=False)
        raise MergeConflict(f" Konflikt rebase na {base}: {conflicted}")


def merge_no_ff(worktree: Path, branch: str, message: str) -> None:
    assert_writable(worktree)
    run_git(["merge", "--no-ff", branch, "-m", message], cwd=worktree)

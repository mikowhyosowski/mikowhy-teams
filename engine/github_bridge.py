"""GitHub Bridge — publikacja fabryki na GitHubie (PR-y, review CR, komentarze Sponsora).

Konfiguracja (wszystko w .env, NIGDY w repo — `orchestrator scan` pilnuje wzorca ghp_):
  GITHUB_TOKEN=ghp_...        # PAT z zakresem `repo`; komentarze/review są autorstwa właściciela tokena
  GITHUB_REPO=mikowhyosowski/mikowhy-teams   # opcjonalnie; domyślnie z `git remote get-url origin`

Funkcje:
  enabled()                 — czy bridge jest skonfigurowany (token + repo)
  ensure_remote(url)        — dodaje remote origin, jeśli brak
  push(branch)              — git push origin <branch>
  pr_for_branch(head)       — otwarty PR dla gałęzi (idempotentnie)
  open_pr(head, base, title, body) -> nr PR (reuse istniejącego)
  post_comment(pr, text)    — komentarz do PR (issue comment)
  post_review(pr, event, body)  — oficjalny review PR: APPROVED | CHANGES_REQUESTED | COMMENT
  merge_pr(pr)              — merge przez API (tylko gdy mergeable)
  protect(branch)           — branch protection (best effort, wymaga admina)
  link_task_pr(task_id, pr) / pr_for_task(task_id)  — kv w state.db

Bez tokena wszystko jest no-op z logiem (fabryka działa lokalnie jak dotąd).
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
DEFAULT_REPO_URL = "https://github.com/mikowhyosowski/mikowhy-teams.git"


def _load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'").split(" #")[0].strip())


def enabled() -> bool:
    _load_env()
    return bool(os.environ.get("GITHUB_TOKEN")) and bool(repo())


def repo() -> str | None:
    """owner/repo — z GITHUB_REPO albo z URL remote origin."""
    _load_env()
    r = os.environ.get("GITHUB_REPO")
    if r:
        return r.strip()
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                             capture_output=True, text=True).stdout.strip()
    except Exception:
        return None
    if not url:
        return None
    if url.startswith("git@github.com:"):
        return url[len("git@github.com:"):].removesuffix(".git")
    if "github.com/" in url:
        return url.split("github.com/", 1)[1].removesuffix(".git")
    return None


def _headers() -> dict:
    _load_env()
    return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "mikowhy-teams-bridge"}


def api(method: str, path: str, payload: dict | None = None) -> dict | list | None:
    if not os.environ.get("GITHUB_TOKEN"):
        print(f"[github] (brak GITHUB_TOKEN) {method} {path}", flush=True)
        return None
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={**_headers(),
                                                                       "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        hint = ""
        if exc.code == 403:
            hint = (" — fine-grained PAT (github_pat_) bez uprawnień! Ustaw w tokenie: "
                    "Pull requests=write, Contents=write, Issues=write, Metadata=read "
                    "albo użyj klasycznego ghp_ z zakresem 'repo'")
        print(f"[github] {method} {path} -> HTTP {exc.code} {body}{hint}", flush=True)
        return None
    except Exception as exc:
        print(f"[github] {method} {path} -> {exc!r}", flush=True)
        return None


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)


def ensure_remote(url: str = DEFAULT_REPO_URL) -> bool:
    cur = _git("remote", "get-url", "origin").stdout.strip()
    if cur:
        return True
    _git("remote", "add", "origin", url)
    print(f"[github] dodano remote origin -> {url}", flush=True)
    return True


def push(branch: str, remote: str = "origin") -> bool:
    r = _git("push", "-u", remote, branch)
    ok = r.returncode == 0
    if not ok:
        print(f"[github] push {branch} rc={r.returncode}: {r.stderr.strip()[:200]}", flush=True)
    return ok


def push_master() -> bool:
    return push(_git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() or "master")


def pr_for_branch(head: str) -> dict | None:
    r = repo()
    if not r:
        return None
    prs = api("GET", f"/repos/{r}/pulls?head={r.split('/')[0]}:{head}&state=open")
    if isinstance(prs, list) and prs:
        return prs[0]
    return None


def open_pr(head: str, base: str, title: str, body: str = "") -> int | None:
    """Idempotentnie: zwraca nr istniejącego otwartego PR albo tworzy nowy."""
    r = repo()
    if not r or not enabled():
        return None
    existing = pr_for_branch(head)
    if existing:
        return existing.get("number")
    pr = api("POST", f"/repos/{r}/pulls",
             {"head": head, "base": base, "title": title[:200], "body": body[:4000], "draft": False})
    if isinstance(pr, dict) and pr.get("number"):
        print(f"[github] PR !{pr['number']} {head} -> {base}: {title[:80]}", flush=True)
        return pr["number"]
    return None


def post_comment(pr: int, text: str) -> bool:
    r = repo()
    if not r or not enabled():
        return False
    out = api("POST", f"/repos/{r}/issues/{pr}/comments", {"body": text[:65000]})
    return isinstance(out, dict) and bool(out.get("id"))


def post_review(pr: int, event: str, body: str) -> bool:
    """event: APPROVED | CHANGES_REQUESTED | COMMENT — oficjalny review PR."""
    r = repo()
    if not r or not enabled():
        return False
    out = api("POST", f"/repos/{r}/pulls/{pr}/reviews",
              {"event": event, "body": body[:65000]})
    return isinstance(out, dict) and bool(out.get("id"))


def merge_pr(pr: int, method: str = "merge") -> bool:
    r = repo()
    if not r or not enabled():
        return False
    out = api("PUT", f"/repos/{r}/pulls/{pr}/merge", {"merge_method": method})
    return isinstance(out, dict) and bool(out.get("merged"))


def protect(branch: str = "master") -> bool:
    r = repo()
    if not r or not enabled():
        return False
    api("PUT", f"/repos/{r}/branches/{branch}/protection", {
        "required_status_checks": {"strict": True, "contexts": ["secret-scan", "lint-tests"]},
        "enforce_admins": False,
        "required_pull_request_reviews": {"required_approving_review_count": 1},
        "restrictions": None,
    })
    return True


# --- linkowanie task <-> PR (kv w state.db) ------------------------------------------
def link_task_pr(task_id: str, pr: int) -> None:
    from engine.state import State
    State().set_kv(f"gh_pr:{task_id}", pr)


def pr_for_task(task_id: str) -> int | None:
    from engine.state import State
    return State().get_kv(f"gh_pr:{task_id}")


def ensure_pr_for_task(task: dict, base: str) -> int | None:
    """Pushuje branch taska i otwiera/reużywa PR. Zwraca nr PR albo None (gdy bridge off)."""
    if not enabled():
        return None
    branch = task.get("branch")
    if not branch:
        return None
    # base musi istnieć na remote — lokalne gałęzie milestonów mogą jeszcze nie być wypchnięte
    remote_has_base = _git("ls-remote", "origin", f"refs/heads/{base}").stdout.strip()
    if not remote_has_base:
        repo_info = api("GET", f"/repos/{repo()}") if enabled() else None
        base = (repo_info or {}).get("default_branch") or "master"
    push(branch)
    pr = pr_for_branch(branch) or open_pr(branch, base, f"[{task['id']}] {task['title']}",
                                          body=(f"Zadanie: {task['id']}\nRola: {task['role']}\n"
                                                f"AC: {task.get('acceptance_criteria', '')[:500]}"))
    if isinstance(pr, dict):  # pr_for_branch zwraca pełny obiekt PR — normalizujemy do numeru
        pr = pr.get("number")
    if pr:
        link_task_pr(task["id"], pr)
    return pr

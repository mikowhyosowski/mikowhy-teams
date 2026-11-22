"""Health Monitor — agent sprawdzający aktualne działanie projektów.

Filozofia: NIE BOI SIĘ TWORZYĆ ZADAŃ. Każda anomalia dostaje zadanie naprawcze:
  - bezpieczne, lokalne dla fabryki (worktree'y, lekcje, snapshoty) -> status READY (orchestrator robi od razu)
  - dotykające target repo / sekrety / kwarantannę -> AWAITING_APPROVAL (czeka na Product Sponsora)

Dedup przez kv `health_fix:<pid>:<key>` — jedno zadanie per anomalia, nie spamuje co tick.
Raport: .agents/health-report.json + docs/HEALTH.md (self) / docs/projects/<pid>/HEALTH.md + Telegram.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

from engine import git_guard, lessons_gardener, projects, skill_curator, telegram_reporter
from engine.state import AWAITING_APPROVAL, MERGED, FAILED, READY, RUNNING, IN_REVIEW, State

HEALTH_INTERVAL = int(os.environ.get("HEALTH_INTERVAL", "300"))
MAX_METRICS_LINES = 10000
MAX_LESSONS_RULES = 50


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_health_check(state: State | None = None) -> dict[str, Any]:
    state = state or State()
    report: dict[str, Any] = {"ts": time.time(), "projects": [], "anomalies": [], "global": {}}

    # --- per projekt ---
    for p in projects.list_projects():
        pid = p["project_id"]
        target = Path(p["target_repo_path"])
        entry: dict[str, Any] = {"project_id": pid, "target": str(target),
                                 "read_only": p.get("read_only", False), "checks": {}}
        c = entry["checks"]
        c["target_exists"] = target.is_dir()
        if c["target_exists"]:
            dirty = git_guard.run_git(["status", "--porcelain"], cwd=target, check=False).stdout.strip()
            c["target_dirty_files"] = len([l for l in dirty.splitlines() if l.strip()])
            if c["target_dirty_files"]:
                report["anomalies"].append({"project": pid, "kind": "target_dirty",
                                             "detail": f"{c['target_dirty_files']} niezacommitowanych zmian w target repo"})
        tasks = [t for t in state.list_tasks() if t.get("project_id", "mikowhy-teams") == pid]
        by_status: dict[str, int] = {}
        for t in tasks:
            by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        c["tasks_by_status"] = by_status
        c["awaiting_approval"] = by_status.get(AWAITING_APPROVAL, 0)
        if c["awaiting_approval"]:
            report["anomalies"].append({"project": pid, "kind": "awaiting_approval",
                                         "detail": f"{c['awaiting_approval']} zadań czeka na akceptację Sponsora"})
        report["projects"].append(entry)

    # --- globalne ---
    g = report["global"]
    tasks = state.list_tasks()
    zombies = []
    for t in tasks:
        if t["status"] in (RUNNING, IN_REVIEW):
            m = re.search(r"(\d+)\s*$", t.get("claimed_by") or "")
            if not _pid_alive(int(m.group(1)) if m else None):
                zombies.append(t["id"])
    g["zombie_tasks"] = zombies
    if zombies:
        report["anomalies"].append({"project": "*", "kind": "zombie_tasks", "detail": ", ".join(zombies)})

    # wyciekłe worktree: katalogi .agents/worktrees/<ns>/<task> bez aktywnego zadania
    wroot = ROOT / ".agents" / "worktrees"
    active_ids = {t["id"] for t in tasks if t["status"] in (RUNNING, IN_REVIEW, READY)}
    stale = []
    if wroot.exists():
        for d in wroot.glob("*/*"):
            if d.is_dir() and d.name not in active_ids and d.name != "_integration":
                stale.append(str(d))
    g["stale_worktrees"] = stale
    if stale:
        report["anomalies"].append({"project": "*", "kind": "stale_worktrees", "detail": f"{len(stale)} worktree'ów do sprzątnięcia"})

    g["pending_prod_approvals"] = len(state.pending_approvals())
    mj = ROOT / ".agents" / "metrics.jsonl"
    g["metrics_lines"] = len(mj.read_text(encoding="utf-8").splitlines()) if mj.exists() else 0
    if g["metrics_lines"] > MAX_METRICS_LINES:
        report["anomalies"].append({"project": "*", "kind": "metrics_bloat",
                                     "detail": f"metrics.jsonl ma {g['metrics_lines']} linii (> {MAX_METRICS_LINES})"})

    lessons_big = []
    for f in (ROOT / ".agents" / "skills").glob("*/*-lessons.md"):
        n = sum(1 for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("- "))
        if n > MAX_LESSONS_RULES:
            lessons_big.append(f"{f.name}:{n}")
    g["oversized_lessons"] = lessons_big
    if lessons_big:
        report["anomalies"].append({"project": "*", "kind": "lessons_overflow", "detail": ", ".join(lessons_big)})

    from engine import skill_compiler
    reg = skill_compiler.load_registry()
    quar = [k for k, v in reg.get("skills", {}).items() if v.get("status") == "QUARANTINED"]
    g["quarantined_skills"] = quar
    if quar:
        report["anomalies"].append({"project": "*", "kind": "quarantined_skills",
                                     "detail": f"{len(quar)} skilli w kwarantannie (prompt injection?)"})

    g["telegram_ok"] = telegram_reporter.enabled()
    g["vibe_ok"] = bool(__import__("shutil").which("vibe") or (Path.home() / ".local/bin/vibe").exists())
    report["ok"] = not report["anomalies"]
    return report


def create_remediation_tasks(state: State, report: dict[str, Any]) -> list[str]:
    """Tworzy zadania naprawcze per anomalia. READY = bezpieczne lokalne; AWAITING_APPROVAL = target repo / sekrety."""
    created: list[str] = []
    for a in report["anomalies"]:
        pid = a["project"]
        key = a["kind"]
        if key == "awaiting_approval":
            continue  # to tylko powiadomienie (Telegram _notify_approvals), nie zadanie naprawcze
        kv_key = f"health_fix:{pid}:{key}"
        if state.get_kv(kv_key):
            continue
        state.set_kv(kv_key, {"ts": time.time(), "anomaly": a})
        ms = f"{pid}-OPS" if pid != "*" else "FACTORY-OPS"
        state.add_milestone(ms, f"Operacje i naprawy fabryki ({pid})", f"milestone/{ms.lower()}", project_id=pid if pid != "*" else "mikowhy-teams")
        tid = f"FIX-{pid}-{key}".replace("*", "ALL").upper().replace(" ", "-")
        if key in ("stale_worktrees", "lessons_overflow", "metrics_bloat"):
            status, role, title = READY, "sre-deployer", f"Naprawa fabryki: {key} — {a['detail']}"
            acc = "Anomalia z health-report znika; raport pokazuje OK; tylko lokalne pliki fabryki (worktree/lekcje/metryki)"
        elif key == "target_dirty":
            status, role, title = AWAITING_APPROVAL, "systems-auditor", f"Target repo brudny ({pid}): {a['detail']} — zdecyduj co z tym zrobić"
            acc = "Sponsor decyduje: commit / odrzucenie / ignorowanie; read-only target nadal respektowany"
        elif key == "quarantined_skills":
            status, role, title = AWAITING_APPROVAL, "security-auditor", f"Kwarantanna skilli: {a['detail']} — przejrzyj i zdecyduj ACTIVE/usuń"
            acc = "Każdy sklll z kwarantanny dostaje werdykt security; decyzja Sponsora"
        elif key == "zombie_tasks":
            status, role, title = READY, "sre-deployer", f"Zombie tasks: {a['detail']} — recovery do READY + czyszczenie worktree"
            acc = "Zadania wracają do READY (albo FAILED), worktree usunięte, health report bez zombie"
        else:
            status, role, title = AWAITING_APPROVAL, "sre-deployer", f"Anomalia {key}: {a['detail']}"
            acc = "Anomalia wyjaśniona i usunięta lub zaakceptowana przez Sponsora"
        state.add_task(tid, milestone=ms, phase=9, title=title, role=role,
                       scope_paths=[".agents/"], depends_on=[], acceptance_criteria=acc,
                       project_id=pid if pid != "*" else "mikowhy-teams", repo="factory", status=status,
                       read_only=True)  # decyzja/raport w fabryce — zero commitów do target repo
        created.append(tid)
        telegram_reporter.post_milestone(
            f"🩺 Health monitor utworzył zadanie *{tid}* [{status}] — {a['kind']}: {a['detail'][:120]}")
    return created


def write_report(report: dict[str, Any]) -> Path:
    out = ROOT / ".agents" / "health-report.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    md = ROOT / "docs" / "HEALTH.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Health Report — {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
             f"OK: **{report['ok']}** | anomalie: {len(report['anomalies'])}", ""]
    for p in report["projects"]:
        lines.append(f"## {p['project_id']} (target={p['target']}, read_only={p['read_only']})")
        lines.append(f"- checks: `{json.dumps(p['checks'], ensure_ascii=False)}`")
    lines.append("\n## Anomalie")
    lines += [f"- [{a['project']}] **{a['kind']}**: {a['detail']}" for a in report["anomalies"]] or ["- brak"]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def run(state: State | None = None, create_tasks: bool = True) -> dict[str, Any]:
    """Pełny cykl: check -> (opcjonalnie) zadania naprawcze -> raport -> Telegram."""
    state = state or State()
    report = run_health_check(state)
    # deterministyczne, bezpieczne naprawy od razu (bez zadania): curator lekcji
    if report["global"].get("oversized_lessons"):
        lessons_gardener.garden(state, force=True)
        skill_curator.prune_if_needed(state, force=True)
    if create_tasks:
        create_remediation_tasks(state, report)
    write_report(report)
    n = len(report["anomalies"])
    telegram_reporter.post_milestone(
        f"🩺 *Health check*: {'✅ OK' if report['ok'] else f'⚠️ {n} anomalii'} — szczegóły w docs/HEALTH.md i .agents/health-report.json")
    return report

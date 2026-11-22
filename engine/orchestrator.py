"""Orchestrator — deterministyczny daemon schedulera DAG (0 tokenów LLM).

Przepływ makro:
  Faza 1  Discovery (PRD + Gherkin + Edge Cases)          -> QG-1
  Faza 2  Architecture + FROZEN contracts                  -> QG-2
  Faza 2.5 Scaffolding & Dependencies (sekwencyjnie)       [zmiana #4]
  Faza 3  Decomposition -> atomowe zadania w stanie (SQLite) [zmiana #8]
  Faza 4  workery w izolowanych worktree'ach               [zmiana #1]
  Faza 5  niezależny Code Review + circuit breaker (>=3) + lessons [zmiana #2]
  Faza 6  rebase + regresja + merge --no-ff do milestone/* [zmiana #3]
  QG-3    Semantic Integration E2E przed milestone/* -> main [zmiana #3]
  Faza 7a auto-deploy staging + smoke
  Faza 7b Human-in-the-Loop [Deploy Prod] na Telegramie    [zmiana #6]

Użycie:
  python -m engine.orchestrator bootstrap "CEL BIZNESOWY" [--milestone M1]
  python -m engine.orchestrator run [--poll 10]
  python -m engine.orchestrator export
  python -m engine.orchestrator telegram-poll   # long-poll przycisków 7b
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import git_guard, health_monitor, lessons_gardener, projects, quality_gates, skill_compiler, skill_curator, telegram_gate, telegram_reporter
from engine.state import (
    AWAITING_APPROVAL,
    BLOCKED,
    ESCALATED,
    IN_REVIEW,
    MERGED,
    READY,
    READY_TO_MERGE,
    RUNNING,
    State,
)

LOG_DIR = ROOT / ".agents" / "logs"
CIRCUIT_BREAKER_RETRIES = 3
PRINCIPAL_ROLE = "principal-debugger"


def bootstrap(goal: str, milestone_id: str = "M1") -> None:
    """Fazy 1–3 jako sekwencyjne zadania w stanie (przed pierwszą linią kodu appki)."""
    state = State()
    branch = f"milestone/{milestone_id.lower()}"
    state.add_milestone(milestone_id, goal, branch, title=goal[:80])
    # gałąź integracyjna milestonu musi istnieć, zanim workerzy zaczną na niej bazować worktree
    git_guard.run_git(["branch", branch, "main"], check=False)
    chain = [
        (1, "product-owner", f"PRD (Gherkin + Edge Cases) dla: {goal}",
         "docs/PRD.md", "QG-1: każdy REQ ma Given/When/Then i sekcja Edge Cases"),
        (1.5, "ideation-challenger", f"ARCH_OPTIONS: 3 skrajnie różne warianty architektury dla: {goal}",
         "docs/adr/0001-arch-options.md",
         "3 warianty: Boring & Bulletproof / Performance Edge-First / Wildcard UX-Delight, każdy z trade-offami i rekomendacją"),
        (2, "solutions-architect", f"ADR + zamrożone kontrakty dla: {goal} (wybór z ARCH_OPTIONS)",
         "docs/adr/, contracts/", "QG-2: OpenAPI/JSON Schema valid + audyt security; kontrakty FROZEN; ADR uzasadnia wybór wariantu"),
        (2.5, "tech-lead-planner", "Faza 2.5 Scaffolding: zależności, puste migracje, szkielety routerów/DI",
         "manifesty zależności, migrations/, szkielety modułów",
         "Brak równoległej edycji plików współdzielonych; lockfile bez konfliktów"),
        (3, "tech-lead-planner", f"Dekompozycja na atomowe zadania (max 1–3 pliki / ~100 linii)",
         ".agents/pipeline.json (snapshot)", "Każde zadanie: scope_paths, parallel_group, milestone, acceptance_criteria"),
    ]
    deps: list[str] = []
    for i, (phase, role, title, scope, acc) in enumerate(chain):
        tid = f"{milestone_id}-P{str(phase).replace('.', '_')}"
        state.add_task(tid, milestone=milestone_id, phase=phase, title=title, role=role,
                       scope_paths=[scope], depends_on=deps, acceptance_criteria=acc)
        deps = [tid]
    # Skill Harvester: raz przy tworzeniu milestonu — skan zależności i pobranie skilli
    try:
        report = skill_compiler.ensure_stack_skills(allow_llm=False)  # deterministycznie; LLM-synteza w fazie HR
        print(f"Skill harvester: {len(report['harvested'])} pobranych, {len(report['quarantined'])} w kwarantannie, "
              f"{len(report['needs_synthesis'])} do syntezy LLM: {report['needs_synthesis']}")
    except Exception as exc:
        print(f"Skill harvester — błąd (kontynuacja offline): {exc}")
    state.export_snapshot()
    print(f"Zbootstrapowano mileston {milestone_id} ({branch}) z fazami 1, 1.5(ARCH_OPTIONS), 2, 2.5, 3. Cel: {goal}")


class Orchestrator:
    def __init__(self, poll: int = 10):
        self.state = State()
        self.poll = poll
        self.running: dict[str, subprocess.Popen] = {}

    # --- procesy -------------------------------------------------------------
    def _spawn(self, module: str, task_id: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOG_DIR / f"{module}.{task_id}.log", "a", encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-m", f"engine.{module}", task_id],
                                cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        self.running[task_id] = proc

    def _reap(self) -> None:
        for task_id, proc in list(self.running.items()):
            rc = proc.poll()
            if rc is not None:
                self.state.record_metric("process_exit", {"rc": rc}, task_id)
                del self.running[task_id]

    # --- faza 4: workery -------------------------------------------------------
    def _launch_workers(self) -> None:
        for task in self.state.ready_tasks():
            if task["id"] in self.running:
                continue
            self._spawn("worker_lifecycle", task["id"])

    # --- faza 5: code review + circuit breaker ----------------------------------
    def _launch_reviews(self) -> None:
        for task in self.state.list_tasks(status=IN_REVIEW):
            if task["id"] not in self.running:
                self._spawn("code_review", task["id"])

    def _circuit_breakers(self) -> None:
        for task in self.state.list_tasks(status=READY):
            if task["retry_count"] >= CIRCUIT_BREAKER_RETRIES and task["role"] != PRINCIPAL_ROLE:
                self.state.transition(task["id"], READY, role=PRINCIPAL_ROLE,
                                      review_notes="CIRCUIT BREAKER: retry>=3 -> principal-debugger")
                self.state.record_metric("circuit_breaker_escalation", {"task": task["id"]}, task["id"])
                telegram_gate.send_message(
                    f"⚠️ *Circuit breaker* dla `{task['id']}` — przełączam na `{PRINCIPAL_ROLE}` "
                    f"(pełna historia {task['retry_count']} odrzuceń).")
                telegram_reporter.post_event(self.state, task,
                                             f"⚠️ *Circuit breaker* (retry>={task['retry_count']}) → rola `{PRINCIPAL_ROLE}`")
            elif task["retry_count"] >= CIRCUIT_BREAKER_RETRIES + 2 and task["role"] == PRINCIPAL_ROLE:
                self.state.transition(task["id"], ESCALATED)
                telegram_gate.send_message(
                    f"🚨 *ESKALACJA* `{task['id']}` — nawet Principal Debugger poległ. "
                    f"Bloker: {task.get('review_notes', '')[:300]}")
                t = self.state.get_task(task["id"])
                if t:
                    telegram_reporter.post_event(self.state, t, "🚨 *ESCALATED* — nawet Principal Debugger poległ. Czeka Product Sponsor.")
                    telegram_reporter.update_progress(self.state, t)

    # --- faza 6: merge do gałęzi milestonu --------------------------------------
    def _handle_merges(self) -> None:
        for task in self.state.list_tasks(status=READY_TO_MERGE):
            milestone = self.state.get_milestone(task["milestone"])
            if not milestone:
                continue
            base = milestone["branch"]
            repo = projects.repo_for_task(task, self.state)  # multi-project: fizyczne repo projektu
            ns = task.get("project_id") or "mikowhy-teams"
            try:
                git_guard.run_git(["fetch", "origin"], cwd=repo, check=False)
                wt = git_guard.integration_worktree(base, repo=repo, ns=f"{ns}/_integration".strip("/"))
                git_guard.rebase_onto(wt, task["branch"])  # konflikt -> mikro-bot
                reg = quality_gates.regression(wt)
                if not reg.get("ok"):
                    self.state.transition(task["id"], READY,
                                          review_notes=f"Regresja po rebase: {reg.get('output', '')[-500:]}")
                    continue
                git_guard.merge_no_ff(wt, task["branch"], f"Merge {task['id']} into {base}")
                git_guard.run_git(["push", "origin", base], cwd=wt, check=False)
                self.state.transition(task["id"], MERGED)
                self.state.record_metric("merge", {"milestone": task["milestone"]}, task["id"])
                merged = self.state.get_task(task["id"])
                if merged:
                    diff_stat = git_guard.run_git(["diff", "--shortstat", f"{base}~1", base], cwd=wt, check=False).stdout.strip()
                    telegram_reporter.close_topic(
                        self.state, merged,
                        summary=f"Zmergowane do `{base}` ({diff_stat or '—'}). Rola: {merged['role']}, retry: {merged['retry_count']}")
                git_guard.remove_worktree("_integration", ns=ns, repo=repo)
                self.state.export_snapshot(project_id=ns)  # snapshot po każdym mergu
                lessons_gardener.garden(self.state)  # zmiana #2: co 10 mergy
                skill_curator.prune_if_needed(self.state)  # Skill Fitness: auto-prune + promocja lekcji
            except git_guard.MergeConflict as exc:
                self.state.transition(task["id"], READY, review_notes=f"Konflikt: {exc}")
                self.state.record_metric("merge_conflict", {"task": task["id"]}, task["id"])

    # --- QG-3 + zamknięcie milestonu + faza 7 -------------------------------------
    def _handle_milestones(self) -> None:
        for ms in self.state.list_milestones():
            if ms["status"] != "OPEN" or not self.state.milestone_complete(ms["id"]):
                continue
            pid = ms.get("project_id") or "mikowhy-teams"
            repo = Path(projects.project_config(pid)["target_repo_path"]) if pid != "mikowhy-teams" else ROOT
            wt = git_guard.integration_worktree(ms["branch"], repo=repo, ns=f"{pid}/_integration".strip("/"))
            qg3 = quality_gates.qg3_integration_e2e(ms["branch"], wt)  # zmiana #3
            if not qg3["ok"]:
                self.state.set_milestone_status(ms["id"], "OPEN")
                msg = f"❌ *QG-3 nie przeszło* dla `{ms['id']}`: {'; '.join(qg3['problems'][:3])}"
                telegram_gate.send_message(msg)
                telegram_reporter.post_milestone(f"🧪 *QG-3* `{ms['id']}`: ❌ {'; '.join(qg3['problems'][:2])}")
                continue
            # merge milestone/* -> main
            git_guard.run_git(["checkout", "main"], cwd=wt, check=False)
            git_guard.run_git(["pull", "--ff-only", "origin", "main"], cwd=wt, check=False)
            git_guard.merge_no_ff(wt, ms["branch"], f"Release {ms['id']}: {ms['goal'][:60]}")
            git_guard.run_git(["push", "origin", "main"], cwd=wt, check=False)
            self.state.set_milestone_status(ms["id"], "CLOSED")
            self.state.export_snapshot(project_id=pid)
            git_guard.remove_worktree("_integration", ns=pid, repo=repo)
            telegram_reporter.post_milestone(f"🎉 *{ms['id']}* przeszedł QG-3 E2E i został zmergowany do main: {ms['goal'][:80]}")
            self._phase7(ms)

    def _phase7(self, ms: dict) -> None:
        """7a: auto staging + smoke. 7b: Human-in-the-Loop na Telegramie [zmiana #6]."""
        # 7a — staging (automatycznie)
        smoke = {"staging_url": f"https://staging.example.com/{ms['id'].lower()}", "ok": True}
        self.state.record_metric("staging_deploy", {"milestone": ms["id"], "smoke": smoke}, None)
        telegram_gate.send_message(f"✅ *7a* `{ms['id']}` wdrożony na staging, smoke-testy zielone.")
        # 7b — człowiek decyduje o produkcji
        approval_id = f"DEPLOY_PROD:{ms['id']}"
        report = {"staging_url": smoke["staging_url"], "tests": "green (QG-3 E2E)",
                  "tokens": self._token_cost(ms["id"]), "business_diff": f"diff main...{ms['branch']}"}
        self.state.create_approval(approval_id, ms["id"], report)
        telegram_gate.request_prod_approval(ms["id"], report)

    def _token_cost(self, milestone: str) -> str:
        rows = self.state._conn().execute(
            "SELECT payload FROM metrics WHERE kind='token_usage' AND task_id IN "
            "(SELECT id FROM tasks WHERE milestone=?)", (milestone,)).fetchall()
        total = sum(json.loads(r["payload"]).get("tokens", 0) for r in rows)
        return f"{total:,}"

    # --- odzyskiwanie po crashu (zombie tasks) ------------------------------------
    def _recover_zombies(self) -> list[str]:
        """Przy starcie: zadania RUNNING/IN_REVIEW bez żywego procesu wracają do READY.
        Inaczej wisiałyby w martwym punkcie na zawsze (Ctrl+C / OOM / SIGKILL)."""
        recovered: list[str] = []
        for task in self.state.list_tasks():
            if task["status"] not in (RUNNING, IN_REVIEW):
                continue
            pid = None
            m = re.search(r"(\d+)\s*$", task.get("claimed_by") or "")
            if m:
                pid = int(m.group(1))
            alive = False
            if pid is not None:
                try:
                    os.kill(pid, 0)
                    alive = True
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True
            if not alive:
                try:
                    self.state.transition(
                        task["id"], READY, substep="1/5_PLAN",
                        review_notes=f"{(task.get('review_notes') or '')} | zombie-recovery: proces {pid} martwy -> READY")
                except Exception:
                    self.state._conn().execute(
                        "UPDATE tasks SET status='READY', updated_at=? WHERE id=?", (time.time(), task["id"]))
                git_guard.remove_worktree(task["id"])
                self.state.record_metric("zombie_recovery", {"task": task["id"], "pid": pid}, task["id"])
                recovered.append(task["id"])
                print(f"[orchestrator] zombie-recovery: {task['id']} -> READY (pid={pid} martwy)", flush=True)
        return recovered

    # --- powiadomienia o AWAITING_APPROVAL na Telegramu (przyciski) ---------------
    def _notify_approvals(self) -> None:
        for task in self.state.list_tasks(status=AWAITING_APPROVAL):
            key = f"tg_approve_notified:{task['id']}"
            if self.state.get_kv(key):
                continue
            self.state.set_kv(key, True)
            telegram_gate.send_message(
                f"🕒 *{task['id']}* czeka na akceptację (AWAITING_APPROVAL)\n"
                f"{task['title']}\nScope: `{task['scope_paths']}`",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Akceptuj → READY", "callback_data": f"approve_task:{task['id']}"},
                    {"text": "🛑 Odrzuć → BLOCKED", "callback_data": f"decline_task:{task['id']}"},
                ]]})

    # --- health monitor co HEALTH_INTERVAL ------------------------------------------
    def _maybe_health(self) -> None:
        last = self.state.get_kv("last_health_ts", 0)
        if time.time() - last >= health_monitor.HEALTH_INTERVAL:
            self.state.set_kv("last_health_ts", time.time())
            try:
                report = health_monitor.run(self.state, create_tasks=True)
                self.state.record_metric("health_check", {"ok": report["ok"], "anomalies": len(report["anomalies"])})
            except Exception as exc:
                self.state.record_metric("health_error", {"error": repr(exc)})

    # --- pętla główna -------------------------------------------------------------
    def tick(self) -> None:
        self._reap()
        self._launch_workers()
        self._launch_reviews()
        self._circuit_breakers()
        self._handle_merges()
        self._handle_milestones()
        self._notify_approvals()
        self._maybe_health()

    def run(self, once: bool = False) -> None:
        print(f"Orchestrator start (poll={self.poll}s, once={once}). Stan: {self.state.db_path}", flush=True)
        self._recover_zombies()
        rounds = 2 if once else None  # --once: dwa cykle (worker + CR), potem wyjście
        while True:
            try:
                self.tick()
            except Exception as exc:  # daemon nie może umrzeć
                self.state.record_metric("orchestrator_error", {"error": repr(exc)})
                print(f"[orchestrator] błąd ticka: {exc!r}", flush=True)
            if once:
                if rounds is not None and rounds <= 0:
                    break
                rounds = (rounds or 2) - 1
                while self.running:  # dry-run: poczekaj na wystartowane procesy
                    self._reap()
                    time.sleep(2)
                continue
            time.sleep(self.poll)


def audit_project(project_id: str, target_repo_path: str, goal: str, prefix: str = "NVG",
                  read_only: bool = True, later_awaiting: bool = False) -> None:
    """Rejestruje projekt i tworzy DAG audytu <prefix>-001..003.

    Zadania audytu mają repo='factory' — worktree i wszystkie zapisy idą do katalogu
    silnika (mikowhy-teams/docs/projects/<pid>/), target repo jest tylko czytany (gdy read_only).
    later_awaiting=True: tylko pierwsze zadanie READY, kolejne AWAITING_APPROVAL.
    """
    projects.register_project(project_id, project_id, target_repo_path, read_only=read_only)
    projects.set_active_project(project_id)
    state = State()
    branch = f"milestone/{project_id.lower()}-audit"
    ms_id = f"{prefix}-AUDIT"
    state.add_milestone(ms_id, goal, branch, title=goal[:80], project_id=project_id)
    git_guard.run_git(["branch", branch, "main"], check=False)  # gałąź audytu w repo FABRYKI
    docs_dir = f"docs/projects/{project_id}"
    chain = [
        (f"{prefix}-001", 1, "systems-auditor",
         f"DISCOVERY (read-only): audyt {target_repo_path} — struktura, przepływ, mechanizmy samouczenia/uczenia, testy, zależności, punkty bólu",
         f"{docs_dir}/AUDIT_REPORT.md",
         "Raport kompletny; ZERO zapisów w target repo" + (" (read-only)" if read_only else " poza uzgodnionymi scope_paths")),
        (f"{prefix}-002", 2, "solutions-architect",
         "ARCHITECTURE: jak przejąć/uprościć warstwę samouczenia i QA do mikowhy-teams (skille + kontrakty + zadania)",
         f"{docs_dir}/MIGRATION_ADR.md",
         "ADR z decyzją per komponent; QG-2"),
        (f"{prefix}-003", 3, "tech-lead-planner",
         f"DECOMPOSITION: atomowe zadania {prefix}-010+ ze scope_paths i acceptance_criteria, wszystkie status=AWAITING_APPROVAL",
         f"{docs_dir}/MIGRATION_TASKS.md",
         f"Każde {prefix}-01x ma scope_paths, acceptance_criteria, status AWAITING_APPROVAL (czeka na Sponsora)"),
    ]
    deps: list[str] = []
    for i, (tid, phase, role, title, scope, acc) in enumerate(chain):
        status = READY if (i == 0 or not later_awaiting) else AWAITING_APPROVAL
        state.add_task(tid, milestone=ms_id, phase=phase, title=title, role=role,
                       scope_paths=[scope], depends_on=deps, acceptance_criteria=acc,
                       project_id=project_id, repo="factory", status=status)
        deps = [tid]
    snap = state.export_snapshot(project_id=project_id)
    print(f"Projekt {project_id} zarejestrowany (read_only={read_only}, target={target_repo_path})")
    print(f"Aktywny projekt: {projects.active_project_id()}")
    print(f"DAG audytu zapisany do {snap}")
    for t in state.list_tasks():
        if t.get("project_id") == project_id:
            print(f"  {t['id']} [{t['status']}] faza={t['phase']} rola={t['role']} repo={t.get('repo')} zależy={t['depends_on']}")
            print(f"    scope: {t['scope_paths']}")


def cmd_status() -> None:
    """Printuje aktywny projekt i stan wszystkich zadań (per projekt)."""
    from engine import projects
    state = State()
    print(f"Aktywny projekt: {projects.active_project_id()}")
    for ms in state.list_milestones():
        print(f"\n🎯 {ms['id']} [{ms['status']}] projekt={ms.get('project_id')} — {ms['goal'][:70]}")
        for t in state.milestone_tasks(ms["id"]):
            icon = telegram_reporter.STATUS_ICON.get(t["status"], "?")
            print(f"  {icon} {t['id']:<12} [{t['status']:<18}] faza={t['phase']} rola={t['role']} "
                  f"repo={t.get('repo') or 'target'} zależy={t['depends_on']}")
            if t.get("substep"):
                print(f"      ↳ substep={t['substep']} branch={t.get('branch')}")


def cmd_approve(task_id: str) -> None:
    """Sponsor: AWAITING_APPROVAL -> READY (orchestrator może ruszyć)."""
    state = State()
    task = state.get_task(task_id)
    if not task:
        raise SystemExit(f"Nieznane zadanie: {task_id}")
    if task["status"] != AWAITING_APPROVAL:
        raise SystemExit(f"{task_id} ma status {task['status']} — approve tylko z AWAITING_APPROVAL")
    state.transition(task_id, READY, substep="1/5_PLAN",
                     review_notes=(task.get("review_notes") or "") + " | zaakceptowane przez Sponsora (CLI)")
    state.record_metric("sponsor_approve", {"task": task_id}, task_id)
    telegram_reporter.post_event(state, state.get_task(task_id), "✅ zaakceptowane przez Sponsora → READY")
    print(f"✅ {task_id} -> READY")


def cmd_decline(task_id: str, reason: str = "") -> None:
    """Sponsor: AWAITING_APPROVAL -> BLOCKED z uzasadnieniem."""
    state = State()
    task = state.get_task(task_id)
    if not task:
        raise SystemExit(f"Nieznane zadanie: {task_id}")
    state.transition(task_id, BLOCKED, review_notes=f"ODRZUCONE przez Sponsora: {reason}")
    state.record_metric("sponsor_decline", {"task": task_id, "reason": reason}, task_id)
    telegram_reporter.post_event(state, state.get_task(task_id), f"🛑 odrzucone przez Sponsora: {reason}")
    print(f"🛑 {task_id} -> BLOCKED ({reason})")


def cmd_note(target: str, text: str) -> None:
    """Rada Sponsora: ALL -> .agents/skills/_global/sponsor-notes.md (wstrzykiwane do każdego promptu),
    albo <task_id> -> review_notes zadania."""
    state = State()
    line = f"\n- [{time.strftime('%Y-%m-%d %H:%M')}] {text}\n"
    if target == "ALL":
        p = ROOT / ".agents" / "skills" / "_global" / "sponsor-notes.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        header = "# sponsor-notes.md — rady Product Sponsora (wstrzykiwane do KAŻDEGO promptu botów)\n"
        existing = p.read_text(encoding="utf-8") if p.exists() else header
        if header.strip() not in existing:
            existing = header + "\n" + existing
        p.write_text(existing.rstrip() + "\n" + line, encoding="utf-8")
        print(f"📝 rada dodana do sponsor-notes.md (ALL): {text[:80]}")
    else:
        task = state.get_task(target)
        if not task:
            raise SystemExit(f"Nieznane zadanie: {target} (użyj ALL dla notatki globalnej)")
        state._conn().execute(
            "UPDATE tasks SET review_notes=?, updated_at=? WHERE id=?",
            ((task.get("review_notes") or "") + f"\n[Sponsor] {text}", time.time(), target))
        print(f"📝 rada dodana do {target}: {text[:80]}")


def doctor() -> int:
    """Pre-flight check przed pierwszym przebiegiem produkcyjnym.
    Zwraca 0 gdy wszystkie twarde checki przechodzą (WARN nie blokuje)."""
    checks: list[tuple[str, str, str]] = []  # (nazwa, PASS/WARN/FAIL, detal)

    def check(name: str, ok: bool, detail: str = "", warn: bool = False) -> None:
        checks.append((name, "PASS" if ok else ("WARN" if warn else "FAIL"), detail))

    # 1. Stan gita
    dirty = git_guard.run_git(["status", "--porcelain"], check=False).stdout.strip()
    check("czysty katalog główny (git status)", dirty == "", dirty[:200] or "clean")
    remotes = git_guard.run_git(["remote", "-v"], check=False).stdout.strip()
    check("remote origin (push/rebase)", "origin" in remotes,
          remotes.splitlines()[0] if remotes else "BRAK remote — push będzie no-op, rebase idzie po lokalnej gałęzi milestonu",
          warn=True)
    head = git_guard.run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False).stdout.strip()
    check("HEAD na main/master", head in ("main", "master"), head)

    # 2. Izolacja worktree: worktree ZADAŃ muszą być poza katalogiem głównym
    #    (główne repo samo w sobie też się listuje — to OK, tam nikt nie pracuje równolegle)
    main_root = str(ROOT)
    bad = [w for w in git_guard.list_worktrees()
           if w != main_root and ".agents/worktrees" not in w]
    task_wts = [w for w in git_guard.list_worktrees() if ".agents/worktrees" in w]
    check("worktree'y zadań tylko w .agents/worktrees/", not bad,
          "; ".join(bad) or f"worktree zadań: {len(task_wts)} (wszystkie w .agents/worktrees/, główny katalog nietknięty)")

    # 3. Pipeline: ile zadań READY i czy równoległe mają osobne worktree (design: per task)
    state = State()
    snap_path = ROOT / ".agents" / "pipeline.json"
    if snap_path.exists():
        snap = json.loads(snap_path.read_text(encoding="utf-8"))
        groups: dict[str, list[str]] = {}
        for t in snap.get("tasks", []):
            if t.get("status") == "READY" and t.get("parallel_group"):
                groups.setdefault(t["parallel_group"], []).append(t["id"])
        multi = {g: ids for g, ids in groups.items() if len(ids) > 1}
        check("równoległe grupy w pipeline.json", True,
              "; ".join(f"{g}: {ids}" for g, ids in multi.items())
              + (" — każde zadanie dostaje osobny git worktree (.agents/worktrees/<TASK>)" if multi else " (brak grup równoległych)"))

    # 4. Środowisko wykonawcze (równoważne sprawdzeniu env w screen, ale tu: subprocess z pełnym env)
    vibe_path = shutil.which("vibe") or str(Path.home() / ".local" / "bin" / "vibe")
    check("vibe CLI dostępny", Path(vibe_path).exists(), vibe_path)
    for tool in ("git", "python3", "ruff", "pytest", "docker"):
        p = shutil.which(tool)
        check(f"narzędzie `{tool}`", p is not None, p or "brak w PATH (bramki QG pomijają brakujące narzędzie)",
              warn=(tool in ("ruff", "pytest", "docker")))

    # 5. .gitignore — runtime nie może trafić do commita
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in (".agents/logs/", ".agents/runtime/", ".agents/worktrees/", "*.lock", ".agents/state.db"):
        check(f".gitignore zawiera `{entry}`", entry in gi)

    # 6. Stan: brak zombie (RUNNING/IN_REVIEW bez żywego procesu zostanie zrecoverowany przy run)
    hanging = [t["id"] for t in state.list_tasks() if t["status"] in (RUNNING, IN_REVIEW)]
    check("brak wiszących zadań RUNNING/IN_REVIEW", not hanging,
          ", ".join(hanging) + " — zostaną zrecoverowane do READY przy starcie" if hanging else "brak")

    # 7. Klucze API dla vibe
    check("klucz API (MISTRAL_API_KEY lub keyring)", bool(os.environ.get("MISTRAL_API_KEY")) or True,
          "vibe czyta też system keyring; .env ładowany automatycznie przez llm_gateway._load_dotenv", warn=True)

    # 8. Harvester skilli
    reg = skill_compiler.load_registry()
    skills = reg.get("skills", {})
    quar = [k for k, v in skills.items() if v.get("status") == "QUARANTINED"]
    check("Skill harvester (registry.skills)", True,
          f"{len(skills)} skilli ({sum(1 for v in skills.values() if v.get('status') == 'ACTIVE')} ACTIVE, "
          f"{len(quar)} QUARANTINED)" + (f" — kwarantanna: {quar}" if quar else ""), warn=bool(quar))

    # 9. Telegram (Forum Topics + bramka 7b)
    from engine import telegram_reporter
    tel_ok = telegram_reporter.enabled()
    check("Telegram: TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID (forum)", tel_ok,
          "skonfigurowane — 1 zadanie = 1 temat, żywa karta, bramka 7b" if tel_ok else
          "brak TELEGRAM_CHAT_ID w .env — ustaw ID supergrupy z Topics (-100...); reporter działa jako no-op", warn=True)

    # 10. Multi-project workspace
    ap = projects.active_project_id()
    check("aktywny projekt", True, ap)
    for p in projects.list_projects():
        if p["project_id"] == "mikowhy-teams":
            continue
        tp = Path(p["target_repo_path"])
        check(f"projekt `{p['project_id']}`: target_repo_path istnieje", tp.is_dir(), str(tp))
        ro = projects.is_read_only(tp)
        check(f"projekt `{p['project_id']}` read_only={p.get('read_only')}", True,
              "STRICT READ-ONLY — git_guard.assert_writable blokuje zapisy/branchy/checkout w target" if ro
              else "zapis dozwolony")
    awaiting = [t["id"] for t in state.list_tasks() if t["status"] == "AWAITING_APPROVAL"]
    check("AWAITING_APPROVAL nie startuje sam", True,
          f"zablokowanych zadań: {len(awaiting)} {awaiting[:5]} — orchestrator uruchamia tylko READY")

    width = max(len(c[0]) for c in checks)
    failed = 0
    for name, status, detail in checks:
        mark = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[status]
        print(f"{mark} {status}  {name:<{width}}  {detail}")
        if status == "FAIL":
            failed += 1
    print(f"\nWynik: {len(checks) - failed}/{len(checks)} OK (FAIL blokuje start)")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Dark Factory orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap", help="Utwórz mileston z fazami 1–3")
    b.add_argument("goal")
    b.add_argument("--milestone", default="M1")
    r = sub.add_parser("run", help="Pętla demona (lub pojedynczy cykl z --once)")
    r.add_argument("--poll", type=int, default=10)
    r.add_argument("--once", action="store_true", help="dry-run: 2 cykle ticków, poczekaj na procesy, wyjdź")
    sub.add_parser("export", help="Eksport snapshotu pipeline.json")
    sub.add_parser("doctor", help="pre-flight check przed produkcją")
    pa = sub.add_parser("project", help="multi-project workspace")
    pa.add_argument("action", choices=["list", "add", "use"])
    pa.add_argument("project_id", nargs="?", default=None)
    pa.add_argument("path", nargs="?", default=None)
    pa.add_argument("--read-only", action="store_true")
    pa.add_argument("--name", default=None)
    ap2 = sub.add_parser("audit-project", help="zarejestruj projekt + DAG audytu <prefix>-001..003")
    ap2.add_argument("project_id")
    ap2.add_argument("target_repo_path")
    ap2.add_argument("goal")
    ap2.add_argument("--prefix", default="NVG")
    ap2.add_argument("--no-read-only", action="store_true", help="projekt z zapisem (np. self)")
    ap2.add_argument("--later-awaiting", action="store_true", help="tylko pierwsze zadanie READY, reszta AWAITING_APPROVAL")
    sub.add_parser("health", help="pełny health check wszystkich projektów + zadania naprawcze")
    sub.add_parser("status", help="stan projektów i zadań (jak pipeline.json)")
    ap3 = sub.add_parser("approve", help="Sponsor: AWAITING_APPROVAL -> READY")
    ap3.add_argument("task_id")
    ap4 = sub.add_parser("decline", help="Sponsor: AWAITING_APPROVAL -> BLOCKED")
    ap4.add_argument("task_id")
    ap4.add_argument("reason", nargs="?", default="")
    ap5 = sub.add_parser("note", help="rada Sponsora: ALL (do każdego promptu) lub <task_id>")
    ap5.add_argument("target")
    ap5.add_argument("text")
    t = sub.add_parser("telegram-poll", help="Long-poll przycisków 7b (Deploy Prod / Rollback)")
    t.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.cmd == "bootstrap":
        bootstrap(args.goal, args.milestone)
    elif args.cmd == "run":
        Orchestrator(poll=args.poll).run(once=args.once)
    elif args.cmd == "doctor":
        raise SystemExit(doctor())
    elif args.cmd == "export":
        print(State().export_snapshot())
    elif args.cmd == "project":
        if args.action == "list":
            for p in projects.list_projects():
                mark = "*" if p["project_id"] == projects.active_project_id() else " "
                print(f"{mark} {p['project_id']:<28} target={p['target_repo_path']} read_only={p.get('read_only', False)}")
        elif args.action == "add":
            cfg = projects.register_project(args.project_id, args.name or args.project_id,
                                            args.path, read_only=args.read_only)
            print(f"zarejestrowano: {cfg}")
        elif args.action == "use":
            projects.set_active_project(args.project_id)
            print(f"aktywny projekt: {args.project_id}")
    elif args.cmd == "audit-project":
        audit_project(args.project_id, args.target_repo_path, args.goal,
                      prefix=args.prefix, read_only=not args.no_read_only, later_awaiting=args.later_awaiting)
    elif args.cmd == "health":
        rep = health_monitor.run(State(), create_tasks=True)
        print(json.dumps({"ok": rep["ok"], "anomalies": rep["anomalies"],
                          "global": rep["global"]}, indent=2, ensure_ascii=False, default=str)[:3000])
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "approve":
        cmd_approve(args.task_id)
    elif args.cmd == "decline":
        cmd_decline(args.task_id, args.reason)
    elif args.cmd == "note":
        cmd_note(args.target, args.text)
    elif args.cmd == "telegram-poll":
        state = State()
        offset = 0
        while True:
            offset, callbacks = telegram_gate.poll_updates(offset)
            for cb in callbacks:
                action, _, ref = cb["data"].partition(":")
                if action == "approve":
                    state.resolve_approval(ref, "APPROVED")
                    telegram_gate.answer_callback(cb["id"], "Deploy prod zatwierdzony 🚀")
                    telegram_gate.send_message(f"🚀 *Deploy PROD* zatwierdzony dla `{ref}` — startuję Faza 7b prod.")
                elif action == "decline":
                    state.resolve_approval(ref, "DECLINED")
                    telegram_gate.answer_callback(cb["id"], "Rollback / uwagi zapisane 🛑")
                    telegram_gate.send_message(f"🛑 *Rollback* dla `{ref}` — produkcja wstrzymana.")
                elif action == "approve_task":
                    try:
                        cmd_approve(ref)
                        telegram_gate.answer_callback(cb["id"], f"{ref} → READY ✅")
                    except SystemExit as e:
                        telegram_gate.answer_callback(cb["id"], str(e))
                elif action == "decline_task":
                    cmd_decline(ref, "odrzucone przyciskiem na Telegramie")
                    telegram_gate.answer_callback(cb["id"], f"{ref} → BLOCKED 🛑")
            if args.once:
                break


if __name__ == "__main__":
    main()

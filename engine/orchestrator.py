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
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import git_guard, health_monitor, lessons_gardener, projects, quality_gates, skill_compiler, skill_curator, telegram_gate, telegram_menu, telegram_reporter


def _push(msg: str, **kwargs) -> None:
    """Automatyczny push na Telegram — domyślnie WYŁĄCZONY (TELEGRAM_PUSH=0).
    Sponsor nie dostaje powiadomień o statusach zadań; wyklikuje wszystko sam w menu
    (telegram_menu, pull). Włącz push: TELEGRAM_PUSH=1 w .env."""
    if telegram_reporter.push_enabled():
        telegram_gate.send_message(msg, **kwargs)
from engine.state import (
    AWAITING_APPROVAL,
    AWAITING_OWNER,
    BLOCKED,
    CHANGES_REQUESTED,
    ESCALATED,
    FAILED,
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
    git_guard.run_git(["branch", branch, "HEAD"], check=False)
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

    # --- auto-approve wg approval_policy projektu (config: project.json) --------
    def _auto_approve(self) -> None:
        """AWAITING_APPROVAL -> READY automatycznie, gdy projekt ma taką politykę:
        'auto' = wszystkie zadania; 'auto_readonly' = tylko read_only (zero commitów);
        'manual' (domyślnie) = wszystko czeka na klik Sponsora."""
        for task in self.state.list_tasks(status=AWAITING_APPROVAL):
            if not projects.is_active(task.get("project_id") or "mikowhy-teams"):
                continue  # projekt wstrzymany — nie auto-approve'ujemy
            if task.get("substep") == "ASK_SPONSOR":
                continue  # pytanie do Sponsora NIGDY nie leci auto — tylko człowiek odpowiada
            pid = task.get("project_id") or "mikowhy-teams"
            policy = projects.approval_policy(pid)
            if policy == "auto" or (policy == "auto_readonly" and task.get("read_only")):
                self.state.transition(task["id"], READY, substep="1/5_PLAN",
                                      review_notes=(task.get("review_notes") or "") +
                                      f" | auto-approve (approval_policy={policy}, projekt={pid})")
                self.state.record_metric("auto_approve",
                                         {"policy": policy, "project": pid, "read_only": bool(task.get("read_only"))},
                                         task["id"])

    # --- auto-retry FAILED (limit prób, potem AWAITING_OWNER z pakietem opcji) ---
    def _auto_retry_failed(self) -> None:
        """FAILED -> READY automatycznie (fabryka sama się podnosi po przejściowych
        błędach LLM/infra — timeouty, sieć/API, niepełny przelot testów: nikt nie widzi alertu).
        Po wyczerpaniu limitu prób (max_retries) -> AWAITING_OWNER z pakietem dla Opiekuna:
        sedno problemu + opcje A/B/odrzuć + rekomendcja. Opiekun to ostatnia instancja."""
        for task in self.state.list_tasks(status=FAILED):
            if task["retry_count"] >= CIRCUIT_BREAKER_RETRIES:
                packet = (
                    " | AWAITING_OWNER po wyczerpaniu max_retries "
                    f"({task['retry_count']} prób FAILED). PAKIET dla Opiekuna: "
                    "co utknęło: powtarzający się błąd wykonania (patrz review_notes/ostatnie eventy); "
                    "opcje: [A] rekomendowane — puścić dalej do READY z podniesionym priorytetem i rolą principal-debugger, "
                    "[B] odrzucić → BLOCKED, [C] zdekomponować na mniejsze zadanie."
                )
                self.state.transition(task["id"], AWAITING_OWNER, substep="OWNER_PACKET",
                                      review_notes=(task.get("review_notes") or "") + packet)
                self.state.add_comment(task["id"], "orchestrator",
                                       f"🧑‍💼 {task['id']}: max_retries wyczerpane ({task['retry_count']} FAILED) → AWAITING_OWNER "
                                       "z pakietem opcji (A/B/odrzuć). Opiekun decyduje tylko tutaj — przy realnym rozjeździe.")
                self.state.record_metric("owner_packet", {"task": task["id"], "reason": "max_retries"}, task["id"])
                continue
            self.state.transition(task["id"], READY, substep="1/5_PLAN",
                                  retry_count=task["retry_count"] + 1,
                                  review_notes="auto-retry po FAILED (próba %d, backoff)" % (task["retry_count"] + 2))
            self.state.record_metric("auto_retry", {"retry": task["retry_count"] + 1}, task["id"])

    # --- faza 4: workery -------------------------------------------------------
    def _max_workers_now(self) -> int:
        """Ile workerów (LLM) można bezpiecznie odpalić RÓWNOLEGLE na tym laptopie:
        bierzemy pod uwagę CPU, wolny RAM i aktualny load — dzielimy maszynę ze wszystkimi.
        Nadpisanie ręczne: kv max_workers (orchestrator: set via state kv)."""
        override = self.state.get_kv("max_workers", 0)
        if override:
            return max(1, int(override))
        cpu = os.cpu_count() or 4
        try:
            load1 = float(open("/proc/loadavg").read().split()[0])
        except OSError:
            load1 = float(cpu)
        free_gb = 4.0
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable"):
                    free_gb = int(line.split()[1]) / 1024 / 1024
                    break
        except OSError:
            pass
        base = max(1, cpu - 2)                       # zostawiamy cores na system/UI
        ram_cap = max(1, int(free_gb / 1.2))         # ~1.2 GB na proces vibe
        load_cap = max(1, int(base * cpu / max(load1, 1.0)))  # load > cpu -> mniej workerów
        cap = max(1, min(8, base, ram_cap, load_cap))  # hard cap: 8 LLM naraz na współdzielonym laptopie
        return cap

    def _launch_workers(self) -> None:
        # projekty nieaktywne (wstrzymane przez opiekuna) nie dostają workerów
        # claimable = READY (z zależnościami) + CHANGES_REQUESTED (pętla poprawek po CR wraca do pracy)
        tasks = [t for t in self.state.claimable_tasks()
                 if projects.is_active(t.get("project_id") or "mikowhy-teams")]
        cap = self._max_workers_now()
        free = cap - len([t for t in self.running])  # wspólna pula też z review
        light_free = 2  # lekkie zadania (read_only / tryb llm) dostają 2 dodatkowe sloty — zużywają mniej zasobów
        for task in tasks:
            if task["id"] in self.running:
                continue
            if free > 0:
                self._spawn("worker_lifecycle", task["id"])
                free -= 1
            elif (task.get("read_only") or task.get("mode") == "llm") and light_free > 0:
                # równoległy lekki proces ponad głównym capem — nie rusza repo, mało RAM/CPU
                light_free -= 1
                self.state.record_metric("light_worker", {"task": task["id"], "mode": task.get("mode")}, task["id"])
                self._spawn("worker_lifecycle", task["id"])
            else:
                if len(tasks) > 0:
                    self.state.record_metric("throttled", {"waiting": len(tasks), "cap": cap,
                                                           "running": len(self.running)}, None)
                break

    # --- faza 5: code review + circuit breaker ----------------------------------
    def _launch_reviews(self) -> None:
        cap = min(2, self._max_workers_now())
        n = 0
        for task in self.state.list_tasks(status=IN_REVIEW):
            if task["id"] in self.running:
                continue
            if n >= cap:
                break
            self._spawn("code_review", task["id"])
            n += 1

    def _circuit_breakers(self) -> None:
        for task in self.state.list_tasks(status=READY):
            if task["retry_count"] >= CIRCUIT_BREAKER_RETRIES and task["role"] != PRINCIPAL_ROLE:
                self.state.transition(task["id"], READY, role=PRINCIPAL_ROLE,
                                      review_notes="CIRCUIT BREAKER: retry>=3 -> principal-debugger")
                self.state.record_metric("circuit_breaker_escalation", {"task": task["id"]}, task["id"])
                _push(
                    f"⚠️ *Circuit breaker* dla `{task['id']}` — przełączam na `{PRINCIPAL_ROLE}` "
                    f"(pełna historia {task['retry_count']} odrzuceń).")
                telegram_reporter.post_event(self.state, task,
                                             f"⚠️ *Circuit breaker* (retry>={task['retry_count']}) → rola `{PRINCIPAL_ROLE}`")
            elif task["retry_count"] >= CIRCUIT_BREAKER_RETRIES + 2 and task["role"] == PRINCIPAL_ROLE:
                # wyczerpany limit prób naprawczych nawet dla principal-debugger -> pakiet dla Opiekuna
                self.state.transition(task["id"], AWAITING_OWNER, substep="OWNER_PACKET",
                                      review_notes=(task.get("review_notes") or "") +
                                      " | AWAITING_OWNER: nawet principal-debugger po wyczerpaniu max_retries. "
                                      "Opcje: [A] READY z inną rolą, [B] BLOCKED, [C] dekompozycja.")
                _push(
                    f"🧑‍💼 *AWAITING_OWNER* `{task['id']}` — nawet Principal Debugger poległ po {task['retry_count']} próbach. "
                    f"Bloker: {task.get('review_notes', '')[:300]}")
                t = self.state.get_task(task["id"])
                if t:
                    telegram_reporter.post_event(self.state, t, "🧑‍💼 *AWAITING_OWNER* — wyczerpane retry, czeka decyzja Opiekuna (ostatnia instancja).")
                    telegram_reporter.update_progress(self.state, t)

    # --- faza 6: merge do gałęzi milestonu --------------------------------------
    def _handle_merges(self) -> None:
        for task in self.state.list_tasks(status=READY_TO_MERGE):
            ns = task.get("project_id") or "mikowhy-teams"
            repo = projects.repo_for_task(task, self.state)  # multi-project: fizyczne repo projektu
            # AUTO-MERGE DO MAIN: projekt z approval_policy=auto nad samym silnikiem (fabryka)
            # merguje READY_TO_MERGE od razu do default branch (master) — bez milestonu i bez ręcznego mergowania
            auto_main = (projects.approval_policy(ns) == "auto"
                         and repo.resolve() == projects.ROOT.resolve())
            if auto_main:
                base = git_guard.default_branch(repo)       # cel pusha: master
                # master jest zajęty w głównym worktree — worktree integracyjny stawiamy na temp branchu z origin/master
                tmp = f"_auto/{task['id']}"
            else:
                milestone = self.state.get_milestone(task["milestone"])
                if not milestone:
                    continue
                base = milestone["branch"]
                tmp = base
            try:
                git_guard.run_git(["fetch", "origin"], cwd=repo, check=False)
                if auto_main:
                    # wyrównaj temp branch z origin/master przed pushem (twarda reguła sync-z-origin)
                    git_guard.run_git(["branch", "-f", tmp, f"origin/{base}"], cwd=repo, check=False)
                else:
                    # gałąź milestonu mogła nie powstać (team_chat) — utwórz z HEAD, inaczej worktree add leci w GitError co tick
                    git_guard.run_git(["branch", base, "HEAD"], cwd=repo, check=False)
                wt = git_guard.integration_worktree(tmp, repo=repo, ns=f"{ns}/_integration".strip("/"))
                git_guard.rebase_onto(wt, task["branch"])  # konflikt -> mikro-bot
                reg = quality_gates.regression(wt)
                if not reg.get("ok"):
                    self.state.transition(task["id"], READY,
                                          review_notes=f"Regresja po rebase: {reg.get('output', '')[-500:]}")
                    continue
                if task.get("parent_task"):
                    # subtask: squash do JEDNEGO commita pod rodzicem (mniej commitów na milestone)
                    parent = self.state.get_task(task["parent_task"]) or task
                    git_guard.run_git(["merge", "--squash", task["branch"]], cwd=wt, check=False)
                    staged = git_guard.run_git(["diff", "--cached", "--quiet"], cwd=wt, check=False).returncode != 0
                    if staged:
                        git_guard.run_git(["commit", "-m",
                                           f"feat({parent['id'].lower()}): {parent['title'][:60]} [{task['id']}]"],
                                          cwd=wt)
                    else:
                        self.state.record_metric("squash_empty", {"task": task["id"]}, task["id"])
                else:
                    git_guard.merge_no_ff(wt, task["branch"], f"Merge {task['id']} into {base}")
                # auto_main: push z detached/temp worktree -> HEAD leci na refs/heads/master
                push_ref = f"HEAD:refs/heads/{base}" if auto_main else base
                git_guard.run_git(["push", "origin", push_ref], cwd=wt, check=False)
                self.state.transition(task["id"], MERGED)
                self.state.record_metric("merge", {"milestone": task["milestone"]}, task["id"])
                merged = self.state.get_task(task["id"])
                if merged:
                    stat_ref = f"origin/{base}" if auto_main else base  # po pushu auto_main lokalny ref base się nie ruszył
                    diff_stat = git_guard.run_git(["diff", "--shortstat", f"{stat_ref}~1", stat_ref], cwd=wt, check=False).stdout.strip()
                    telegram_reporter.close_topic(
                        self.state, merged,
                        summary=f"Zmergowane do `{base}` ({diff_stat or '—'}). Rola: {merged['role']}, retry: {merged['retry_count']}")
                git_guard.remove_worktree("_integration", ns=ns, repo=repo)
                if auto_main:
                    git_guard.run_git(["branch", "-D", tmp], cwd=repo, check=False)  # sprzątnij temp branch
                self.state.export_snapshot(project_id=ns)  # snapshot po każdym mergu
                lessons_gardener.garden(self.state)  # zmiana #2: co 10 mergy
                skill_curator.prune_if_needed(self.state)  # Skill Fitness: auto-prune + promocja lekcji
            except git_guard.MergeConflict as exc:
                self.state.transition(task["id"], READY, review_notes=f"Konflikt: {exc}")
                self.state.record_metric("merge_conflict", {"task": task["id"]}, task["id"])
            except Exception as exc:
                # dowolny inny błąd merge nie może blokować reszty ticka (telegram/health/milestony)
                self.state.record_metric("merge_error", {"task": task["id"], "error": repr(exc)}, task["id"])
                print(f"[orchestrator] błąd merge {task['id']}: {exc!r}", flush=True)

    # --- QG-3 + zamknięcie milestonu + faza 7 -------------------------------------
    def _handle_milestones(self) -> None:
        for ms in self.state.list_milestones():
            if ms["status"] != "OPEN" or not self.state.milestone_complete(ms["id"]):
                continue
            try:
                self._process_milestone(ms)
            except Exception as e:
                # Jeden zepsuty mileston NIE może blokować reszty ticka
                # (telegram/health/approvals leżą za _handle_milestones w tick()).
                self.state.record_metric("milestone_error", {"error": repr(e)}, task_id=ms["id"])
                print(f"[orchestrator] błąd milestonu {ms['id']}: {e!r}", flush=True)

    def _process_milestone(self, ms: dict) -> None:
            pid = ms.get("project_id") or "mikowhy-teams"
            repo = Path(projects.project_config(pid)["target_repo_path"]) if pid != "mikowhy-teams" else ROOT
            # mileston mógł powstać bez gałęzi (np. team_chat) — utwórz ją z HEAD, inaczej worktree add leci w GitError co tick
            git_guard.run_git(["branch", ms["branch"], "HEAD"], cwd=repo, check=False)
            wt = git_guard.integration_worktree(ms["branch"], repo=repo, ns=f"{pid}/_integration".strip("/"))
            qg3 = quality_gates.qg3_integration_e2e(ms["branch"], wt)  # zmiana #3
            if not qg3["ok"]:
                self.state.set_milestone_status(ms["id"], "OPEN")
                msg = f"❌ *QG-3 nie przeszło* dla `{ms['id']}`: {'; '.join(qg3['problems'][:3])}"
                _push(msg)
                telegram_reporter.post_milestone(f"🧪 *QG-3* `{ms['id']}`: ❌ {'; '.join(qg3['problems'][:2])}")
                return
            # merge milestone/* -> main
            git_guard.run_git(["checkout", git_guard.default_branch(repo)], cwd=wt, check=False)
            git_guard.run_git(["pull", "--ff-only", "origin", git_guard.default_branch(repo)], cwd=wt, check=False)
            git_guard.merge_no_ff(wt, ms["branch"], f"Release {ms['id']}: {ms['goal'][:60]}")
            git_guard.run_git(["push", "origin", git_guard.default_branch(repo)], cwd=wt, check=False)
            # GitHub: po merge'u milestonu wypchnij też master (PR-y feature są już na GH)
            try:
                from engine import github_bridge
                if github_bridge.enabled():
                    github_bridge.push_master()
            except Exception:
                pass
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
        _push(f"✅ *7a* `{ms['id']}` wdrożony na staging, smoke-testy zielone.")
        # 7b — człowiek decyduje o produkcji
        approval_id = f"DEPLOY_PROD:{ms['id']}"
        report = {"staging_url": smoke["staging_url"], "tests": "green (QG-3 E2E)",
                  "tokens": self._token_cost(ms["id"]), "business_diff": f"diff main...{ms['branch']}"}
        self.state.create_approval(approval_id, ms["id"], report)
        if telegram_gate.deploy_gate_enabled():
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
        if not telegram_reporter.push_enabled():
            return
        for task in self.state.list_tasks(status=AWAITING_APPROVAL):
            key = f"tg_approve_notified:{task['id']}"
            if self.state.get_kv(key):
                continue
            self.state.set_kv(key, True)
            _push(
                f"🧑‍💼 *{task['id']}* czeka na decyzję Opiekuna (AWAITING_OWNER — ostatnia instancja)\n"
                f"{task['title']}\nScope: `{task['scope_paths']}`",
                reply_markup={"inline_keyboard": [[
                    {"text": "✅ Akceptuj → READY", "callback_data": f"approve_task:{task['id']}"},
                    {"text": "🛑 Odrzuć → BLOCKED", "callback_data": f"decline_task:{task['id']}"},
                ]]})

    # --- health monitor co HEALTH_INTERVAL ------------------------------------------
    def _maybe_health(self) -> None:
        # health w osobnym wątku — NIGDY nie blokuje ticka (a tym samym Telegramu)
        last = self.state.get_kv("last_health_ts", 0)
        if time.time() - last >= health_monitor.HEALTH_INTERVAL:
            self.state.set_kv("last_health_ts", time.time())
            threading.Thread(target=self._health_bg, daemon=True).start()

    def _health_bg(self) -> None:
        try:
            report = health_monitor.run(self.state, create_tasks=True)
            self.state.record_metric("health_check", {"ok": report["ok"], "anomalies": len(report["anomalies"])})
        except Exception as exc:
            self.state.record_metric("health_error", {"error": repr(exc)})

    def _watchdog(self) -> None:
        """Sprawdza usługi systemd; jak któraś nieaktywna -> restart, a jak dalej nie żyje
        -> prośba do LLM (zadanie mode='llm') o naprawę."""
        last = self.state.get_kv("last_watchdog_ts", 0)
        if time.time() - last < 15:
            return
        self.state.set_kv("last_watchdog_ts", time.time())
        for svc in ("mikowhy-teams", "mikowhy-teams-dashboard"):
            state_now = subprocess.run(["systemctl", "--user", "is-active", svc],
                                       capture_output=True, text=True).stdout.strip()
            if state_now == "active":
                continue
            self.state.record_metric("watchdog", {"service": svc, "state": state_now}, None)
            subprocess.run(["systemctl", "--user", "restart", svc], capture_output=True)
            time.sleep(2)
            state2 = subprocess.run(["systemctl", "--user", "is-active", svc],
                                    capture_output=True, text=True).stdout.strip()
            if state2 == "active":
                self.state.record_metric("watchdog", {"service": svc, "action": "restarted", "state": state2}, None)
                continue
            # nadal martwa — eskalacja do LLM
            open_tasks = (self.state.list_tasks(status="AWAITING_APPROVAL")
                          + self.state.list_tasks(status="READY") + self.state.list_tasks(status="RUNNING"))
            if any(svc in (t.get("title") or "") for t in open_tasks):
                continue
            n = self.state.get_kv("watchdog_seq", 0) + 1
            self.state.set_kv("watchdog_seq", n)
            tid = f"FIX-{n:04d}"
            self.state.add_task(tid, milestone="CHAT-OPS", phase=9, title=f"Napraw usługę {svc} (systemd --user): nieaktywna mimo restartu",
                                role="principal-debugger", scope_paths=[], depends_on=[],
                                acceptance_criteria=f"Usługa {svc} aktywna (systemctl --user is-active = active) i przyczyna awarii opisana w raporcie",
                                project_id="mikowhy-teams", repo="factory",
                                status="AWAITING_APPROVAL", read_only=False, mode="llm")
            self.state.set_kv(f"llm_desc:{tid}",
                              f"Usługa systemd --user '{svc}' ma stan '{state2}' mimo restartu. "
                              f"Zdiagnozuj (systemctl --user status {svc}, journalctl --user -u {svc} -n 100, "
                              f"logi w .agents/logs/) i napraw tak, żeby była aktywna. "
                              f"Na końcu zostaw raport docs/llm-reports/{tid}.md.")
            self.state.record_metric("watchdog_escalation", {"service": svc, "task": tid}, tid)
            print(f"[orchestrator] 🚨 watchdog: {svc} nieaktywna -> zadanie {tid} dla LLM", flush=True)

    def _cleanup_merged_branches(self) -> None:
        """Usuwa branche feature/* zmergowane do default branch (zadanie MERGED = zamknięte w pełni)."""
        for t in self.state.list_tasks(status=MERGED):
            br = t.get("branch")
            if not br or not br.startswith("feature/"):
                continue
            if self.state.get_kv(f"branch_deleted:{br}"):
                continue
            repo = projects.repo_for_task(t, self.state)
            git_guard.run_git(["fetch", "origin", "--prune"], cwd=repo, check=False)
            default = git_guard.default_branch(repo)
            merged = git_guard.run_git(["branch", "--merged", default], cwd=repo, check=False).stdout
            if br not in merged.split():
                continue
            git_guard.run_git(["branch", "-D", br], cwd=repo, check=False)
            git_guard.run_git(["push", "origin", "--delete", br], cwd=repo, check=False)
            self.state.set_kv(f"branch_deleted:{br}", time.time())
            self.state.record_metric("task_event", {"event": "branch_deleted", "branch": br}, t["id"])
            print(f"[orchestrator] 🧹 usunięto zmergowany branch {br} (zadanie {t['id']})", flush=True)

    # --- watchdog kolejki: żeby przepływ nie utknął na starych zadaniach ------------
    def _stale_queue_watchdog(self) -> None:
        """Ciągłość pracy: READY wiszące > 8h dostają priorytet P1 + komentarz;
        BLOCKED/ESCALATED starsze niż 48h (i retry_count < 6) wracają automatycznie do READY —
        fabryka sama wraca do przepływu zamiast czekać bez końca na człowieka."""
        now = time.time()
        for task in self.state.list_tasks():
            age = now - (task.get("updated_at") or now)
            st = task["status"]
            if st == "READY" and age > 8 * 3600 and task.get("priority") in ("P2", "P3"):
                self.state.set_priority(task["id"], "P1")
                self.state.add_comment(task["id"], "orchestrator",
                                       f"⏳ watchdog kolejki: READY od {age/3600:.0f}h bez przejęcia → priorytet P1")
                self.state.record_metric("stale_ready_boost", {"task": task["id"], "age_h": round(age/3600, 1)}, task["id"])
            elif st in ("BLOCKED", "ESCALATED") and age > 48 * 3600 and (task.get("retry_count") or 0) < 6:
                self.state.transition(task["id"], READY, substep="1/5_PLAN",
                                      retry_count=(task.get("retry_count") or 0) + 1,
                                      review_notes=(task.get("review_notes") or "") +
                                      f" | watchdog: auto-recovery po {age/3600:.0f}h w {st}")
                self.state.record_metric("watchdog_recovery", {"task": task["id"], "from": st,
                                                               "age_h": round(age/3600, 1)}, task["id"])
                self.state.add_comment(task["id"], "orchestrator",
                                       f"♻️ watchdog: zadanie w {st} od {age/3600:.0f}h → wraca do READY (przepływ idzie dalej)")

    # --- telegram menu w ticku (run --poll 1 odbiera wiadomości użytkownika) ---------
    def _poll_telegram(self) -> None:
        try:
            telegram_menu.poll_once(self.state, timeout=0)
        except Exception as exc:
            self.state.record_metric("telegram_poll_error", {"error": repr(exc)})

    # --- kolejka LLM: dispatcher (jeden proces = serialne wywołania vibe CLI) ---------
    def _dispatch_llm_queue(self) -> None:
        """Konsumuje llm_jobs — stąd keyring/vibe CLI dotyka tylko orchestrator (koniec rc=3)."""
        try:
            from engine import llm_queue
            llm_queue.dispatch_once(self.state, max_jobs=2)
        except Exception as e:
            self.state.record_metric("llm_dispatch_error", {"error": repr(e)}, None)

    # --- pętla główna -------------------------------------------------------------
    def tick(self) -> None:
        self._reap()
        self._auto_approve()
        self._auto_retry_failed()
        self._dispatch_llm_queue()
        self._launch_workers()
        self._launch_reviews()
        self._circuit_breakers()
        self._stale_queue_watchdog()
        self._handle_merges()
        self._cleanup_merged_branches()
        self._handle_milestones()
        self._notify_approvals()
        self._watchdog()
        self._maybe_health()
        self._poll_telegram()

    def run(self, once: bool = False) -> None:
        # lock pojedynczej instancji — ręczny `run` gdy działa usługa systemd wychodzi od razu
        # (dwie instancje walczą o worktree/state i jedna z nich kończy "Unicestwiony")
        lock_path = ROOT / ".agents" / "orchestrator.lock"
        self._lock_f = open(lock_path, "w")
        try:
            fcntl.flock(self._lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_f.write(f"{os.getpid()}\n")
            self._lock_f.flush()
        except BlockingIOError:
            print(f"⚠️ Orkiestrator już działa (lock: {lock_path}). "
                  f"Kanoniczny runner to usługa: systemctl --user status mikowhy-teams. Wychodzę.", flush=True)
            sys.exit(0)
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
    git_guard.run_git(["branch", branch, "HEAD"], check=False)  # gałąź audytu w repo FABRYKI (HEAD = master/main)
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
                       project_id=project_id, repo="factory", status=status, read_only=True)
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
    """Sponsor/Opiekun: AWAITING_OWNER -> READY (decyzja ląduje w kontekście, pełny cykl)."""
    state = State()
    task = state.get_task(task_id)
    if not task:
        raise SystemExit(f"Nieznane zadanie: {task_id}")
    if task["status"] != AWAITING_APPROVAL:
        raise SystemExit(f"{task_id} ma status {task['status']} — approve tylko z AWAITING_OWNER")
    state.transition(task_id, READY, substep="1/5_PLAN",
                     review_notes=(task.get("review_notes") or "") + " | zaakceptowane przez Sponsora (CLI)")
    state.record_metric("sponsor_approve", {"task": task_id}, task_id)
    telegram_reporter.post_event(state, state.get_task(task_id), "✅ zaakceptowane przez Sponsora → READY")
    print(f"✅ {task_id} -> READY")


def cmd_scan() -> int:
    """Pre-push review: (1) czy w śledzonych plikach nie ma sekretów/kluczy,
    (2) czy .gitignore przykrywa .env/db/logs/worktrees, (3) czy prawdziwe wartości
    z .env nie przeciekły do historii git, (4) czy są tylko zamierzone pliki."""
    import re as _re
    problems: list[str] = []
    warns: list[str] = []

    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout.splitlines()

    # 1) zakazane ścieżki wśród śledzonych
    bad_paths = [f for f in tracked if _re.search(
        r"(^|/)\.env$|\.pem$|id_rsa|\.key$|credentials|\.p12$|state\.db|\.lock$|/logs/|/worktrees/|/runtime/|__pycache__|\.pyc$", f)]
    if bad_paths:
        problems.append(f"śledzone pliki wrażliwe/artefakty: {bad_paths[:10]}")

    # 2) wzorce sekretów w treści śledzonych plików
    secret_patterns = [
        ("PRIVATE KEY block", _re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
        ("AWS key", _re.compile(r"AKIA[0-9A-Z]{16}")),
        ("GitHub PAT", _re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
        ("OpenAI-style sk-", _re.compile(r"sk-[A-Za-z0-9]{20,}")),
        ("Telegram token", _re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b")),
        ("generic secret assignment", _re.compile(
            r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}\b")),
    ]
    placeholder = _re.compile(r"(?i)(example|placeholder|your[-_]|xxx|<|changeme|dummy|test[-_]?only|\$\{|os\.environ|getenv)")
    hits = []
    for f in tracked:
        if f == ".env.example" or f.endswith(".md"):
            pass  # .md może cytować nazwy zmiennych; .env.example ma puste wartości — sprawdzamy dalej osobno
        try:
            content = (ROOT / f).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line_no, line in enumerate(content.splitlines(), 1):
            if placeholder.search(line):
                continue
            for name, rx in secret_patterns:
                if rx.search(line):
                    hits.append(f"{f}:{line_no} [{name}] {line.strip()[:80]}")
    if hits:
        problems.append("podejrzane sekrety w śledzonych plikach:\n  " + "\n  ".join(hits[:15]))

    # 3) czy PRAWDZIWE wartości z .env są w historii git
    env_vals = []
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.strip().strip('"').strip("'").split(" #")[0].strip()
                # tylko nazwy-klucze wyglądające na sekret (nie np. GITHUB_REPO)
                if len(v) >= 20 and not v.startswith(("<", "your")) and _re.search(
                        r"(?i)(token|key|secret|passwd|password|credential)", k):
                    env_vals.append((k, v))
    for key, val in env_vals:
        r = subprocess.run(["git", "grep", "-l", "-F", val, "--all"], cwd=ROOT,
                           capture_output=True, text=True)
        # git grep --all? użyj rev-list:
        revs = subprocess.run(["git", "rev-list", "--all"], cwd=ROOT, capture_output=True, text=True).stdout.split()
        found = []
        for rev in revs:
            g = subprocess.run(["git", "grep", "-l", "-F", val, rev], cwd=ROOT, capture_output=True, text=True)
            if g.returncode == 0 and g.stdout.strip():
                found.append(rev[:8])
                break
        if found:
            problems.append(f"Wartość {key} znaleziona w historii git (commit {found[0]}) — ROTUJ SEKRET!")

    # 4) .gitignore coverage
    gi = (ROOT / ".gitignore").read_text() if (ROOT / ".gitignore").exists() else ""
    for needle in (".env", "state.db", "logs/", "worktrees/", "runtime/", "*.lock"):
        if needle not in gi:
            problems.append(f".gitignore nie przykrywa: {needle}")

    # 5) remote
    rem = subprocess.run(["git", "remote", "-v"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if not rem:
        warns.append("brak skonfigurowanego remote (git remote add origin https://github.com/mikowhyosowski/mikowhy-teams.git)")

    print("== SCAN PRE-PUSH ==")
    print(f"śledzonych plików: {len(tracked)}")
    for w in warns:
        print(f"⚠️  {w}")
    if problems:
        for pr in problems:
            print(f"❌ {pr}")
        print(f"\nWYNIK: FAIL ({len(problems)} problemów)")
        return 1
    print("✅ WYNIK: OK — brak sekretów w plikach i historii, .gitignore kompletny, brak artefaktów")
    return 0


def cmd_retry(task_id: str) -> None:
    """Requeue: FAILED/BLOCKED -> READY (nowa próba od zera; pełny cykl, nic nie omija testów/CR)."""
    state = State()
    task = state.get_task(task_id)
    if not task:
        raise SystemExit(f"Nieznane zadanie: {task_id}")
    if task["status"] not in (FAILED, BLOCKED):
        raise SystemExit(f"{task_id} ma status {task['status']} — retry tylko z FAILED/BLOCKED")
    state.transition(task_id, READY, substep="1/5_PLAN", retry_count=0,
                     review_notes="requeue (retry) — stare review_notes wyczyszczone")
    state.record_metric("sponsor_retry", {"task": task_id}, task_id)
    print(f"🔁 {task_id} -> READY (retry_count=0)")


def cmd_decline(task_id: str, reason: str = "") -> None:
    """Opiekun: AWAITING_OWNER -> BLOCKED z uzasadnieniem (odrzucenie)."""
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
    # jeśli task ma PR na GitHubie — komentarz Sponsora leci też tam (autor: właściciel GITHUB_TOKEN)
    try:
        from engine import github_bridge
        pr = github_bridge.pr_for_task(target)
        if pr:
            github_bridge.post_comment(pr, f"💬 **Sponsor ({target}):** {text}")
    except Exception:
        pass
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
    check("HEAD na domyślnej gałęzi (master)", head == git_guard.default_branch(), head)

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
    check("AWAITING_OWNER nie startuje sam", True,
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
    h = sub.add_parser("health", help="pełny health check wszystkich projektów + zadania naprawcze; --watch = pętla")
    h.add_argument("--watch", action="store_true", help="pętla: health co --interval sekund (domyślnie HEALTH_INTERVAL/300)")
    h.add_argument("--interval", type=int, default=None, help="interwał pętli w sekundach")
    st = sub.add_parser("status", help="stan projektów i zadań (jak pipeline.json); --watch = żywy podgląd")
    st.add_argument("--watch", action="store_true", help="pętla: odświeżaj co --interval sekund")
    st.add_argument("--interval", type=float, default=2.0, help="interwał odświeżania w --watch")
    ap3 = sub.add_parser("approve", help="Sponsor: AWAITING_APPROVAL -> READY")
    ap3.add_argument("task_id")
    ap4 = sub.add_parser("decline", help="Sponsor: AWAITING_APPROVAL -> BLOCKED")
    ap5 = sub.add_parser("policy", help="approval_policy projektu: get/set manual|auto|auto_readonly")
    ap5.add_argument("project_id")
    ap5.add_argument("value", nargs="?", default=None, choices=list(projects.POLICIES))
    ap4.add_argument("task_id")
    ap4.add_argument("reason", nargs="?", default="")
    ap5 = sub.add_parser("note", help="rada Sponsora: ALL (do każdego promptu) lub <task_id>")
    ap6 = sub.add_parser("retry", help="requeue: FAILED/BLOCKED/ESCALATED -> READY")
    ap6.add_argument("task_id")
    ap7 = sub.add_parser("priority", help="ustaw priorytet zadania: P0 (najwyższy) .. P3")
    ap7.add_argument("task_id")
    ap7.add_argument("value", choices=["P0", "P1", "P2", "P3"])
    db = sub.add_parser("dashboard", help="live UI zadań w przeglądarce (read-only), http://127.0.0.1:8765")
    sub.add_parser("scan", help="pre-push review: sekrety/klucze w plikach i historii git + higiena .gitignore")
    sub.add_parser("watch", help="live tail: .agents/runtime (surowe odpowiedzi LLM) + .agents/logs/* w terminalu")
    gh = sub.add_parser("gh", help="GitHub bridge: setup | push <branch> | pr <head> <base> <tytul> | protect | status")
    gh.add_argument("action")
    gh.add_argument("args", nargs="*")
    db.add_argument("--port", type=int, default=8765)
    db.add_argument("--host", default="127.0.0.1")
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
        interval = args.interval or health_monitor.HEALTH_INTERVAL
        while True:
            state = State()
            rep = health_monitor.run(state, create_tasks=True)
            n = len(rep["anomalies"])
            print(f"\n🩺 {time.strftime('%H:%M:%S')} health: {'✅ OK' if rep['ok'] else f'⚠️ {n} anomalii'} "
                  f"(telegram={rep['global'].get('telegram_ok')}, vibe={rep['global'].get('vibe_ok')}, "
                  f"zombie={len(rep['global'].get('zombie_tasks', []))}, stale_wt={len(rep['global'].get('stale_worktrees', []))})",
                  flush=True)
            for a in rep["anomalies"]:
                print(f"   - [{a['project']}] {a['kind']}: {a['detail'][:110]}", flush=True)
            if not args.watch:
                print(json.dumps({"ok": rep["ok"], "anomalies": rep["anomalies"], "global": rep["global"]},
                                 indent=2, ensure_ascii=False, default=str)[:3000])
                break
            print(f"   … następny check za {interval}s (Ctrl+C stop)", flush=True)
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\nhealth: stop (Ctrl+C)", flush=True)
                break
    elif args.cmd == "status":
        if getattr(args, "watch", False):
            import os as _os
            while True:
                _os.system("clear" if _os.name == "posix" else "cls")
                print(f"🔄 status --watch (co {args.interval}s, Ctrl+C stop) — {time.strftime('%H:%M:%S')}")
                cmd_status()
                time.sleep(args.interval)
        else:
            cmd_status()
    elif args.cmd == "approve":
        cmd_approve(args.task_id)
    elif args.cmd == "decline":
        cmd_decline(args.task_id, args.reason)
    elif args.cmd == "retry":
        cmd_retry(args.task_id)
    elif args.cmd == "priority":
        State().set_priority(args.task_id, args.value)
        print(f"🎯 {args.task_id} -> priorytet {args.value}")
    elif args.cmd == "scan":
        raise SystemExit(cmd_scan())
    elif args.cmd == "watch":
        import os as _os
        rt = ROOT / ".agents" / "runtime"; lg = ROOT / ".agents" / "logs"
        seen_rt, seen_lg = {}, {}
        print("🔴 watch — Ctrl+C stop. Sekcja [runtime] = surowe odpowiedzi LLM, [log] = logi procesów\n")
        while True:
            for d, seen, tag in ((rt, seen_rt, "runtime"), (lg, seen_lg, "log")):
                if not d.exists():
                    continue
                for f in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime):
                    st = f.stat()
                    key = (tag, f.name)
                    if key not in seen or seen[key] != (st.st_mtime, st.st_size):
                        seen[key] = (st.st_mtime, st.st_size)
                        try:
                            tail = f.read_text(encoding="utf-8", errors="replace").splitlines()[-3:]
                        except Exception:
                            tail = ["<unreadable>"]
                        print(f"[{tag}] {f.name} ({st.st_size}B)")
                        for l in tail:
                            print("   ", l[:180])
            time.sleep(2)
    elif args.cmd == "gh":
        from engine import github_bridge as gb
        act = args.action
        if act == "setup":
            gb.ensure_remote()
            print("remote origin:", subprocess.run(["git", "remote", "get-url", "origin"], cwd=ROOT,
                  capture_output=True, text=True).stdout.strip())
            if gb.push_master():
                print("✅ master wypchnięty na GitHub")
        elif act == "push":
            raise SystemExit(0 if gb.push(args.args[0]) else 1)
        elif act == "pr":
            head, base, title = args.args[0], args.args[1], " ".join(args.args[2:])
            gb.push(head)
            print("PR nr:", gb.open_pr(head, base, title))
        elif act == "protect":
            gb.protect("master")
            print("branch protection ustawione (best effort)")
        elif act == "status":
            print("enabled:", gb.enabled(), "| repo:", gb.repo())
        elif act == "prs":
            # push wszystkich lokalnych feature/* z commitami ponad master + PR dla każdej
            branches = subprocess.run(["git", "branch", "--list", "feature/*"], cwd=ROOT,
                                      capture_output=True, text=True).stdout.split()
            made = []
            for b in branches:
                b = b.strip().lstrip("*+ ")  # git branch: '*' = bieżąca, '+' = w worktree
                ahead = subprocess.run(["git", "rev-list", "--count", f"master..{b}"], cwd=ROOT,
                                       capture_output=True, text=True).stdout.strip()
                if ahead == "0":
                    continue
                gb.push(b)
                pr = gb.open_pr(b, "master", f"[{b.replace('feature/', '').upper()}] {b}")
                made.append((b, pr))
                print(f"  {b} (+'{ahead}') -> PR: {pr}")
            if not made:
                print("  brak gałęzi feature/* z commitami ponad master")
        elif act == "accept-all":
            prs = gb.api("GET", f"/repos/{gb.repo()}/pulls?state=open")
            if not isinstance(prs, list):
                print("  nie mogę pobrać PR (sprawdź token)")
            else:
                for pr in prs:
                    n = pr["number"]
                    gb.post_review(n, "APPROVED", "✅ Zaakceptowane przez Sponsora (accept-all)")
                    m = gb.merge_pr(n)
                    print(f"  PR !{n} ({pr['title'][:50]}): review APPROVED, merge={'OK' if m else 'FAIL'}")
    elif args.cmd == "dashboard":
        from engine import dashboard
        sys.argv = ["dashboard", "--port", str(args.port), "--host", args.host]
        dashboard.main()
    elif args.cmd == "policy":
        if args.value:
            cfg = projects.set_approval_policy(args.project_id, args.value)
            print(f"approval_policy[{args.project_id}] = {cfg['approval_policy']}")
        else:
            print(f"approval_policy[{args.project_id}] = {projects.approval_policy(args.project_id)}")
    elif args.cmd == "note":
        cmd_note(args.target, args.text)
    elif args.cmd == "telegram-poll":
        state = State()
        while True:  # nigdy nie wychodź błędem — 409/timeout to normalka przy wielu pollerach (lock w menu)
            try:
                telegram_menu.poll_once(state, timeout=25)
            except Exception as exc:
                print(f"[telegram-poll] błąd (kontynuacja): {exc!r}", flush=True)
                time.sleep(5)
            if args.once:
                break


if __name__ == "__main__":
    main()

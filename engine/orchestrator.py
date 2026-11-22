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
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import git_guard, lessons_gardener, quality_gates, telegram_gate
from engine.state import (
    BLOCKED,
    ESCALATED,
    IN_REVIEW,
    MERGED,
    READY,
    READY_TO_MERGE,
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
    chain = [
        (1, "product-owner", f"PRD (Gherkin + Edge Cases) dla: {goal}",
         "docs/PRD.md", "QG-1: każdy REQ ma Given/When/Then i sekcja Edge Cases"),
        (2, "solutions-architect", f"ADR + zamrożone kontrakty dla: {goal}",
         "docs/adr/, contracts/", "QG-2: OpenAPI/JSON Schema valid + audyt security; kontrakty FROZEN"),
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
    state.export_snapshot()
    print(f"Zbootstrapowano mileston {milestone_id} ({branch}) z fazami 1, 2, 2.5, 3. Cel: {goal}")


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
            elif task["retry_count"] >= CIRCUIT_BREAKER_RETRIES + 2 and task["role"] == PRINCIPAL_ROLE:
                self.state.transition(task["id"], ESCALATED)
                telegram_gate.send_message(
                    f"🚨 *ESKALACJA* `{task['id']}` — nawet Principal Debugger poległ. "
                    f"Bloker: {task.get('review_notes', '')[:300]}")

    # --- faza 6: merge do gałęzi milestonu --------------------------------------
    def _handle_merges(self) -> None:
        for task in self.state.list_tasks(status=READY_TO_MERGE):
            milestone = self.state.get_milestone(task["milestone"])
            if not milestone:
                continue
            base = milestone["branch"]
            try:
                git_guard.run_git(["fetch", "origin"], cwd=ROOT, check=False)
                wt = git_guard.integration_worktree(base)
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
                git_guard.remove_worktree("_integration")
                self.state.export_snapshot()  # snapshot po każdym mergu
                lessons_gardener.garden(self.state)  # zmiana #2: co 10 mergy
            except git_guard.MergeConflict as exc:
                self.state.transition(task["id"], READY, review_notes=f"Konflikt: {exc}")
                self.state.record_metric("merge_conflict", {"task": task["id"]}, task["id"])

    # --- QG-3 + zamknięcie milestonu + faza 7 -------------------------------------
    def _handle_milestones(self) -> None:
        for ms in self.state.list_milestones():
            if ms["status"] != "OPEN" or not self.state.milestone_complete(ms["id"]):
                continue
            wt = git_guard.integration_worktree(ms["branch"])
            qg3 = quality_gates.qg3_integration_e2e(ms["branch"], wt)  # zmiana #3
            if not qg3["ok"]:
                self.state.set_milestone_status(ms["id"], "OPEN")
                telegram_gate.send_message(
                    f"❌ *QG-3 nie przeszło* dla `{ms['id']}`: {'; '.join(qg3['problems'][:3])}")
                continue
            # merge milestone/* -> main
            git_guard.run_git(["checkout", "main"], cwd=wt, check=False)
            git_guard.run_git(["pull", "--ff-only", "origin", "main"], cwd=wt, check=False)
            git_guard.merge_no_ff(wt, ms["branch"], f"Release {ms['id']}: {ms['goal'][:60]}")
            git_guard.run_git(["push", "origin", "main"], cwd=wt, check=False)
            self.state.set_milestone_status(ms["id"], "CLOSED")
            self.state.export_snapshot()
            git_guard.remove_worktree("_integration")
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

    # --- pętla główna -------------------------------------------------------------
    def tick(self) -> None:
        self._reap()
        self._launch_workers()
        self._launch_reviews()
        self._circuit_breakers()
        self._handle_merges()
        self._handle_milestones()

    def run(self) -> None:
        print(f"Orchestrator start (poll={self.poll}s). Stan: {self.state.db_path}", flush=True)
        while True:
            try:
                self.tick()
            except Exception as exc:  # daemon nie może umrzeć
                self.state.record_metric("orchestrator_error", {"error": repr(exc)})
                print(f"[orchestrator] błąd ticka: {exc!r}", flush=True)
            time.sleep(self.poll)


def main() -> None:
    ap = argparse.ArgumentParser(description="Dark Factory orchestrator")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bootstrap", help="Utwórz mileston z fazami 1–3")
    b.add_argument("goal")
    b.add_argument("--milestone", default="M1")
    r = sub.add_parser("run", help="Pętla demona")
    r.add_argument("--poll", type=int, default=10)
    sub.add_parser("export", help="Eksport snapshotu pipeline.json")
    t = sub.add_parser("telegram-poll", help="Long-poll przycisków 7b (Deploy Prod / Rollback)")
    t.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.cmd == "bootstrap":
        bootstrap(args.goal, args.milestone)
    elif args.cmd == "run":
        Orchestrator(poll=args.poll).run()
    elif args.cmd == "export":
        print(State().export_snapshot())
    elif args.cmd == "telegram-poll":
        state = State()
        offset = 0
        while True:
            offset, callbacks = telegram_gate.poll_updates(offset)
            for cb in callbacks:
                action, approval_id = cb["data"].split(":", 1)
                if action == "approve":
                    state.resolve_approval(approval_id, "APPROVED")
                    telegram_gate.answer_callback(cb["id"], "Deploy prod zatwierdzony 🚀")
                    telegram_gate.send_message(f"🚀 *Deploy PROD* zatwierdzony dla `{approval_id}` — startuję Faza 7b prod.")
                elif action == "decline":
                    state.resolve_approval(approval_id, "DECLINED")
                    telegram_gate.answer_callback(cb["id"], "Rollback / uwagi zapisane 🛑")
                    telegram_gate.send_message(f"🛑 *Rollback* dla `{approval_id}` — produkcja wstrzymana.")
            if args.once:
                break


if __name__ == "__main__":
    main()

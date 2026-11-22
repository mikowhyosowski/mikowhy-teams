"""Niezależny Code Review — osobny proces, czysty kontekst (zero historii
z sesji deweloperskiej = brak efektu potwierdzenia). Widzi tylko git diff,
acceptance_criteria, wyniki testów i anti-slop.md. Twardy zakaz edycji kodu:
zwraca wyłącznie werdykt JSON.

REJECTED -> lessons-learned (zmiana #2: per-stack) + retry_count++
retry_count >= 3 -> circuit breaker: rola principal-debugger (orchestrator)
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

from engine import git_guard, lessons_gardener, projects, skill_compiler, telegram_reporter
from engine.llm_gateway import get_gateway
from engine.state import IN_REVIEW, READY, READY_TO_MERGE, State

CIRCUIT_BREAKER_RETRIES = 3


def review(task_id: str) -> int:
    state = State()
    task = state.get_task(task_id)
    if task is None or task["status"] != IN_REVIEW:
        return 1
    milestone = state.get_milestone(task["milestone"])
    base = milestone["branch"] if milestone else git_guard.default_branch()
    repo = projects.repo_for_task(task, state)  # diff względem fizycznego repo projektu

    diff = git_guard.run_git(["diff", f"{base}...{task['branch']}"], cwd=repo, check=False).stdout
    tests = state.get_kv(f"tests:{task_id}", {})
    payload = {
        "diff": diff[-60000:],
        "acceptance_criteria": task["acceptance_criteria"],
        "test_results": tests,
        "retry_count": task["retry_count"],
    }
    gw = get_gateway()
    system = skill_compiler.compile_system_prompt("code-reviewer", stack=None, task=task)
    verdict = gw.complete_json(
        system=system, user=json.dumps(payload, ensure_ascii=False),
        step="CODE_REVIEW", model=skill_compiler.model_for_role("code-reviewer", kind="strong"),
    )

    if verdict.get("verdict") == "APPROVED":
        state.transition(task_id, READY_TO_MERGE, review_notes=json.dumps(verdict, ensure_ascii=False))
        state.record_metric("cr_approved", {"verdict": verdict}, task_id)
        skill_compiler.promote_role_if_probation(task["role"])  # zmiana #5
        skill_compiler.record_skill_usage(task, "APPROVED")     # Skill Fitness Score
        t = state.get_task(task_id)
        if t:
            telegram_reporter.post_event(state, t, f"✅ CR *APPROVED* za {task['retry_count'] + 1}. próbą → READY_TO_MERGE")
            telegram_reporter.update_progress(state, t)
        return 0

    # REJECTED — system sam się uczy (zmiana #2: lekcja per-stack, nie globalnie)
    notes = verdict.get("notes", [])
    state.transition(task_id, READY, retry_count=task["retry_count"] + 1,
                     review_notes=json.dumps(verdict, ensure_ascii=False))
    t = state.get_task(task_id)
    if t:
        reasons = "; ".join((n.get("rule", str(n)) if isinstance(n, dict) else str(n)) for n in notes)[:500]
        telegram_reporter.post_event(state, t, f"🔍 CR *REJECTED* (próba {task['retry_count'] + 1}): {reasons}")
        telegram_reporter.update_progress(state, t)
    for note in notes:
        rule = note.get("rule") if isinstance(note, dict) else str(note)
        if rule:
            lessons_gardener.add_lesson(stack="python-3.14-async", rule=rule, task_id=task_id)
    state.record_metric("cr_reject", {"retry": task["retry_count"] + 1, "notes": notes}, task_id)
    skill_compiler.record_skill_usage(task, "REJECTED")         # Skill Fitness Score

    if task["retry_count"] + 1 >= CIRCUIT_BREAKER_RETRIES:
        # Circuit breaker: orchestrator podmieni rolę na principal-debugger
        state.record_metric("circuit_breaker", {"task": task_id}, task_id)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Niezależny Code Review zadania")
    ap.add_argument("task_id")
    args = ap.parse_args()
    raise SystemExit(review(args.task_id))


if __name__ == "__main__":
    main()

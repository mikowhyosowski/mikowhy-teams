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
from engine.state import AWAITING_OWNER, CHANGES_REQUESTED, IN_REVIEW, READY_TO_MERGE, State

CIRCUIT_BREAKER_RETRIES = 3  # max prób pętli CR -> po limicie AWAITING_OWNER (pakiet dla Opiekuna)


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

    # GitHub: werdykt CR jako oficjalny review PR (jeśli task ma PR)
    try:
        from engine import github_bridge
        pr = github_bridge.pr_for_task(task_id)
        if pr:
            v = verdict.get("verdict")
            notes_md = "\n".join(f"- {n.get('rule', n) if isinstance(n, dict) else n}" for n in verdict.get("notes", [])[:10])
            body = (f"## Code Review — {v} (próba {task['retry_count'] + 1})\n\n"
                    + (f"**Uwagi:**\n{notes_md}" if notes_md else "Bez uwag."))
            github_bridge.post_review(pr, "APPROVED" if v == "APPROVED" else "CHANGES_REQUESTED", body)
    except Exception as exc:
        print(f"[github] review post skipped: {exc!r}", flush=True)

    if verdict.get("verdict") == "APPROVED":
        state.add_comment(task_id, "code-reviewer", f"✅ CR APPROVED (próba {task['retry_count'] + 1}) → READY_TO_MERGE. Proszę o merge.")
        state.transition(task_id, READY_TO_MERGE, review_notes=json.dumps(verdict, ensure_ascii=False))
        state.record_metric("cr_approved", {"verdict": verdict}, task_id)
        skill_compiler.promote_role_if_probation(task["role"])  # zmiana #5
        skill_compiler.record_skill_usage(task, "APPROVED")     # Skill Fitness Score
        t = state.get_task(task_id)
        if t:
            telegram_reporter.post_event(state, t, f"✅ CR *APPROVED* za {task['retry_count'] + 1}. próbą → READY_TO_MERGE")
            telegram_reporter.update_progress(state, t)
        return 0

    # REJECTED — pętla naprawcza: CHANGES_REQUESTED -> worker wraca do RUNNING z listą poprawek.
    # Opiekun NIE jest angażowany w zwykłe poprawki kodu (samoregulująca się linia produkcyjna).
    notes = verdict.get("notes", [])
    reasons = "; ".join((n.get("rule", str(n)) if isinstance(n, dict) else str(n)) for n in notes)
    state.add_comment(task_id, "code-reviewer",
                      f"🔍 CR REJECTED (próba {task['retry_count'] + 1}) → CHANGES_REQUESTED: " + reasons[:300])
    if task["retry_count"] + 1 >= CIRCUIT_BREAKER_RETRIES:
        # limit prób pętli CR wyczerpany -> pakiet dla Opiekuna (ostatnia instancja), nie rutynowa eskalacja
        state.transition(task_id, AWAITING_OWNER, substep="OWNER_PACKET", retry_count=task["retry_count"] + 1,
                         review_notes=json.dumps(verdict, ensure_ascii=False) +
                         " | AWAITING_OWNER: CR odrzucił po wyczerpaniu max_retries. "
                         "Opcje: [A] READY z rolą principal-debugger, [B] BLOCKED, [C] dekompozycja.")
        state.record_metric("circuit_breaker_owner", {"task": task_id}, task_id)
        t = state.get_task(task_id)
        if t:
            telegram_reporter.post_event(state, t,
                                         f"🧑‍💼 CR odrzucił {task['retry_count'] + 1}. próbę z rzędu → AWAITING_OWNER (pakiet A/B/odrzuć)")
            telegram_reporter.update_progress(state, t)
    else:
        state.transition(task_id, CHANGES_REQUESTED, retry_count=task["retry_count"] + 1,
                         review_notes=json.dumps(verdict, ensure_ascii=False))
        t = state.get_task(task_id)
        if t:
            telegram_reporter.post_event(state, t, f"🛠️ CR *CHANGES_REQUESTED* (próba {task['retry_count'] + 1}): {reasons[:500]}")
            telegram_reporter.update_progress(state, t)
    for note in notes:
        rule = note.get("rule") if isinstance(note, dict) else str(note)
        if rule:
            lessons_gardener.add_lesson(stack="python-3.14-async", rule=rule, task_id=task_id)
    state.record_metric("cr_reject", {"retry": task["retry_count"] + 1, "notes": notes}, task_id)
    skill_compiler.record_skill_usage(task, "REJECTED")         # Skill Fitness Score
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Niezależny Code Review zadania")
    ap.add_argument("task_id")
    args = ap.parse_args()
    raise SystemExit(review(args.task_id))


if __name__ == "__main__":
    main()

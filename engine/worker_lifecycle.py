"""Worker lifecycle — deterministyczny wrapper prowadzący bota LLM za rękę
przez 5 izolowanych mikro-etapów (każdy ze świeżym kontekstem).

Bot LLM NIGDY nie wywołuje git ani nie zapisuje stanu — robi to ten moduł.

Zmiana #7: SPLIT_TASK tylko do max_split_depth=1; diff > MAX_DIFF_LINES
odrzucany przez git_guard jeszcze przed Code Review.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import git_guard, projects, quality_gates, skill_compiler, telegram_reporter
from engine.llm_gateway import get_gateway
from engine.state import (
    FAILED,
    IN_REVIEW,
    READY,
    State,
)

MAX_LOCAL_FIXES = 3        # Krok 4b — lokalne próby naprawy
MAX_SPLIT_DEPTH = 1        # zmiana #7 — twardy limit głębokości podziału
STEP_TIMEOUT_HINT = {"1/5_PLAN": 45, "2/5_IMPL": 120, "3/5_TEST": 90, "4/5_QG": 90}


def _log(task_id: str, msg: str) -> None:
    print(f"[{task_id}] {msg}", flush=True)


def step0_prepare(state: State, task: dict) -> Path:
    """Krok 0 (Python): izolowany worktree + gałąź feature/<task>.
    Repo worktree = target_repo_path projektu (lub katalog silnika gdy task['repo']=='factory')."""
    milestone = state.get_milestone(task["milestone"])
    base = milestone["branch"] if milestone else git_guard.default_branch()
    branch = task["branch"] or f"feature/{task['id'].lower()}"
    repo = projects.repo_for_task(task, state)          # multi-project: fizyczne repo na dysku
    # fallback: gdy gałąź milestonu jeszcze nie istnieje (np. FIX z health) — leć z głównej gałęzi
    if base != git_guard.default_branch(repo):
        chk = git_guard.run_git(["rev-parse", "--verify", "--quiet", base], cwd=repo, check=False)
        if chk.returncode != 0:
            _log(task["id"], f"base {base} nie istnieje — fallback do {git_guard.default_branch(repo)}")
            base = git_guard.default_branch(repo)
    git_guard.assert_writable(repo)                      # STRICT READ-ONLY guard
    ns = task.get("project_id") or "mikowhy-teams"
    worktree = git_guard.create_worktree(task["id"], base, branch, repo=repo, ns=ns)
    state.transition(task["id"], "RUNNING", substep="1/5_PLAN", branch=branch, worktree=str(worktree))
    _log(task["id"], f"worktree={worktree} repo={repo} base={base} branch={branch}")
    return worktree


def _llm_step(task: dict, step: str, user_payload: str, model: str) -> dict:
    gw = get_gateway()
    system = skill_compiler.compile_system_prompt(task["role"], stack=None, task=task)
    return gw.complete_json(system=system, user=user_payload, step=step, model=model)


def step1_plan(state: State, task: dict) -> None:
    """Krok 1 (LLM, ~45s): mikro-plan. Może zwrócić SPLIT_TASK (z limitowaną głębokością)."""
    t0 = time.time()
    state.transition(task["id"], "RUNNING", substep="1/5_PLAN")
    result = _llm_step(
        task, "1/5_PLAN",
        json.dumps({"title": task["title"], "acceptance_criteria": task["acceptance_criteria"],
                    "scope_paths": task["scope_paths"], "split_depth": task["split_depth"],
                    "max_split_depth": MAX_SPLIT_DEPTH,
                    "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"plan": "<krótki plan kroków>"} '
                              'lub jeśli zadanie za duże: {"signal": "SPLIT_TASK", "subtasks": [{"title": ..., "scope_paths": [...], "role": ...}]}. '
                              'NIE osadzaj treści plików — tylko plan.'}, ensure_ascii=False),
        model=skill_compiler.model_for_role(task["role"], kind="fast"),
    )
    state.record_metric("step_time", {"step": "1/5_PLAN", "seconds": round(time.time() - t0, 2)}, task["id"])
    if result.get("signal") == "SPLIT_TASK":
        try:
            ids = state.split_task(task["id"], result.get("subtasks", []), max_split_depth=MAX_SPLIT_DEPTH)
            _log(task["id"], f"SPLIT_TASK -> {ids}")
            state.record_metric("split_task", {"children": ids}, task["id"])
            return
        except Exception as exc:  # limit głębokości — każemy iść dalej
            _log(task["id"], f"SPLIT_TASK odrzucony ({exc}) — kontynuacja bez podziału")
    plan = result.get("plan", "")
    Path(ROOT / ".agents" / "runtime" / f"{task['id']}.plan.md").write_text(plan, encoding="utf-8")
    state.transition(task["id"], "RUNNING", substep="2/5_IMPL")


def step2_implement(state: State, task: dict, worktree: Path) -> None:
    """Krok 2 (LLM, ~120s): wyłącznie kod produkcyjny — plan + zamrożone kontrakty + scope."""
    t0 = time.time()
    contracts = skill_compiler.load_contracts()
    payload = {
        "plan": (ROOT / ".agents" / "runtime" / f"{task['id']}.plan.md").read_text(encoding="utf-8"),
        "contracts": contracts,
        "scope_paths": task["scope_paths"],
        "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"files": {"<path dokładnie ze scope_paths>": "<pełna treść pliku>"}}. '
                  'Żadnych innych kluczy, żadnego tekstu poza JSON.',
    }
    result = _llm_step(task, "2/5_IMPL", json.dumps(payload, ensure_ascii=False),
                       model=skill_compiler.model_for_role(task["role"], kind="strong"))
    files = result.get("files") or {}
    if not files and isinstance(result.get("artifacts"), list):  # fallback: kontrakt roli (artifacts)
        for a in result["artifacts"]:
            if isinstance(a, dict) and a.get("path"):
                files[a["path"]] = a.get("content", "")
    for rel, content in files.items():
        if task["scope_paths"] and rel not in task["scope_paths"]:
            raise PermissionError(f"Plik spoza scope_paths: {rel}")
        target = worktree / rel
        if target.exists() and target.is_dir():
            raise RuntimeError(f"LLM zwrócił katalog zamiast pliku: {rel}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    state.record_metric("step_time", {"step": "2/5_IMPL", "seconds": round(time.time() - t0, 2)}, task["id"])
    state.transition(task["id"], "RUNNING", substep="3/5_TEST")


def step3_tests(state: State, task: dict, worktree: Path) -> None:
    """Krok 3 (LLM, ~90s): testy jednostkowe + integracyjne względem acceptance_criteria."""
    t0 = time.time()
    code_files = {p: (worktree / p).read_text(encoding="utf-8")
                  for p in task["scope_paths"] if (worktree / p).exists()}
    payload = {"code": code_files, "acceptance_criteria": task["acceptance_criteria"],
               "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"test_files": {"tests/<plik>_test.py": "<kod testu>"}}. '
                         'Żadnych innych kluczy, żadnego tekstu poza JSON.'}
    result = _llm_step(task, "3/5_TEST", json.dumps(payload, ensure_ascii=False),
                       model=skill_compiler.model_for_role(task["role"], kind="fast"))
    test_files = result.get("test_files") or {}
    for rel, content in test_files.items():
        target = worktree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    state.record_metric("step_time", {"step": "3/5_TEST", "seconds": round(time.time() - t0, 2)}, task["id"])
    state.transition(task["id"], "RUNNING", substep="4/5_QG")


def step4_quality_gate(state: State, task: dict, worktree: Path, base: str) -> None:
    """Krok 4 (Python + LLM): lokalny QG; przy błędzie max 3 próby fix (Krok 4b)."""
    t0 = time.time()
    for attempt in range(1, MAX_LOCAL_FIXES + 2):
        results = quality_gates.local_gates(worktree)
        failed = [r for r in results if not r["ok"]]
        state.record_metric("local_qg", {"attempt": attempt, "failed": [r["name"] for r in failed]}, task["id"])
        if not failed:
            state.record_metric("step_time", {"step": "4/5_QG", "seconds": round(time.time() - t0, 2)}, task["id"])
            state.transition(task["id"], "RUNNING", substep="5/5_COMMIT")
            return
        if attempt > MAX_LOCAL_FIXES:
            state.transition(task["id"], FAILED, review_notes=json.dumps(
                {"stage": "QG", "failures": failed}, ensure_ascii=False))
            return
        # Krok 4b: LLM dostaje WYŁĄCZNIE traceback i konkretny plik
        fix_payload = {"failures": [
            {"name": r["name"], "tool": r.get("tool"), "output": (r.get("output") or "")[-4000:]}
            for r in failed]}
        try:
            fix = _llm_step(task, "4b_FIX", json.dumps(fix_payload, ensure_ascii=False),
                            model=skill_compiler.model_for_role(task["role"], kind="fast"))
            for rel, content in fix.get("files", {}).items():
                if rel not in task["scope_paths"]:
                    continue
                (worktree / rel).write_text(content, encoding="utf-8")
        except RuntimeError:
            state.transition(task["id"], FAILED, review_notes="LLM niedostępny w kroku 4b")
            return
    state.transition(task["id"], FAILED, review_notes="QG nieprzechodzony po lokalnych poprawkach")


def step45_simplify(state: State, task: dict, worktree: Path, base: str) -> None:
    """Simplification Pass (opcjonalny, tylko złożone zadania): kod działa i QG zielone
    -> LLM dostaje kod i ma zaproponować tę samą logikę idiomatycznie, o ~30% mniej linii.
    Jeśli po uproszczeniu lokalny QG pada — revert do wersji przed uproszczeniem."""
    try:
        lines = git_guard.diff_line_count(worktree, base)
    except git_guard.GitError:
        return
    if lines < 80:  # tylko większe zmiany mają sens
        return
    code = {p: (worktree / p).read_text(encoding="utf-8")
            for p in task["scope_paths"] if (worktree / p).exists()}
    if not code:
        return
    state.transition(task["id"], "RUNNING", substep="4.5_SIMPLIFY")
    payload = {"code": code, "diff_lines": lines,
               "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"files": {"<path ze scope_paths>": "<uproszczona treść>"}}. '
                         'TA SAMA logika i testy, idiomatycznie, ok. 30% mniej linii. Żadnego innego klucza.'}
    try:
        res = _llm_step(task, "4.5_SIMPLIFY", json.dumps(payload, ensure_ascii=False),
                        model=skill_compiler.model_for_role(task["role"], kind="strong"))
    except RuntimeError:
        return  # LLM niedostępny — pomijamy uproszczenie
    files = res.get("files") or {}
    files = {p: c for p, c in files.items() if p in task["scope_paths"]}
    if not files:
        return
    backup = {p: (worktree / p).read_text(encoding="utf-8") for p in files if (worktree / p).exists()}
    for rel, content in files.items():
        (worktree / rel).write_text(content, encoding="utf-8")
    results = quality_gates.local_gates(worktree)
    if any(not r["ok"] for r in results if not r.get("skipped")):
        for p, c in backup.items():  # revert
            (worktree / p).write_text(c, encoding="utf-8")
        state.record_metric("simplify_reverted", {"task": task["id"]}, task["id"])
        _log(task["id"], "simplify: QG po uproszczeniu padło -> revert")
        return
    after = git_guard.diff_line_count(worktree, base)
    state.record_metric("simplify_applied", {"before": lines, "after": after}, task["id"])
    _log(task["id"], f"simplify: {lines} -> {after} linii diffu")
    state.transition(task["id"], "RUNNING", substep="5/5_COMMIT")


def step5_commit(state: State, task: dict, worktree: Path, base: str) -> None:
    """Krok 5 (Python): scope_paths + limit diffu + commit + push -> IN_REVIEW."""
    reverted = git_guard.enforce_scope(worktree, task["scope_paths"])
    if reverted:
        state.record_metric("scope_revert", {"files": reverted}, task["id"])
    try:
        lines = git_guard.assert_diff_limit(worktree, base)  # zmiana #7
    except git_guard.DiffTooLarge as exc:
        git_guard.run_git(["checkout", "--", "."], cwd=worktree, check=False)
        state.transition(task["id"], READY, substep="1/5_PLAN",
                         review_notes=f"Odrzucono przed CR: {exc}")
        return
    git_guard.commit_all(worktree, f"feat({task['id'].lower()}): {task['title']}")
    git_guard.push(task["branch"], worktree)
    state.add_comment(task["id"], f"worker({task['role']})",
                      f"✅ zrobiłem i zacommitowałem ({lines} linii, branch {task['branch']}) → proszę o Code Review")
    # GitHub: push branch + PR (base = gałąź milestonu albo master)
    try:
        from engine import github_bridge
        pr = github_bridge.ensure_pr_for_task(task, base)
        if pr:
            state.add_comment(task["id"], f"worker({task['role']})",
                              f"🐙 PR !{pr} otwarty na GitHubie ({task['branch']} -> {base})")
    except Exception as exc:
        _log(task["id"], f"github bridge: {exc!r}")
    state.transition(task["id"], IN_REVIEW, substep="done")
    state.record_metric("diff_lines", {"lines": lines}, task["id"])
    _log(task["id"], f"commit OK ({lines} linii diffu) -> IN_REVIEW")


def _tg(state: State, task_id: str, extra: str = "") -> None:
    """Aktualizacja żywej karty w temacie Telegram (no-op gdy brak env)."""
    t = state.get_task(task_id)
    if t:
        telegram_reporter.update_progress(state, t, extra=extra)


def step_llm(state: State, task: dict, worktree: Path, base: str) -> None:
    """Tryb 'llm' (komenda `llm <opis>` z Telegrama): całe zadanie wykonuje LLM (vibe z narzędziami)
    w worktree. LLM ma na końcu zostawić raport dla drużyny (docs/llm-reports/<id>.md);
    worker dopisuje go do feeda (komentarz) i commituje wszystko przez step5."""
    tid = task["id"]
    state.transition(tid, "RUNNING", substep="LLM_EXEC")
    full_desc = state.get_kv(f"llm_desc:{tid}") or task["title"]
    report_rel = f"docs/llm-reports/{tid}.md"
    system = (
        "Jesteś agentem-wykonawcą (tryb LLM) w repozytorium mikowhy-teams (Dark Factory).\n"
        "Pracujesz w worktree zadania — WSZYSTKIE zmiany rób WŁAŚNIE TAM.\n"
        "Masz włączone narzędzia (bash, read/write/edit plików, process) — wykonuj zadanie SAMODZIELNIE, krok po kroku.\n"
        "Zasady repo: przeczytaj AGENTS.md i contracts/git-contract.md w katalogu głównym worktree i ich przestrzegaj.\n"
        "NIE rób `git commit`/`git push` — zmiany commituje i pushuje silnik po Twoim zakończeniu.\n"
        "Odpowiadaj i pisz raporty PO POLSKU.\n"
        f"OBOWIĄZKOWO na końcu napisz raport dla drużyny: `{report_rel}` w formacie:\n"
        f"# {tid} — raport LLM\n## Co zrobiono\n## Zmienione pliki\n## Jak zweryfikować (komendy)\n## Ryzyka / uwagi\n"
        "Raport kompletny, ale zwięzły (max ~120 linii)."
    )
    user = (
        f"Zadanie: {tid} — {task['title']}\n\nPełny opis od Sponsora:\n{full_desc}\n\n"
        f"Acceptance criteria: {task.get('acceptance_criteria') or '—'}\n\n"
        f"Katalog roboczy (worktree): {worktree}\nBase branch: {base}\nBranch zadania: {task.get('branch')}\n\n"
        f"Wykonaj zadanie w całości, a na końcu ZAWSZE dopisz raport `{report_rel}`."
    )
    gw = get_gateway()
    if not hasattr(gw, "agent"):
        raise RuntimeError("tryb 'llm' wymaga vibe CLI w PATH (LLM_PROVIDER=vibe)")
    model = skill_compiler.model_for_role(task["role"], kind="strong")
    _log(tid, f"LLM_EXEC start (model={model}, worktree={worktree})")
    proc = gw.agent(system=system, user=user, cwd=worktree, step=f"LLM_EXEC-{tid}", model=model)
    _log(tid, f"LLM_EXEC rc={proc.returncode}: stdout_tail={proc.stdout.strip()[-400:]!r} stderr_tail={proc.stderr.strip()[-300:]!r}")
    report = worktree / report_rel
    if report.exists():
        text = report.read_text(encoding="utf-8", errors="replace")
        state.add_comment(tid, f"llm({model})", f"🤖 Raport LLM (`{report_rel}`):\n{text[:1500]}")
        telegram_reporter.post_event(state, task, f"🤖 LLM wykonał `{tid}` — raport w `{report_rel}` (patrz też feed)")
    else:
        state.add_comment(tid, f"llm({model})",
                          f"⚠️ LLM skończył (rc={proc.returncode}) ale NIE ma raportu `{report_rel}`. "
                          f"Output: {proc.stdout.strip()[-500:]}")
        if proc.returncode != 0:
            raise RuntimeError(f"LLM agent rc={proc.returncode} i brak raportu: {proc.stderr.strip()[-400:]}")
    # LLM mógł już sam zacommitować — wtedy tylko push + IN_REVIEW
    staged = git_guard.run_git(["diff", "--cached", "--quiet"], cwd=worktree, check=False).returncode != 0
    ahead = git_guard.run_git(["diff", "--quiet", f"{base}...HEAD"], cwd=worktree, check=False).returncode != 0
    if not staged and ahead:
        git_guard.push(task["branch"], worktree)
        state.add_comment(tid, f"llm({model})", f"✅ LLM sam zacommitował ({task['branch']}) → IN_REVIEW")
        state.transition(tid, IN_REVIEW, substep="done")
        return
    step5_commit(state, task, worktree, base)


def run(task_id: str) -> int:
    state = State()
    task = state.claim_task(task_id, claimed_by=f"worker-{os.getpid()}")
    if task is None:
        _log(task_id, "zadanie niedostępne do przejęcia")
        return 1
    telegram_reporter.ensure_topic(state, task)
    worktree: Path | None = None
    try:
        worktree = step0_prepare(state, task)
        _tg(state, task_id, "worktree gotowy")
        base = state.get_milestone(task["milestone"])["branch"]
        task = state.get_task(task_id)  # świeży wiersz (branch z step0)
        if (task.get("mode") or "pipeline") == "llm":
            step_llm(state, task, worktree, base)
            task = state.get_task(task_id)
            if task and task["status"] == "IN_REVIEW":
                _tg(state, task_id, "LLM wykonał zadanie → IN_REVIEW (czeka na Code Review)")
            return 0
        step1_plan(state, task)
        task = state.get_task(task_id)  # split może zmienić status
        if task["status"] != "RUNNING":
            telegram_reporter.post_event(state, task, "✂️ SPLIT_TASK — zadanie podzielone na podzadania (patrz karta)")
            return 0
        _tg(state, task_id)
        step2_implement(state, task, worktree)
        _tg(state, task_id)
        step3_tests(state, task, worktree)
        _tg(state, task_id)
        step4_quality_gate(state, task, worktree, base)
        task = state.get_task(task_id)
        if task["status"] != "RUNNING":
            telegram_reporter.post_event(state, task, f"❌ lokalny QG nie przeszedł: {(task.get('review_notes') or '')[:300]}")
            _tg(state, task_id)
            return 0
        step45_simplify(state, task, worktree, base)
        task = state.get_task(task_id)
        if task["status"] == "RUNNING":
            step5_commit(state, task, worktree, base)
        task = state.get_task(task_id)
        if task and task["status"] == "IN_REVIEW":
            _tg(state, task_id, "commit OK → IN_REVIEW (czeka na Code Review)")
        return 0
    except Exception as exc:  # deterministyczny fallback — nigdy cicha śmierć
        state.transition(task_id, FAILED, review_notes=repr(exc))
        t = state.get_task(task_id)
        if t:
            telegram_reporter.post_event(state, t, f"💥 FAILED: `{exc!r}`")
            telegram_reporter.update_progress(state, t)
        _log(task_id, f"FAILED: {exc!r}")
        return 2
    finally:
        if worktree is not None:
            repo = projects.repo_for_task(task, state)
            git_guard.remove_worktree(task_id, ns=task.get("project_id") or "mikowhy-teams", repo=repo)


def main() -> None:
    ap = argparse.ArgumentParser(description="Worker mikro-etapów dla zadania DAG")
    ap.add_argument("task_id")
    args = ap.parse_args()
    raise SystemExit(run(args.task_id))


if __name__ == "__main__":
    main()

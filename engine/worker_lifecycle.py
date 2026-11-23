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
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import git_guard, projects, quality_gates, skill_compiler, telegram_reporter
from engine.llm_gateway import get_gateway
from engine.state import (
    AWAITING_OWNER,
    FAILED,
    IN_REVIEW,
    READY,
    State,
    resume_substep,
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
    state.transition(task["id"], "RUNNING", substep=resume_substep(task), branch=branch, worktree=str(worktree))  # v2.7.9: kontynuacja od wypracowanego etapu
    _log(task["id"], f"worktree={worktree} repo={repo} base={base} branch={branch}")
    return worktree


SECRET_NAME_HINTS = (".env", "secret", "credential", "token", "id_rsa", ".pem", ".key", "password", "api_key", "apikey")
SCOPE_AUTOEXTEND_MAX = 5  # max automatycznych poszerzeń scope na zadanie (potem wymaga człowieka)


def _scope_autoextend(state: State, task: dict, rel: str) -> bool:
    """Auto-poszerzenie scope_paths o `rel` (zasada „Opiekunowi łatwiej” — rutynowy przypadek,
    nie eskalujemy). Zwraca False gdy plik wygląda na sekret albo limit poszerzeń wyczerpany."""
    low = rel.lower()
    if any(h in low for h in SECRET_NAME_HINTS):
        return False  # sekrety nigdy automatycznie
    cur = list(task.get("scope_paths") or [])
    if rel in cur:
        return True  # już w scope
    key = f"scope_autoext_count:{task['id']}"
    used = int(state.get_kv(key, 0) or 0)
    if used >= SCOPE_AUTOEXTEND_MAX:
        return False
    import json as _json
    state._conn().execute("UPDATE tasks SET scope_paths=?, updated_at=? WHERE id=?",
                          (_json.dumps(sorted(set(cur + [rel]))), time.time(), task["id"]))
    state.set_kv(key, used + 1)
    state.add_comment(task["id"], "worker(auto-heal)",
                      f"🔧 auto-poszerzenie scope_paths o `{rel}` (rutynowy przypadek — bez eskalacji do Opiekuna; {used + 1}/{SCOPE_AUTOEXTEND_MAX})")
    state.record_metric("scope_autoextend", {"path": rel, "used": used + 1}, task["id"])
    return True


def _llm_step(task: dict, step: str, user_payload: str, model: str) -> dict:
    gw = get_gateway()
    system = skill_compiler.compile_system_prompt(task["role"], stack=None, task=task)
    # priorytet zadania leci też do kolejki LLM (P0/P1 nie czekają za P3)
    return gw.complete_json(system=system, user=user_payload, step=step, model=model,
                            priority=task.get("priority") or "P2")


def _ask_sponsor(state: State, task: dict, question: str, options: list | None = None) -> None:
    """ASK_OWNER: agent nie podejmuje decyzji spoza swoich kompetencji — pakuje zwięzłe
    pytanie dla Opiekuna (sedno problemu + 2-3 opcje + dlaczego automat nie może tego
    bezpiecznie rozstrzygnąć) i czeka: AWAITING_OWNER/substep=ASK_OWNER.
    To JEDYNY przypadek, gdy Opiekun wchodzi przed końcem flow (graniczna decyzja).
    Odpowiedź Opiekuna ląduje w review_notes (decyzja na stałe w kontekście zadania),
    a po approve zadanie wraca do READY i przechodzi pełny cykl."""
    opts = [str(o) for o in (options or [])][:3] or ["A: kontynuować z bezpiecznym, odwracalnym wariantem (założenie odnotowane)",
                                                       "B: zmienić zakres/wymaganie", "C: odrzucić zadanie"]
    opts_txt = "\n".join(f"- {o}" for o in opts)
    q = str(question)[:800]
    state.add_comment(task["id"], f"worker({task['role']})",
                      f"🧑‍💼 Pytanie do Opiekuna (ostatnia instancja): {q}\nOpcje:\n{opts_txt}")
    try:
        telegram_reporter.post_event(state, task,
                                     f"🧑‍💼 *AWAITING_OWNER* `{task['id']}` ({task['role']}): {q}"
                                     + f"\nOpcje: {', '.join(opts)}"
                                     + "\nOdpisz w wątku zadania, potem zatwierdź (decyzja → READY, pełny cykl).")
    except Exception:
        pass
    state.transition(task["id"], AWAITING_OWNER, substep="ASK_OWNER",
                     review_notes=f"Pakiet dla Opiekuna — co utknęło: {q} | Opcje: {opts_txt}")
    state.record_metric("ask_owner", {"question": q[:300], "options": opts}, task["id"])
    state.add_notification("owner", f"🧑‍💼 {task['id']} czeka na Twoją decyzję: {q[:140]}", task["id"])  # v2.7.15: dzwonek
    _log(task["id"], f"ASK_OWNER: {q[:200]}")


def step1_plan(state: State, task: dict) -> None:
    """Krok 1 (LLM, ~45s): mikro-plan. Może zwrócić SPLIT_TASK lub ASK_OWNER (graniczna decyzja Opiekuna)."""
    t0 = time.time()
    state.transition(task["id"], "RUNNING", substep=resume_substep(task))  # v2.7.9: kontynuacja od wypracowanego etapu
    sponsor_notes = state.recent_comments(5, task_id=task["id"])
    notes_txt = "\n".join(f"- {c.get('author')}: {str(c.get('text'))[:200]}" for c in sponsor_notes)
    result = _llm_step(
        task, "1/5_PLAN",
        json.dumps({"title": task["title"], "acceptance_criteria": task["acceptance_criteria"],
                    "scope_paths": task["scope_paths"], "split_depth": task["split_depth"],
                    "max_split_depth": MAX_SPLIT_DEPTH,
                    "review_notes": task.get("review_notes") or "",
                    "sponsor_comments": notes_txt,
                    "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"plan": "<krótki plan kroków>"} '
                              'lub TYLKO GDY zadanie jest naprawdę za duże: {"signal": "SPLIT_TASK", "subtasks": [{"title": ..., "scope_paths": [...], "role": ...}]}. '
                              'REGUŁA ANTY-SPLIT: NIE dziel, gdy zadanie dotyczy <=3 plików, szacunkowo <=150 linii diffu '
                              'i ma <3 punktów AC — wtedy wolisz jeden solidny plan i jedna paczka zmian (więcej zmian, mniej commitów). '
                              'Split max 3 podzadania, tylko gdy to osobne, niezależne części. '
                              'NIE osadzaj treści plików — tylko plan.'}, ensure_ascii=False),
        model=skill_compiler.model_for_role(task["role"], kind="fast"),
    )
    state.record_metric("step_time", {"step": "1/5_PLAN", "seconds": round(time.time() - t0, 2)}, task["id"])
    if result.get("signal") in ("ASK_SPONSOR", "ASK_OWNER"):  # ASK_OWNER = alias, graniczna decyzja Opiekuna
        _ask_sponsor(state, task, str(result.get("question", "")), result.get("options") or [])
        return
    if result.get("signal") == "SPLIT_TASK":
        try:
            ids = state.split_task(task["id"], result.get("subtasks", []), max_split_depth=MAX_SPLIT_DEPTH)
            _log(task["id"], f"SPLIT_TASK -> {ids}")
            state.record_metric("split_task", {"children": ids}, task["id"])
            state.add_comment_report(task["id"], f"worker({task['role']})",
                                     f"rozbić {task['id']} na części, bo jest za duże na jedno podejście",
                                     f"utworzono podzadania: {', '.join(ids)} — każde dostało własny zakres i rolę")
            return
        except Exception as exc:  # limit głębokości — każemy iść dalej
            _log(task["id"], f"SPLIT_TASK odrzucony ({exc}) — kontynuacja bez podziału")
            state.add_comment_report(task["id"], f"worker({task['role']})",
                                     f"rozbić {task['id']} na części",
                                     f"podział odrzucony ({exc}) — realizuję jako całość, żeby nie mnożyć podzadań")
    plan = result.get("plan", "")
    plan_rel = f".agents/runtime/{task['id']}.plan.md"
    Path(ROOT / plan_rel).write_text(plan, encoding="utf-8")
    state.add_attachment(task["id"], plan_rel, "📋 plan zadania (1/5_PLAN)", "md")  # v2.7.10: rezultat = załącznik zadania
    state.add_comment_report(task["id"], f"worker({task['role']})",
                             f"zaplanować realizację: {(task.get('title') or '')[:80]}",
                             f"plan gotowy ({len(plan)} znaków, zapisany jako załącznik 📎 {plan_rel}) — przechodzę do implementacji")
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
    if result.get("signal") in ("ASK_SPONSOR", "ASK_OWNER"):  # ASK_OWNER = alias, graniczna decyzja Opiekuna
        _ask_sponsor(state, task, str(result.get("question", "")), result.get("options") or [])
        return
    files = result.get("files") or {}
    if not files and isinstance(result.get("artifacts"), list):  # fallback: kontrakt roli (artifacts)
        for a in result["artifacts"]:
            if isinstance(a, dict) and a.get("path"):
                files[a["path"]] = a.get("content", "")
    for rel, content in files.items():
        if task["scope_paths"] and rel not in task["scope_paths"]:
            # AUTO-HEAL (zasada „Opiekunowi łatwiej”): plik spoza scope nie eskaluje do Opiekuna —
            # poszerzamy scope automatycznie (limit + ochrona sekretów), logujemy i kontynuujemy.
            if _scope_autoextend(state, task, rel):
                task = state.get_task(task["id"])  # odśwież scope_paths
            else:
                raise PermissionError(f"Plik spoza scope_paths (zablokowany sekret/limit): {rel}")
        target = worktree / rel
        if target.exists() and target.is_dir():
            # LLM zwrócił katalog zamiast pliku — nie failujemy całego zadania, pomijamy z logiem
            state.add_comment_report(task["id"], f"worker({task['role']})",
                                     f"zapisać plik `{rel}`",
                                     f"LLM zwrócił katalog zamiast pliku — pomijam ten wpis (auto-heal), reszta idzie dalej")
            state.record_metric("llm_dir_instead_of_file", {"path": rel}, task["id"])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    state.record_metric("step_time", {"step": "2/5_IMPL", "seconds": round(time.time() - t0, 2)}, task["id"])
    state.add_comment_report(task["id"], f"worker({task['role']})",
                             f"zaimplementować zmiany w zakresie {', '.join(task.get('scope_paths') or [])[:80]}",
                             f"zapisano {len(files)} plików — przechodzę do testów")
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
    if lines == 0:
        # pusty diff = brak realnej zmiany — NIE commitujemy pustego commita (koniec z '0 linii diffu')
        git_guard.run_git(["checkout", "--", "."], cwd=worktree, check=False)
        state.transition(task["id"], READY, substep="1/5_PLAN",
                         review_notes="Pusty diff względem bazy — worker nie wprowadził realnej zmiany (odrzucono przed commitem)")
        state.record_metric("empty_diff_rejected", {}, task["id"])
        _log(task["id"], "pusty diff (0 linii) — odrzucono przed commitem")
        return
    how = git_guard.commit_task(worktree, task["id"], f"feat({task['id'].lower()}): {task['title']}")
    if how == "amended":
        state.record_metric("commit_amended", {"branch": task["branch"]}, task["id"])
    try:
        git_guard.sync_with_origin(worktree, base)  # twarda reguła: wyrównaj z origin przed pushem
    except git_guard.MergeConflict as exc:
        state.transition(task["id"], READY, substep="1/5_PLAN",
                         review_notes=f"Konflikt z origin/{base} przed pushem: {exc} — wyrównaj i spróbuj ponownie")
        state.record_metric("origin_conflict", {"base": base, "error": str(exc)[:200]}, task["id"])
        _log(task["id"], f"konflikt z origin/{base}: {exc}")
        return
    git_guard.push(task["branch"], worktree)
    state.add_comment_report(task["id"], f"worker({task['role']})",
                             f"dostarczyć zmiany z {task['id']} do review",
                             f"zacommitowano {lines} linii na gałęzi {task['branch']} i wypchnięto — czekam na niezależny Code Review")
    # GitHub: push branch + PR (base = gałąź milestonu albo master)
    try:
        from engine import github_bridge
        pr = github_bridge.ensure_pr_for_task(task, base)
        if pr:
            state.add_comment_report(task["id"], f"worker({task['role']})",
                                     f"otworzyć PR dla {task['id']}",
                                     f"PR !{pr} otwarty na GitHubie ({task['branch']} → {base})")
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


def _llm_agent_loop(state: State, task: dict, worktree: Path, base: str, max_turns: int = 30) -> str:
    """Pętla agentowa trybu 'llm': LLM (vibe headless, multi-turn) SAM wykonuje zadanie —
    każdą turę odpowiada JSON-em z akcją (bash / write_file / read_file / list / done),
    a silnik egzekwuje ją w worktree i odsyła wynik. LLM na końcu zapisuje raport
    docs/llm-reports/<tid>.md (przez własną akcję write_file), potem daje action=done.

    Dlaczego pętla w Pythonie, a nie narzędzia vibe: zagnieżdżony headless vibe ma w tym
    sandboxie twardo odcięte wykonywanie tooli (tool_denied mimo --auto-approve), więc
    LLM dostaje "ręce" od silnika — samo decyduje i wykonuje komendy krok po kroku.
    """
    tid = task["id"]
    gw = get_gateway()
    model = skill_compiler.model_for_role(task["role"], kind="strong")
    full_desc = state.get_kv(f"llm_desc:{tid}") or task["title"]
    report_rel = f"docs/llm-reports/{tid}.md"
    system = (
        "Jesteś agentem-wykonawcą (tryb LLM) w repozytorium mikowhy-teams (Dark Factory).\n"
        f"Pracujesz w worktree zadania: {worktree} — wszystkie ścieżki podawaj WZGLĘDEM niego.\n"
        "Przeczytaj AGENTS.md i contracts/git-contract.md w katalogu głównym i ich przestrzegaj.\n"
        "NIE rób `git commit`/`git push` — zmiany commituje i pushuje silnik po Twoim `done`.\n"
        "Pisz po polsku.\n"
        "ZASADA GŁÓWNA: DZIAŁAJ, NIE ANALIZUJ — wykonujesz zadanie dosłownie wg opisu Sponsora. "
        "Nie czytaj więcej niż 2-3 plików rozpoznawczych; nie powtarzaj tych samych komend.\n\n"
        "ODPOWIADASZ WYŁĄCZNIE JEDNYM JSON-em (bez żadnego innego tekstu), akcja do wykonania:\n"
        '{"action":"bash","cmd":"..."} — komenda powłoki w worktree (limit 300s)\n'
        '{"action":"write_file","path":"relative/path","content":"..."} — zapis/utworzenie pliku w worktree\n'
        '{"action":"read_file","path":"relative/path"} — odczyt pliku (max 3000 znaków zwrócone)\n'
        '{"action":"list","path":"."} — lista katalogu\n'
        '{"action":"ask","question":"...","options":["A","B"]} — gdy decyzja wykracza poza Twoje kompetencje '
        '(kolor/wygląd/UX/priorytet) — NIE zgaduj, zapytaj Sponsora i czekaj\n'
        '{"action":"done","summary":"co zrobiono"} — koniec zadania\n\n'
        f"KONIECZNOŚĆ: przed `done` ZAWSZE zapisz raport drużynie przez write_file do `{report_rel}`:\n"
        f"# {tid} — raport LLM\n## Co zrobiono\n## Zmienione pliki\n## Jak zweryfikować (komendy)\n## Ryzyka / uwagi\n"
        "(max ~120 linii). Dopiero potem wyślij action=done."
    )
    sponsor_notes = state.recent_comments(5, task_id=tid)
    notes_txt = "\n".join(f"- {c.get('author')}: {str(c.get('text'))[:250]}" for c in sponsor_notes)
    user0 = (
        f"Zadanie: {tid} — {task['title']}\n\nPełny opis od Sponsora:\n{full_desc}\n\n"
        f"Acceptance criteria: {task.get('acceptance_criteria') or '—'}\n"
        f"Worktree: {worktree} | base: {base} | branch: {task.get('branch')}\n\n"
        "Wykonaj zadanie krok po kroku (bash/read_file/list/write_file), a na końcu zapisz raport i daj done.\n"
        "NIE podejmuj decyzji wizualnych/produktowych (kolor, wygląd, UX) — dawaj wtedy action=ask.\n"
        f"Uwagi/odpowiedzi Sponsora:\n{task.get('review_notes') or ''}\n{notes_txt}\n"
    )
    history: list[str] = []
    wt_resolved = str(worktree.resolve())
    done = False
    for turn in range(1, max_turns + 1):
        if turn == 1:
            payload = user0
        else:
            recent = [h[:700] for h in history[-10:]]
            payload = ("Kontynuuj zadanie. Dotychczasowe kroki i wyniki:\n" + "\n".join(recent)
                       + "\n\nTeraz kolejny krok — odpowiadaj WYŁĄCZNIE JSON-em z action.")
            cmds = [h.split("\n", 1)[0] for h in history[-3:] if h.startswith(f"t") and " $ " in h]
            if len(cmds) >= 2 and cmds[-1] == cmds[-2]:
                payload += ("\n\nUWAGA: powtarzasz ostatnią komendę — PRZESTAŃ eksplorować. "
                            "Przejdź od razu do działania: write_file (pliki zadania + raport) i done.")
        try:
            resp = gw.complete_json(system=system, user=payload, step=f"LLM_EXEC-{tid}-t{turn}", model=model)
        except Exception as exc:
            history.append(f"t{turn}: błąd LLM {exc!r} — kontynuuj mimo to")
            _log(tid, f"LLM t{turn} błąd: {exc!r}")
            continue
        if not isinstance(resp, dict):
            history.append(f"t{turn}: odpowiedź nie-JSON: {str(resp)[:300]}")
            continue
        action = str(resp.get("action", "")).strip().lower()
        if action == "bash":
            cmd = str(resp.get("cmd", ""))
            try:
                proc = subprocess.run(cmd, shell=True, cwd=worktree, capture_output=True,
                                      text=True, timeout=300)
                out = (proc.stdout + "\n" + proc.stderr).strip()[-1500:]
                history.append(f"t{turn} $ {cmd}\n→ rc={proc.returncode}\n{out}")
                _log(tid, f"LLM t{turn} bash rc={proc.returncode}: {cmd[:120]}")
            except subprocess.TimeoutExpired:
                history.append(f"t{turn} $ {cmd}\n→ TIMEOUT 300s")
        elif action == "write_file":
            rel = str(resp.get("path", ""))
            p = (worktree / rel).resolve()
            if not str(p).startswith(wt_resolved + os.sep) and str(p) != wt_resolved:
                history.append(f"t{turn} write_file ODRZUCONE (poza worktree): {rel}")
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(resp.get("content", ""))
            p.write_text(content, encoding="utf-8")
            history.append(f"t{turn} write_file {rel} OK ({len(content)} znaków)")
            _log(tid, f"LLM t{turn} write_file {rel} ({len(content)}B)")
        elif action == "read_file":
            p = (worktree / str(resp.get("path", ""))).resolve()
            try:
                content = p.read_text(encoding="utf-8", errors="replace")[:3000]
            except OSError as exc:
                content = f"błąd odczytu: {exc}"
            history.append(f"t{turn} read_file {resp.get('path')}\n→\n{content}")
        elif action == "list":
            p = (worktree / str(resp.get("path", ".") or ".")).resolve()
            try:
                content = "\n".join(sorted(os.listdir(p))[:200])
            except OSError as exc:
                content = f"błąd listowania: {exc}"
            history.append(f"t{turn} ls {resp.get('path')}\n→\n{content}")
        elif action == "ask":
            # decyzja poza kompetencjami LLM — pyta Sponsora i czeka (NIE zgaduje)
            q = str(resp.get("question", ""))[:800]
            history.append(f"t{turn} ASK: {q}")
            _ask_sponsor(state, task, q, resp.get("options") or [])
            done = True
            break
        elif action == "done":
            history.append(f"t{turn} DONE: {str(resp.get('summary', ''))[:500]}")
            _log(tid, f"LLM t{turn} done: {str(resp.get('summary',''))[:200]}")
            done = True
            break
        else:
            history.append(f"t{turn}: nieznana akcja {action!r} (resp={str(resp)[:200]}) — użyj bash|write_file|read_file|list|done")
    if not done:
        _log(tid, f"LLM agent: limit {max_turns} turnów bez done")
    return report_rel


def step_llm(state: State, task: dict, worktree: Path, base: str) -> None:
    """Tryb 'llm' (komenda `llm <opis>` z Telegrama): całe zadanie wykonuje LLM w pętli agentowej
    (patrz _llm_agent_loop). LLM na końcu zostawia raport docs/llm-reports/<id>.md;
    worker dopisuje go do feeda (komentarz) i commituje wszystko przez step5."""
    tid = task["id"]
    state.transition(tid, "RUNNING", substep="LLM_EXEC")
    model = skill_compiler.model_for_role(task["role"], kind="strong")
    _log(tid, f"LLM_EXEC start (model={model}, worktree={worktree})")
    report_rel = _llm_agent_loop(state, task, worktree, base)
    task = state.get_task(tid)
    if task["status"] != "RUNNING":  # ASK_OWNER/AWAITING_OWNER — czekamy na decyzję Opiekuna, nie commitujemy
        _log(tid, "LLM_EXEC zatrzymany: czekamy na Sponsora")
        return
    report = worktree / report_rel
    if report.exists():
        text = report.read_text(encoding="utf-8", errors="replace")
        state.add_comment(tid, f"llm({model})", f"🤖 Raport LLM (`{report_rel}`):\n{text[:1500]}")
        telegram_reporter.post_event(state, task, f"🤖 LLM wykonał `{tid}` — raport w `{report_rel}` (patrz też feed)")
    else:
        state.add_comment(tid, f"llm({model})",
                          f"⚠️ LLM skończył, ale NIE ma raportu `{report_rel}` — zadanie idzie do review mimo to")
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
        task = state.get_task(task_id)
        if task["status"] != "RUNNING":  # np. AWAITING_OWNER — czekamy na decyzję Opiekuna
            _tg(state, task_id)
            return 0
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

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
from engine.console_streamer import ConsoleStreamer
from engine.llm_gateway import get_gateway
from engine.state import (
    AWAITING_OWNER,
    COOLING,
    FAILED,
    IN_REVIEW,
    READY,
    RUNNING,
    State,
    resume_substep,
)

MAX_LOCAL_FIXES = 3        # Krok 4b — lokalne próby naprawy
MAX_SPLIT_DEPTH = 1        # zmiana #7 — twardy limit głębokości podziału
STEP_TIMEOUT_HINT = {"1/5_PLAN": 45, "2/5_IMPL": 120, "3/5_TEST": 90, "4/5_QG": 90}


def _log(task_id: str, msg: str) -> None:
    print(f"[{task_id}] {msg}", flush=True)


def _stage_stop(state: State, task_id: str, stage: str) -> bool:
    """v2.7.26 — pauza na granicy etapu (przycisk ⏹️ „Zakończ po etapie" Sponsora).

    Zwraca True gdy Sponsor żądał stopu: worker kończy bieżący etap, czyści flagę,
    przechodzi RUNNING → READY (bez epilogów kolejnych etapów) i zostawia czytelny
    komentarz Cel/Efekt. Wznowienie = standardowa kolejka (claim), NIC nie ginie.
    """
    try:
        if not state.stop_requested(task_id):
            return False
    except Exception:
        return False  # brak tabeli/kv — gdyby feature nie było dostępne, nie blokuj pracy
    state.clear_stop(task_id)
    state.transition(task_id, READY,
                     review_notes=f"pauza ⏹️ na granicy etapu {stage} (żądanie Sponsora: zakończ po etapie)")
    state.add_comment(task_id, "orchestrator",
                      f"⏹️ Cel: zakończyć pracę po etapie {stage} → ✅ Efekt: pauza na granicy etapu "
                      f"(status READY, postęp i załączniki zachowane) — wznowienie ze standardowej kolejki")
    state.record_metric("stage_stop", {"task": task_id, "stage": stage}, task_id)
    _log(task_id, f"stage-stop: pauza po etapie {stage} → READY")
    return True


def step0_prepare(state: State, task: dict) -> Path:
    """Krok 0 (Python): izolowany worktree + gałąź feature/<task>.
    v2.8.0 W-1: base = branch integracyjny projektu (develop) gdy projekt go ma
    (worktree WYŁĄCZNIE od develop); fallback: branch milestonu / default branch.

    🔴 NOWE: Pomijaj, jeśli wznawiamy od późniejszego etapu.
    """
    # 🔴 NOWE: Jeśli wznawiamy od kroku > 0/5_PREPARE, spróbuj użyć istniejącego worktree
    current_substep = task.get("substep") or ""
    if current_substep not in ("0/5_PREPARE", ""):
        _log(task["id"], f"⏭️ step0_prepare: wznowienie od {current_substep}, sprawdzam worktree...")
        # Zwróć istniejącą ścieżkę worktree z bazy, JEŚLI ISTNIEJE
        existing_worktree = task.get("worktree")
        if existing_worktree and Path(existing_worktree).exists():
            _log(task["id"], f"✅ worktree istnieje: {existing_worktree}")
            return Path(existing_worktree)
        # 🔴 NOWE: Jeśli worktree nie istnieje, NIE zwracaj None - kontynuuj tworzenie
        _log(task["id"], "⚠️ worktree nie istnieje, tworzę nowy (wznowienie nie było czyste)")
    milestone = state.get_milestone(task["milestone"])
    ns = task.get("project_id") or "mikowhy-teams"
    repo = projects.repo_for_task(task, state)
    if not git_guard.is_git_repo(repo):
        from engine.state import ROOT as factory
        if git_guard.is_git_repo(factory):
            _log(task["id"], f"repo {repo} nie jest gitem — używam fabryki {factory}")
            repo = factory
    ib = projects.integration_branch(ns)
    if ib and git_guard.ensure_branch(repo, ib):
        base = ib
    elif ib and git_guard.is_git_repo(repo) and git_guard.run_git(
            ["rev-parse", "--verify", "--quiet", ib], cwd=repo, check=False).returncode == 0:
        base = ib
    else:
        base = milestone["branch"] if milestone else git_guard.default_branch(repo)
    branch = task["branch"] or f"feature/{task['id'].lower()}"
    # fallback: gdy gałąź milestonu jeszcze nie istnieje (np. FIX z health) — leć z głównej gałęzi
    if base != git_guard.default_branch(repo):
        chk = git_guard.run_git(["rev-parse", "--verify", "--quiet", base], cwd=repo, check=False)
        if chk.returncode != 0:
            _log(task["id"], f"base {base} nie istnieje — fallback do {git_guard.default_branch(repo)}")
            base = git_guard.default_branch(repo)
    git_guard.assert_writable(repo)                      # STRICT READ-ONLY guard
    ns = task.get("project_id") or "mikowhy-teams"
    worktree = git_guard.create_worktree(task["id"], base, branch, repo=repo, ns=ns)
    _safe_transition(state, task["id"], "RUNNING", substep=resume_substep(task), branch=branch, worktree=str(worktree))  # v2.7.9: kontynuacja od wypracowanego etapu
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
    _safe_transition(state, task["id"], AWAITING_OWNER, substep="ASK_OWNER",
                     review_notes=f"Pakiet dla Opiekuna — co utknęło: {q} | Opcje: {opts_txt}")
    state.record_metric("ask_owner", {"question": q[:300], "options": opts}, task["id"])
    state.add_notification("owner", f"🧑‍💼 {task['id']} czeka na Twoją decyzję: {q[:140]}", task["id"])  # v2.7.15: dzwonek
    _log(task["id"], f"ASK_OWNER: {q[:200]}")


def _safe_transition(state: "State", task_id: str, new_status: str, **fields):
    """v2.7.20: odporne przejście — przy wyścigu (zombie-recovery/orchestrator zmienił status między
    krokami) InvalidTransition nie crashuje workera: jeśli zadanie jest znów claimowalne, przejmujemy
    je ponownie; jeśli ktoś inny zamknął/przejął — wychodzimy cicho (SystemExit 0)."""
    from engine.state import InvalidTransition, READY as _READY, CHANGES_REQUESTED as _CR
    try:
        return state.transition(task_id, new_status, **fields)
    except InvalidTransition:
        t = state.get_task(task_id)
        if t is None:
            _log(task_id, "zadanie zniknęło — wychodzę")
            raise SystemExit(0)
        cur = t["status"]
        if cur in (_READY, _CR) and new_status == "RUNNING":
            state.claim_task(task_id, t.get("claimed_by") or "worker-reclaim")
            return state.transition(task_id, "RUNNING", **fields)
        _log(task_id, f"status {cur} (inny proces przejął/zmienił) — wychodzę cicho")
        raise SystemExit(0)


def step1_plan(state: State, task: dict) -> None:
    """Krok 1 (LLM, ~45s): mikro-plan. Może zwrócić SPLIT_TASK lub ASK_OWNER (graniczna decyzja Opiekuna)."""
    t0 = time.time()
    _safe_transition(state, task["id"], "RUNNING", substep=resume_substep(task))  # v2.7.9: kontynuacja od wypracowanego etapu
    sponsor_notes = state.recent_comments(5, task_id=task["id"])
    notes_txt = "\n".join(f"- {c.get('author')}: {str(c.get('text'))[:200]}" for c in sponsor_notes)
    result = _llm_step(
        task, "1/5_PLAN",
        json.dumps({"title": task["title"], "acceptance_criteria": task["acceptance_criteria"],
                    "scope_paths": task["scope_paths"], "split_depth": task["split_depth"],
                    "max_split_depth": MAX_SPLIT_DEPTH,
                    "review_notes": task.get("review_notes") or "",
                    "sponsor_comments": notes_txt,
                    "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"plan": "<krótki plan kroków>", "eta_s": <szacunek CAŁEGO zadania w sekundach — patrz skill estimation.md>} '
                              'lub TYLKO GDY zadanie jest naprawdę za duże: {"signal": "SPLIT_TASK", "subtasks": [{"title": ..., "scope_paths": [...], "role": ..., "eta_s": ...}]}. '
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
    # v2.7.19: agent szacuje ETA całego zadania (skill estimation) — doprecyzowanie heurystyki z add_task
    try:
        eta = float(result.get("eta_s") or 0)
        if eta > 0:
            state.set_eta(task["id"], eta)
            state.record_metric("eta_estimated", {"eta_s": round(eta), "via": "plan"}, task["id"])
    except Exception:
        pass
    plan = _coerce_file_content(result.get("plan", ""))
    plan_rel = f".agents/runtime/{task['id']}.plan.md"
    Path(ROOT / plan_rel).write_text(plan, encoding="utf-8")
    state.add_attachment(task["id"], plan_rel, "📋 plan zadania (1/5_PLAN)", "md")  # v2.7.10: rezultat = załącznik zadania
    state.add_comment_report(task["id"], f"worker({task['role']})",
                             f"zaplanować realizację: {(task.get('title') or '')[:80]}",
                             f"plan gotowy ({len(plan)} znaków, zapisany jako załącznik 📎 {plan_rel}) — przechodzę do implementacji")
    _safe_transition(state, task["id"], "RUNNING", substep="2/5_IMPL")


def _resolve_task_rel(rel: str, worktree: Path) -> str | None:
    """v2.7.29: NORMALIZACJA ścieżki od LLM.

    LLM potrafi zwrócić ŚCIEŻKĘ ABSOLUTNĄ (np. /home/miko/Projekty/M+/mikowhy-teams/engine/x.py,
    "/home/miko/...").  `worktree / abs_path` == abs_path → zapis ląduje W MASTERZE
    (root cause „target dirty" i zapisów obok fabryki).  Zwraca ścieżkę relatywną
    do worktree albo None gdy ścieżka jest absolutna poza ROOT (do pominięcia z logiem).
    """
    rel = str(rel or "").strip()
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        return rel
    wt_str = str(worktree.resolve())
    root_str = str(ROOT.resolve())
    ps = str(p)
    if ps.startswith(wt_str + os.sep):
        return str(Path(ps).relative_to(wt_str))
    if ps.startswith(root_str + os.sep):
        # LLM podał ścieżkę w GŁÓWNYM repo — mapuj na odpowiednik w worktree (nie w masterze!)
        return str(Path(ps).relative_to(root_str))
    return None  # mystyczna ścieżka absolutna poza fabryką — odrzucamy z logiem


PATCH_OVERWRITE_LIMIT = 150  # W-2: pliki > 150 linii NIE mogą być nadpisywane w całości (tylko SEARCH/REPLACE)


def _coerce_file_content(content) -> str:
    """v2.7.29: LLM potrafi zwrócić content jako dict/list/bool — koercja do str
    zamiast TypeError('data must be str, not ...') wybijającego cały krok."""
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, ensure_ascii=False, indent=2)
    return "" if content is None else str(content)


def _apply_patches(state: State, task: dict, worktree: Path, patches) -> int:
    """W-2 GATE (Search/Replace): aplikuje patche (diff semantyczny) zamiast nadpisywania plików.
    patch: {"path": ..., "search": ..., "replace": ...}. search musi wystąpić DOKŁADNIE raz.
    Zwraca liczbę zaaplikowanych patchy; problemy trafiają jako komentarz do zadania."""
    applied = 0
    problems: list[str] = []
    for pt in patches or []:
        rel = _resolve_task_rel(str(pt.get("path", "")), worktree)
        if not rel or (task.get("scope_paths") and rel not in task["scope_paths"]
                        and not _scope_autoextend(state, task, rel)):
            problems.append(f"{rel or pt.get('path')}: poza scope")
            continue
        target = worktree / rel
        if not target.is_file():
            problems.append(f"{rel}: brak pliku (patch do nieistniejącego pliku)")
            continue
        cur = target.read_text(encoding="utf-8")
        search, replace = str(pt.get("search", "")), str(pt.get("replace", ""))
        if not search or cur.count(search) != 1:
            problems.append(f"{rel}: search znaleziony {cur.count(search)}× (wymagane dokładnie 1×)")
            continue
        target.write_text(cur.replace(search, replace, 1), encoding="utf-8")
        applied += 1
    if problems:
        state.add_comment_report(task["id"], f"worker({task['role']})",
                                 "patch-search-replace", "; ".join(problems)[:300])
    return applied


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
    # v2.7.27: pusta odpowiedź LLM = PRAWIE zawsze karamy pełnym cyklem (pusty diff -> retry).
    # Teraz: natychmiastowy wewnętrzny retry z feedbackiem (max 2) — bez zmiany statusu.
    empty_retries = 0
    while not files and empty_retries < 2:
        empty_retries += 1
        state.record_metric("impl_empty_response", {"attempt": empty_retries}, task["id"])
        result = _llm_step(
            task, "2/5_IMPL",
            json.dumps({**payload,
                        "ERROR_FEEDBACK": f"Twoja poprzednia odpowiedź nie zawierała klucza 'files' "
                                          f"(próba {empty_retries + 1}/3). Zwróć PLIKI z implementacją wg planu — "
                                          f"pusty wynik oznacza nieudane zadanie."},
                       ensure_ascii=False),
            model=skill_compiler.model_for_role(task["role"], kind="strong"))
        files = result.get("files") or {}
        if not files and isinstance(result.get("artifacts"), list):
            for a in result["artifacts"]:
                if isinstance(a, dict) and a.get("path"):
                    files[a["path"]] = a.get("content", "")
    if result.get("signal") in ("ASK_SPONSOR", "ASK_OWNER"):  # ASK_OWNER = alias, graniczna decyzja Opiekuna
        _ask_sponsor(state, task, str(result.get("question", "")), result.get("options") or [])
        return
    if not files:
        raise RuntimeError("LLM trzykrotnie zwrócił pustą implementację (bez 'files') — przejściowy błąd LLM")
    for rel0, content in files.items():
        rel = _resolve_task_rel(rel0, worktree)  # v2.7.29: absolutna ścieżka LLM → worktree, Nie master!
        if rel is None:
            state.add_comment_report(task["id"], f"worker({task['role']})",
                                     f"zapisać plik `{rel0}`",
                                     "LLM zwrócił absolutną ścieżkę poza fabryką — wpis pominięty (ochrona mastera)")
            continue
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
        # W-2 GATE: istniejący plik >150 linii NIE może być nadpisany w całości (pętle QG!) —
        # dopuszczalny wyłącznie tryb SEARCH/REPLACE (patches)
        if target.is_file() and len(target.read_text(encoding="utf-8").splitlines()) > PATCH_OVERWRITE_LIMIT:
            state.add_comment_report(task["id"], f"worker({task['role']})",
                                     f"przepisać `{rel}` w całości — BLOKADA W-2",
                                     f"plik ma {len(target.read_text(encoding='utf-8').splitlines())} linii "
                                     f"(>{PATCH_OVERWRITE_LIMIT}) — użyj trybu patches (search/replace), pełne nadpisanie odrzucone")
            state.record_metric("overwrite_gate_rejected", {"path": rel}, task["id"])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_coerce_file_content(content), encoding="utf-8")
    # W-2: tryb Search/Replace — patche aplikowane PO zwykłych plikach
    n_patches = _apply_patches(state, task, worktree, result.get("patches"))
    if n_patches:
        _log(task["id"], f"W-2 patches: zaaplikowano {n_patches}")
    state.record_metric("step_time", {"step": "2/5_IMPL", "seconds": round(time.time() - t0, 2)}, task["id"])
    state.add_comment_report(task["id"], f"worker({task['role']})",
                             f"zaimplementować zmiany w zakresie {', '.join(task.get('scope_paths') or [])[:80]}",
                             f"zapisano {len(files)} plików — przechodzę do testów")
    _safe_transition(state, task["id"], "RUNNING", substep="3/5_TEST")


def step3_tests(state: State, task: dict, worktree: Path) -> None:
    """Krok 3 (LLM, ~90s): testy jednostkowe + integracyjne względem acceptance_criteria."""
    t0 = time.time()
    code_files = {p: (worktree / p).read_text(encoding="utf-8")
                  for p in task["scope_paths"] if (worktree / p).is_file()}  # v2.7.29: is_file, nie exists (katalog w scope = IsADirectoryError)
    payload = {"code": code_files, "acceptance_criteria": task["acceptance_criteria"],
               "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"test_files": {"tests/<plik>_test.py": "<kod testu>"}}. '
                         'Żadnych innych kluczy, żadnego tekstu poza JSON.'}
    result = _llm_step(task, "3/5_TEST", json.dumps(payload, ensure_ascii=False),
                       model=skill_compiler.model_for_role(task["role"], kind="fast"))
    test_files = result.get("test_files") or {}
    for rel0, content in test_files.items():
        rel = _resolve_task_rel(rel0, worktree)  # v2.7.29: normalizacja absolutnych ścieżek
        if rel is None:
            state.record_metric("llm_path_rejected", {"path": rel0, "step": "3/5_TEST"}, task["id"])
            continue
        target = worktree / rel
        if target.exists() and target.is_dir():
            # spójnie ze step2: LLM zwrócił katalog zamiast pliku — pomijamy z logiem,
            # zamiast wybijać całe zadanie IsADirectoryError(21)
            state.add_comment_report(task["id"], f"worker({task['role']})",
                                     f"zapisać test `{rel}`",
                                     f"LLM zwrócił katalog zamiast pliku testu — pomijam wpis (auto-heal), reszta idzie dalej")
            state.record_metric("llm_dir_instead_of_file", {"path": rel, "step": "3/5_TEST"}, task["id"])
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_coerce_file_content(content), encoding="utf-8")
    state.record_metric("step_time", {"step": "3/5_TEST", "seconds": round(time.time() - t0, 2)}, task["id"])
    _safe_transition(state, task["id"], "RUNNING", substep="4/5_QG")


def step4_quality_gate(state: State, task: dict, worktree: Path, base: str) -> None:
    """Krok 4 (Python + LLM): PRE-FLIGHT samokonta + lokalny QG; przy błędzie max 3 próby fix (Krok 4b)."""
    t0 = time.time()
    # v2.7.27 — PRE-FLIGHT: wykonawca łapie swoje błędy SAM (składnia/JSON/nowe testy),
    # zanim padnie QG i kosztuje pełny cykl. Naprawa na miejscu (LLM, max 2 próby).
    _run_preflight(state, task, worktree)
    for attempt in range(1, MAX_LOCAL_FIXES + 2):
        results = quality_gates.local_gates(worktree)
        failed = [r for r in results if not r["ok"]]
        state.record_metric("local_qg", {"attempt": attempt, "failed": [r["name"] for r in failed]}, task["id"])
        if not failed:
            state.record_metric("step_time", {"step": "4/5_QG", "seconds": round(time.time() - t0, 2)}, task["id"])
            _safe_transition(state, task["id"], "RUNNING", substep="5/5_COMMIT")
            return
        if attempt > MAX_LOCAL_FIXES:
            _safe_transition(state, task["id"], FAILED, review_notes=json.dumps(
                {"stage": "QG", "failures": failed}, ensure_ascii=False))
            return
        # Krok 4b: LLM dostaje WYŁĄCZNIE traceback i konkretny plik
        fix_payload = {"failures": [
            {"name": r["name"], "tool": r.get("tool"), "output": (r.get("output") or "")[-4000:]}
            for r in failed]}
        try:
            fix = _llm_step(task, "4b_FIX", json.dumps(fix_payload, ensure_ascii=False),
                            model=skill_compiler.model_for_role(task["role"], kind="fast"))
            for rel0, content in fix.get("files", {}).items():
                rel = _resolve_task_rel(rel0, worktree)  # v2.7.29: normalizacja
                if rel is None:
                    continue
                if rel not in task["scope_paths"]:
                    if not _scope_autoextend(state, task, rel):  # v2.7.27: rutyna — bez eskalacji
                        continue
                    task = state.get_task(task["id"])
                if (worktree / rel).is_dir():
                    continue
                (worktree / rel).write_text(_coerce_file_content(content), encoding="utf-8")
        except RuntimeError:
            _safe_transition(state, task["id"], FAILED, review_notes="LLM niedostępny w kroku 4b")
            return
    _safe_transition(state, task["id"], FAILED, review_notes="QG nieprzechodzony po lokalnych poprawkach")


def _run_preflight(state: State, task: dict, worktree: Path) -> None:
    """v2.7.27: pre-flight (tanie deterministyczne kontrole) + LLM-naprawa na miejscu.
    Błąd tu NIE odbija zadania — wykonawca domyka go sam (zasada: sam wychwytaj swoje błędy)."""
    from engine import preflight
    for attempt in range(1, 4):  # 1 przejście + 2 naprawy LLM
        report = preflight.run(worktree)
        state.record_metric("preflight", {"attempt": attempt, "ok": report["ok"],
                                          "n_problems": len(report["problems"])}, task["id"])
        if report["ok"]:
            return
        # szybkie i pewne rozwiązanie bez LLM: niepoprawna składnia/JSON — LLM dostaje
        # dokładny problem i poprawia plik; zawartość zmian pilnuje wyłącznie scope
        digest = preflight.problems_digest(report)
        state.add_comment_report(task["id"], f"worker({task['role']})",
                                 "sprawdzić własną pracę samemu (pre-flight: składnia/JSON/testy)",
                                 f"pre-flight wyłapał {len(report['problems'])} problem(ów) — naprawiam od razu, "
                                 f"przed review i QG (próba {attempt}/3)")
        try:
            fix = _llm_step(task, "4a_PREFLIGHT",
                            json.dumps({"problems": report["problems"],
                                        "changed_files": report["changed"],
                                        "scope_paths": task["scope_paths"],
                                        "SCHEMA": 'Popraw wskazane pliki i zwróć wyłącznie JSON: '
                                                  '{"files": {"<path ze scope_paths>": "<poprawiona pełna treść>"}}. '
                                                  'Żadnego innego klucza.'},
                                       ensure_ascii=False),
                            model=skill_compiler.model_for_role(task["role"], kind="fast"))
            for rel0, content in (fix.get("files") or {}).items():
                rel = _resolve_task_rel(rel0, worktree)  # v2.7.29: normalizacja absolutnych ścieżek
                if rel is None:
                    continue
                # v2.7.27: spójnie ze step2 — plik poza scope auto-rozszerza scope (rutyna, nie eskalacja)
                if task["scope_paths"] and rel not in task["scope_paths"]:
                    if not _scope_autoextend(state, task, rel):
                        continue  # sekret/limit — QG i tak zweryfikuje
                    task = state.get_task(task["id"])
                if (worktree / rel).is_dir():
                    continue
                (worktree / rel).write_text(_coerce_file_content(content), encoding="utf-8")
            state.record_metric("preflight_fix", {"attempt": attempt}, task["id"])
        except RuntimeError:
            return  # LLM niedostępny — Zwykłe QG wyłapi to dalej
    # po 2 naprawach nie blokujemy — QG da ostateczny wyrok (pełne testy, nie tylko zmienione)


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
    _safe_transition(state, task["id"], "RUNNING", substep="4.5_SIMPLIFY")
    payload = {"code": code, "diff_lines": lines,
               "SCHEMA": 'Zwróć wyłącznie kompaktowy JSON: {"files": {"<path ze scope_paths>": "<uproszczona treść>"}}. '
                         'TA SAMA logika i testy, idiomatycznie, ok. 30% mniej linii. Żadnego innego klucza.'}
    try:
        res = _llm_step(task, "4.5_SIMPLIFY", json.dumps(payload, ensure_ascii=False),
                        model=skill_compiler.model_for_role(task["role"], kind="strong"))
    except RuntimeError:
        return  # LLM niedostępny — pomijamy uproszczenie
    files = res.get("files") or {}
    files = {_resolve_task_rel(p, worktree) or p: c for p, c in files.items()
             if (_resolve_task_rel(p, worktree) or p) in task["scope_paths"]}
    if not files:
        return
    backup = {p: (worktree / p).read_text(encoding="utf-8") for p in files if (worktree / p).is_file()}
    for rel, content in files.items():
        if (worktree / rel).is_dir():  # v2.7.29: katalog zamiast pliku — skip (jak w step2/step3)
            continue
        (worktree / rel).write_text(_coerce_file_content(content), encoding="utf-8")
    results = quality_gates.local_gates(worktree)
    if any(not r["ok"] for r in results if not r.get("skipped")):
        for p, c in backup.items():  # revert
            (worktree / p).write_text(_coerce_file_content(c), encoding="utf-8")
        state.record_metric("simplify_reverted", {"task": task["id"]}, task["id"])
        _log(task["id"], "simplify: QG po uproszczeniu padło -> revert")
        return
    after = git_guard.diff_line_count(worktree, base)
    state.record_metric("simplify_applied", {"before": lines, "after": after}, task["id"])
    _log(task["id"], f"simplify: {lines} -> {after} linii diffu")
    _safe_transition(state, task["id"], "RUNNING", substep="5/5_COMMIT")


def step5_commit(state: State, task: dict, worktree: Path, base: str) -> None:
    """Krok 5 (Python): scope_paths + limit diffu + commit + push -> IN_REVIEW."""
    reverted = git_guard.enforce_scope(worktree, task["scope_paths"])
    if reverted:
        state.record_metric("scope_revert", {"files": reverted}, task["id"])
    try:
        lines = git_guard.assert_diff_limit(worktree, base)  # zmiana #7
    except git_guard.DiffTooLarge as exc:
        git_guard.run_git(["checkout", "--", "."], cwd=worktree, check=False)
        _safe_transition(state, task["id"], READY,
                         review_notes=f"Odrzucono przed CR: {exc}")
        return
    if lines == 0:
        # QG jest już zielone (krok 5 jest za bramką). Pusty diff — zmiany siedzą w branchu
        # albo nie ma czego dopisać — idzie do Code Review od pierwszej próby.
        # Wcześniejszy próg retry>=1 zrzucał zadanie do READY i kręcił pętlę (empty_diff_rejected).
        state.add_comment_report(task["id"], f"worker({task['role']})",
                                 "przekazać do Code Review mimo pustego diffu",
                                 "diff zerowy (zmiany już siedzą w branchu albo nie ma nowej treści), "
                                 "QG zielone — oddaję decyzję Code Review")
        _safe_transition(state, task["id"], IN_REVIEW, substep="done")
        _log(task["id"], "pusty diff + QG zielone → IN_REVIEW")
        state.record_metric("empty_diff_to_review", {}, task["id"])
        return
    how = git_guard.commit_task(worktree, task["id"], f"feat({task['id'].lower()}): {task['title']}")
    if how == "amended":
        state.record_metric("commit_amended", {"branch": task["branch"]}, task["id"])
    try:
        git_guard.sync_with_origin(worktree, base)  # twarda reguła: wyrównaj z origin przed pushem
    except git_guard.MergeConflict as exc:
        _safe_transition(state, task["id"], READY,
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
    _safe_transition(state, task["id"], IN_REVIEW, substep="done")
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
    pf_feedback_count = 0  # v2.7.27: ile razy pre-flight zwrócił pracę agentowi przed przyjęciem done
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
            rel0 = str(resp.get("path", ""))
            rel = _resolve_task_rel(rel0, worktree) or rel0  # v2.7.29: normalizacja
            if rel != rel0:
                history.append(f"t{turn} write_file: zmapowano '{rel0}' -> '{rel}' (ścieżka absolutna)")
            p = (worktree / rel).resolve()
            if not str(p).startswith(wt_resolved + os.sep) and str(p) != wt_resolved:
                history.append(f"t{turn} write_file ODRZUCONE (poza worktree): {rel}")
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            content = str(resp.get("content", ""))
            if p.is_dir():  # v2.7.29: path = katalog — nie wybijać zadania IsADirectoryError
                history.append(f"t{turn} write_file ODRZUCONE (katalog zamiast pliku): {rel}")
                _log(tid, f"LLM t{turn} write_file odrzucone (katalog): {rel}")
                continue
            p.write_text(_coerce_file_content(content), encoding="utf-8")
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
            # v2.7.27: pre-flight PRZED przyjęciem done — wykonawca sam łapie składnię/JSON/
            # zepsute własne testy. Problemy wracają do agenta jako feedback (max 2 razy):
            # „nie kłóć się z maszyną, popraw i zgłoś done ponownie".
            from engine import preflight
            pf = preflight.run(worktree)
            if not pf["ok"] and pf_feedback_count < 2:
                pf_feedback_count += 1
                history.append(f"t{turn} PREFLIGHT: co najmniej 1 problem — NAPRAW i daj done ponownie:\n"
                               f"{preflight.problems_digest(pf)}")
                state.record_metric("preflight_agent_loop", {"task": tid, "attempt": pf_feedback_count}, tid)
                _log(tid, f"pre-flight zwrócił pracę agentowi (problemów: {len(pf['problems'])})")
                continue
            if not pf["ok"]:
                _log(tid, f"pre-flight: {len(pf['problems'])} problemów po 2 zwrotach — przyjmuję done (QG wykaże)")
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
    _safe_transition(state, tid, "RUNNING", substep="LLM_EXEC")
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
        _safe_transition(state, tid, IN_REVIEW, substep="done")
        return
    step5_commit(state, task, worktree, base)


_PIPELINE = ("1/5_PLAN", "2/5_IMPL", "3/5_TEST", "4/5_QG", "4.5_SIMPLIFY", "5/5_COMMIT")


def _should_run(resume: str, step: str) -> bool:
    """Wznowienie wykonuje krok `step` i wszystko po nim. Wcześniejsze etapy zostają.

    "done" to paczka już oddana — zostaje sam commit (albo wyjście do review), nie plan od zera.
    Nieznany marker (LLM_EXEC, pusty) puszcza całą ścieżkę."""
    r = "5/5_COMMIT" if resume == "done" else (resume or "")
    if r not in _PIPELINE:
        return True
    return _PIPELINE.index(step) >= _PIPELINE.index(r)


def run(task_id: str) -> int:
    state = State()
    task = state.claim_task(task_id, claimed_by=f"worker-{os.getpid()}")
    if task is None:
        _log(task_id, "zadanie niedostępne do przejęcia")
        return 1
    
    # 🔥 Użyj resume_substep z task (substep > last_completed_step > max_substep)
    resume_step = resume_substep(task)
    _log(task_id, f"Resuming from: {resume_step}")
    
    # Ustaw substep, jeśli wznawiamy od kroku innego niż 0/5_PREPARE
    if resume_step and resume_step != "0/5_PREPARE":
        try:
            state.transition(task_id, RUNNING, substep=resume_step,
                           review_notes=f"{(task.get('review_notes') or '')} | Resumed from {resume_step}")
            task = state.get_task(task_id)
        except Exception:
            pass
    
    telegram_reporter.ensure_topic(state, task)
    streamer = ConsoleStreamer(task_id, state)
    streamer.log_meta("init", f"worker-{os.getpid()} claim task {task_id} (RESUMING from {resume_step})"
                      if resume_step and resume_step != "0/5_PREPARE" else
                      f"worker-{os.getpid()} claim task {task_id}")
    
    worktree: Path | None = None
    try:
        # Pomijaj step0_prepare, jeśli wznawiamy od późniejszego kroku
        if resume_step not in ("0/5_PREPARE", "", None):
            streamer.log_meta("0/5_PREPARE", f"step0_prepare SKIPPED (resuming from {resume_step})")
            existing_worktree = task.get("worktree")
            if existing_worktree and Path(existing_worktree).exists():
                worktree = Path(existing_worktree)
                _log(task_id, f"✅ using existing worktree: {worktree}")
            else:
                _log(task_id, "⚠️ worktree missing, recreating...")
        
        if worktree is None:
            streamer.log_meta("0/5_PREPARE", "step0_prepare start")
            worktree = step0_prepare(state, task)
        state.set_kv(f"last_successful_step:{task_id}", "0/5_PREPARE")
        _tg(state, task_id, "worktree gotowy")
        base = state.get_milestone(task["milestone"])["branch"]
        task = state.get_task(task_id)  # świeży wiersz (branch z step0)
        if (task.get("mode") or "pipeline") == "llm":
            streamer.log_meta("LLM_EXEC", "step_llm start")
            step_llm(state, task, worktree, base)
            task = state.get_task(task_id)
            if task and task["status"] == "IN_REVIEW":
                _tg(state, task_id, "LLM wykonał zadanie → IN_REVIEW (czeka na Code Review)")
            streamer.log_meta("LLM_EXEC", "step_llm done")
            return 0
        if _should_run(resume_step, "1/5_PLAN"):
            streamer.log_meta("1/5_PLAN", "step1_plan start")
            step1_plan(state, task)
            state.set_kv(f"last_successful_step:{task_id}", "1/5_PLAN")
            task = state.get_task(task_id)  # split może zmienić status
            if task["status"] != "RUNNING":
                telegram_reporter.post_event(state, task, "✂️ SPLIT_TASK — zadanie podzielone na podzadania (patrz karta)")
                streamer.log_meta("1/5_PLAN", "SPLIT_TASK or non-running")
                return 0
            if _stage_stop(state, task_id, "1/5_PLAN"):
                streamer.log_meta("1/5_PLAN", "stage_stop")
                return 0
            _tg(state, task_id)
        else:
            streamer.log_meta("1/5_PLAN", f"skipped (resume {resume_step})")
        if _should_run(resume_step, "2/5_IMPL"):
            streamer.log_meta("2/5_IMPL", "step2_implement start")
            step2_implement(state, task, worktree)
            state.set_kv(f"last_successful_step:{task_id}", "2/5_IMPL")
            task = state.get_task(task_id)
            if task["status"] != "RUNNING":  # np. AWAITING_OWNER — czekamy na decyzję Opiekuna
                _tg(state, task_id)
                streamer.log_meta("2/5_IMPL", f"status transition: {task['status']}")
                return 0
            if _stage_stop(state, task_id, "2/5_IMPL"):
                streamer.log_meta("2/5_IMPL", "stage_stop")
                return 0
            _tg(state, task_id)
        else:
            streamer.log_meta("2/5_IMPL", f"skipped (resume {resume_step})")
        if _should_run(resume_step, "3/5_TEST"):
            streamer.log_meta("3/5_TEST", "step3_tests start")
            step3_tests(state, task, worktree)
            state.set_kv(f"last_successful_step:{task_id}", "3/5_TEST")
            if _stage_stop(state, task_id, "3/5_TEST"):
                streamer.log_meta("3/5_TEST", "stage_stop")
                return 0
            _tg(state, task_id)
        else:
            streamer.log_meta("3/5_TEST", f"skipped (resume {resume_step})")
        if _should_run(resume_step, "4/5_QG"):
            streamer.log_meta("4/5_QG", "step4_quality_gate start")
            step4_quality_gate(state, task, worktree, base)
            state.set_kv(f"last_successful_step:{task_id}", "4/5_QG")
            task = state.get_task(task_id)
            if task["status"] != "RUNNING":
                telegram_reporter.post_event(state, task, f"❌ lokalny QG nie przeszedł: {(task.get('review_notes') or '')[:300]}")
                _tg(state, task_id)
                streamer.log_meta("4/5_QG", "QG failed")
                return 0
            if _stage_stop(state, task_id, "4/5_QG"):
                streamer.log_meta("4/5_QG", "stage_stop")
                return 0
        else:
            streamer.log_meta("4/5_QG", f"skipped (resume {resume_step})")
        if _should_run(resume_step, "4.5_SIMPLIFY"):
            streamer.log_meta("4.5_SIMPLIFY", "step45_simplify start")
            step45_simplify(state, task, worktree, base)
        task = state.get_task(task_id)
        if task["status"] == "RUNNING":
            if _stage_stop(state, task_id, "4.5_SIMPLIFY"):
                streamer.log_meta("4.5_SIMPLIFY", "stage_stop")
                return 0
            streamer.log_meta("5/5_COMMIT", "step5_commit start")
            step5_commit(state, task, worktree, base)
        task = state.get_task(task_id)
        if task and task["status"] == "IN_REVIEW":
            _tg(state, task_id, "commit OK → IN_REVIEW (czeka na Code Review)")
            streamer.log_meta("5/5_COMMIT", "commit OK -> IN_REVIEW")
        return 0
    except Exception as exc:  # deterministyczny fallback — nigdy cicha śmierć
        # v2.7.29: pełny traceback do loga — bez tego błędy typu OperationalError/TypeError
        # w pętli są niemożliwe do namierzenia (sam repr jest za mało)
        import traceback as _tb
        _log(task_id, "TRACEBACK:\n" + _tb.format_exc())
        
        # Zapisz ostatni udany etap do KV, aby orkiestrator mógł restartować od niego
        last_step = state.get_kv(f"last_successful_step:{task_id}", "")
        if last_step:
            state.set_kv(f"worker_error:{task_id}", f"{exc!r}")
            state.set_kv(f"resume_from_step:{task_id}", last_step)
            streamer.log_meta("ERROR", f"Exception: {exc!r} - RESUME FROM: {last_step}")
        else:
            streamer.log_meta("ERROR", f"Exception: {exc!r}")
        
        state.transition(task_id, COOLING, review_notes=repr(exc))
        t = state.get_task(task_id)
        if t:
            telegram_reporter.post_event(state, t, f"💥 FAILED: `{exc!r}` - Ostatni udany etap: {last_step or 'brak'}")
            telegram_reporter.update_progress(state, t)
        _log(task_id, f"FAILED: {exc!r}")
        return 2
    finally:
        try:
            streamer.close()
        except Exception:
            pass
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

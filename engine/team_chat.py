"""Team Chat — wiadomość Sponsora na Telegramu (poza komendami menu) leci do drużyny:

  1. routing pytania do roli (keywordy -> fallback: klasyfikacja LLM fast-model)
  2. odpowiedź bota tej roli (pełny prompt roli ze skilli + historia wątku)
  3. odpowiedź wraca na Telegram Z PODPISEM: kto (rola) i jaki model odpisał
  4. kolejna wiadomość = follow-up w tym samym wątku (ta sama rola, historia w kv)
     komenda "nowe pytanie" / "new" / "reset" — kasuje wątek i czyści routing

Uruchamiane jako osobny proces (nie blokuje ticka orkiestratora):
  python -m engine.team_chat <chat_id> <tekst>
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import personas, skill_compiler, telegram_gate
from engine.llm_gateway import get_gateway
from engine.state import State

ROUTE_KEYWORDS: list[tuple[list[str], str]] = [
    (["security", "bezpieczeńst", "vulnerab", "secret", "injection", "owasp", "sast"], "security-auditor"),
    (["architektur", "architect", "adr", "kontrakt", "openapi", "schema", "design"], "solutions-architect"),
    (["plan", "dekompozycj", "podziel", "roadmap", "prioryt", "milestone"], "tech-lead-planner"),
    (["test", "pytest", "coverage", "qg", "lint"], "backend-dev"),
    (["frontend", "react", "ui", "css", "remotion", "animac", "wideo"], "frontend-dev"),
    (["deploy", "sre", "docker", "ci/", "staging", "prod", "worktree", "git", "pipeline"], "sre-deployer"),
    (["review", "code review", "cr ", "diff", "scope_paths"], "code-reviewer"),
    (["debug", "bug", "błąd", "traceback", "nie działa", "pada", "wyjątek"], "principal-debugger"),
    (["biznes", "prd", "wymagania", "product", "cennik", "stripe", "cena"], "product-owner"),
    (["audyt", "audit", "struktura", "co jest w repo", "jak działa"], "systems-auditor"),
]

FALLBACK_ROLES = ["systems-auditor", "solutions-architect", "backend-dev", "product-owner"]


def route_role(question: str, history: list[dict]) -> tuple[str, str]:
    q = question.lower()
    for keys, role in ROUTE_KEYWORDS:
        if any(k in q for k in keys):
            return role, "keyword"
    roles = list(skill_compiler.load_registry().get("roles", {}).keys()) or FALLBACK_ROLES
    try:
        gw = get_gateway()
        out = gw.complete_json(
            system=('Z treści polecenia Sponsora wyciągnij specyfikację zadania dla fabryki. '
                    'Odpowiedz WYŁĄCZNIE JSON: {"title": "...", "acceptance_criteria": "...", "scope_hint": "..."}. '
                    'Tytuł po polsku, max 90 znaków.'),
            user=json.dumps({"pytanie": question, "ostatnia_historia": history[-4:]},
                            ensure_ascii=False),
            step="ROUTE", model="fast-model")
        role = out.get("role")
        if role in roles:
            return role, "llm"
    except Exception:
        pass
    return "systems-auditor", "fallback"


ACTION_CREATE_TASK = ["stwórz zadanie", "stworz zadanie", "dodaj zadanie", "dodaj task",
                      "create task", "zróbcie zadanie", "zrobcie zadanie", "rozpisz zadanie",
                      "napraw to jako zadanie", "stwórz task"]
ACTION_ADD_RULE = ["dodaj do globalnej reguły", "dodaj globalną regułę", "dodaj globalna regule",
                   "dodaj regułę", "dodaj regule", "zapamiętaj od teraz", "pamiętaj od teraz",
                   "add global rule", "globalna zasada"]


def build_state_summary(state: State) -> str:
    """Kompaktowy snapshot fabryki — doklejany do każdego promptu team chatu,
    żeby pytania typu 'jaki jest stan projektu' miały odpowiedź z żywych danych."""
    from engine import projects as _projects
    lines = [f"Aktywny projekt: {_projects.active_project_id()}"]
    tasks = state.list_tasks()
    by_status: dict[str, list[str]] = {}
    for t in tasks:
        by_status.setdefault(t["status"], []).append(t["id"])
    for st, ids in sorted(by_status.items()):
        lines.append(f"  {st}: {len(ids)} — {', '.join(ids[:8])}{'…' if len(ids) > 8 else ''}")
    for ms in state.list_milestones():
        lines.append(f"  mileston {ms['id']} [{ms['status']}] ({ms.get('project_id')}): {str(ms.get('goal'))[:70]}")
    comments = state.recent_comments(3)
    if comments:
        lines.append("  ostatnie komentarze: " + " | ".join(f"{c['author']}: {c['text'][:60]}" for c in comments))
    return "\n".join(lines)


def _extract_after(text: str, keywords: list[str]) -> str:
    low = text.lower()
    for k in keywords:
        i = low.find(k)
        if i >= 0:
            return text[i + len(k):].strip(" :-–—")
    # fallback dla luźnych wzorców: wytnij od rzeczownika (reguła/zadanie)
    for k in ("reguły", "regule", "zasad", "zadanie", "zadania", "task"):
        i = low.find(k)
        if i >= 0:
            return text[i + len(k):].strip(" :-–—") or text
    return text


def main() -> int:
    chat_id, text = sys.argv[1], sys.argv[2]
    state = State()
    low = text.lower()

    # lock per czat — jedna odpowiedź na raz; druga wiadomość podczas generowania dostaje info
    import fcntl
    lock_f = open(ROOT / ".agents" / "logs" / f"team_chat.{chat_id}.lock", "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        telegram_gate._call("sendMessage", {"chat_id": chat_id,
            "text": "🤖 Poprzednia odpowiedź jeszcze się generuje — poczekaj na nią (albo napisz «nowe pytanie»)."})
        return 0

    # --- tryb LLM: "llm <opis>" -> zadanie wykonywane w 100% przez LLM (vibe z narzędziami) ---
    if low == "llm" or low.startswith("llm ") or low.startswith("llm\t"):
        desc = text[3:].strip()
        if not desc:
            telegram_gate._call("sendMessage", {"chat_id": chat_id, "text": (
                "Użycie: `llm <opis zadania>`\n"
                "Opisz co LLM ma zrobić — zadanie wykona samodzielnie (vibe z włączonymi narzędziami: "
                "bash, edycja plików), a na końcu sam dopisze raport dla drużyny "
                "(docs/llm-reports/<zadanie>.md + komentarz w feedzie).").replace("*", "")})
            return 0
        from engine import projects as _projects
        n = state.get_kv("chat_task_seq", 0) + 1
        state.set_kv("chat_task_seq", n)
        tid = f"CHAT-{n:04d}"
        # zadania LLM piszą do fabryki (repo="factory") i mają iść od razu — projekt self ma approval_policy=auto
        pid = "mikowhy-teams-self" if (p := _projects.AGENTS / "projects" / "mikowhy-teams-self" / "project.json").exists() else _projects.active_project_id()
        state.add_milestone("CHAT-OPS", "Zadania tworzone przez Sponsora z czatu (Telegram team chat)",
                            "milestone/chat-ops", project_id=pid)
        prio = "P0" if any(k in low for k in ("pilne", "pilny", "pilna", "asap", "natychmiast", "od razu")) else "P2"
        from engine.llm_gateway import make_sponsor_title
        sponsor_title = make_sponsor_title(desc[:90], desc)
        state.add_task(tid, milestone="CHAT-OPS", phase=4, title=desc[:90], role="llm-executor",
                       title_sponsor=sponsor_title,
                       scope_paths=[], depends_on=[],
                       acceptance_criteria=(f"Zadanie wykonane w 100% przez LLM; raport dla drużyny w "
                                            f"docs/llm-reports/{tid}.md; zmiany zacommitowane na branchu feature/{tid.lower()}"),
                       project_id=pid, repo="factory", status="AWAITING_APPROVAL", read_only=False, mode="llm",
                       priority=prio)
        state.set_kv(f"llm_desc:{tid}", desc)  # pełny opis Sponsora (tytuł jest skrócony do 90 znaków)
        telegram_gate._call("sendMessage", {"chat_id": chat_id, "text": (
            f"🤖 Stworzyłem zadanie {tid} w trybie LLM (wykona je sam vibe z narzędziami, "
            f"projekt: {pid}, status: AWAITING_APPROVAL — wg polityki albo samo pójdzie dalej, albo czeka na klik).\n"
            f"Opis: {desc[:200]}\nŚledź: `list` / dashboard :8765").replace("*", "")})
        state.record_metric("team_chat_llm_task", {"task": tid, "desc": desc[:200]}, tid)
        return 0

    # --- akcja: dodaj globalną regułę (sponsor-notes.md — wstrzykiwane do KAŻDEGO promptu) ---
    is_add_rule = (any(k in low for k in ("reguł", "regule", "zasad", "rule", "pamiętaj", "zapamiętaj"))
                   and any(k in low for k in ("dodaj", "add")))
    if is_add_rule or any(k in low for k in ACTION_ADD_RULE):
        rule = _extract_after(text, ACTION_ADD_RULE)
        # wytnij od "żeby..." jeśli jest — reszta to treść reguły
        i = rule.lower().find("żeby")
        if i >= 0:
            rule = rule[i + len("żeby"):].strip(" :-–—")
        rule = rule.strip(" :-–—.") or text
        from engine.orchestrator import cmd_note
        cmd_note("ALL", rule)
        telegram_gate._call("sendMessage", {"chat_id": chat_id,
            "text": f"📌 Dodałem do GLOBALNYCH reguł (sponsor-notes.md, leci do każdego promptu botów):\n{rule}".replace("*", "")})
        state.record_metric("team_chat_rule", {"rule": rule[:200]}, None)
        return 0

    # --- akcja: stwórz zadanie w fabryce ---
    is_create_task = (any(k in low for k in ("zadanie", "zadania", "task"))
                      and any(k in low for k in ("stwórz", "stworz", "dodaj", "zrób", "zrob", "rozpisz", "create", "napraw")))
    if is_create_task or any(k in low for k in ACTION_CREATE_TASK):
        from engine import projects as _projects
        role, how = route_role(text, [])
        gw = get_gateway()
        spec = gw.complete_json(
            system="Z tresci polecenia Sponsora wyciagnij specyfikacje zadania. "
                    "Odpowiedz WYLACZNIE JSON: {title, acceptance_criteria, scope_hint}. "
                    "Tytul po polsku, max 90 znakow.",
            user=text, step="TASK_SPEC", model="fast-model")
        title = str(spec.get("title") or text)[:90]
        acc = spec.get("acceptance_criteria")
        if not isinstance(acc, str):
            acc = json.dumps(acc, ensure_ascii=False) if acc else "Zadanie wykonane, przeszło lokalny QG i Code Review"
        acc = acc[:500]
        n = state.get_kv("chat_task_seq", 0) + 1
        state.set_kv("chat_task_seq", n)
        tid = f"CHAT-{n:04d}"
        pid = _projects.active_project_id()
        state.add_milestone("CHAT-OPS", "Zadania tworzone przez Sponsora z czatu (Telegram team chat)",
                            "milestone/chat-ops", project_id=pid)
        from engine.llm_gateway import make_sponsor_title
        state.add_task(tid, milestone="CHAT-OPS", phase=4, title=title, role=role,
                       scope_paths=[], depends_on=[], acceptance_criteria=acc,
                       project_id=pid, repo="factory", status="AWAITING_APPROVAL", read_only=False,
                       title_sponsor=make_sponsor_title(title, text))
        telegram_gate._call("sendMessage", {"chat_id": chat_id,
            "text": (f"✅ Stworzyłem zadanie {tid} (rola: {role}, projekt: {pid}, status: AWAITING_APPROVAL; "
                     f"zgodnie z polityką projektu zadanie albo samo pójdzie dalej, albo poczeka na Twój klik). "
                     f"Tytuł: {title}\nŚledź: `list` / dashboard :8765").replace("*", "")})
        state.record_metric("team_chat_task", {"task": tid, "role": role}, tid)
        return 0
    key = f"menu:{chat_id}:thread"
    thread = state.get_kv(key, {"role": None, "history": []}) or {"role": None, "history": []}
    history: list[dict] = thread.get("history", [])

    if thread.get("role"):
        role, how = thread["role"], "kontynuacja wątku"
    else:
        role, how = route_role(text, history)

    task = {"id": "TEAM-CHAT", "title": text[:80], "role": role, "status": "RUNNING",
            "milestone": "chat", "phase": 0, "scope_paths": [], "depends_on": [],
            "acceptance_criteria": "", "retry_count": 0, "review_notes": "",
            "project_id": "mikowhy-teams", "repo": "factory", "read_only": True}
    system = skill_compiler.compile_system_prompt(role, stack=None, task=task)
    system += "\n\n# Aktualny stan fabryki (live, z state.db):\n" + build_state_summary(state)
    system += ("\n\n# Styl odpowiedzi: odpowiadaj PO POLSKU, zwięźle, prozą (kilka zdań/bulletów), "
               "chyba że Sponsor wprost poprosi o JSON/kod.")
    if history:
        system += ("\n\n# Poprzednia rozmowa w tym wątku (kontekst — odpowiadaj spójnie, "
                   "możesz odnieść się do wcześniejszych odpowiedzi):\n"
                   + "\n".join(f"- {h.get('author')}: {str(h.get('text'))[:400]}" for h in history[-6:]))

    gw = get_gateway()
    model = skill_compiler.model_for_role(role, kind="fast")
    try:
        answer = gw.complete(system=system, user=text, step="TEAM_CHAT", model=model)
    except Exception as exc:
        answer = f"(nie udało się uzyskać odpowiedzi: {exc!r})"

    p = personas.persona_for_role(role)
    telegram_gate._call("sendMessage", {
        "chat_id": chat_id,
        "text": f"👤 {p['name']} — {p['position']} ({p['gender']}, rola: {role}, model: {model}) odpowiada [{how}]:\n\n{answer}".replace("*", ""),
    })
    history.append({"author": "sponsor", "text": text})
    history.append({"author": role, "text": answer[:2000]})
    state.set_kv(key, {"role": role, "history": history[-20:], "model": model})
    state.record_metric("team_chat", {"role": role, "model": model, "how": how, "q": text[:150]}, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

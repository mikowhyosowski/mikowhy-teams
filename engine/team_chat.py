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

from engine import skill_compiler, telegram_gate
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
            system=("Wybierz rolę najlepiej odpowiadającą pytaniu Sponsora. "
                    "Odpowiedz WYŁĄCZNIE JSON: {\"role\": \"<jedna z listy>\"}. "
                    "Dostępne role: " + ", ".join(roles)),
            user=json.dumps({"pytanie": question, "ostatnia_historia": history[-4:]},
                            ensure_ascii=False),
            step="ROUTE", model="fast-model")
        role = out.get("role")
        if role in roles:
            return role, "llm"
    except Exception:
        pass
    return "systems-auditor", "fallback"


def main() -> int:
    chat_id, text = sys.argv[1], sys.argv[2]
    state = State()
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

    telegram_gate._call("sendMessage", {
        "chat_id": chat_id,
        "text": f"👤 {role} (model: {model}) odpowiada [{how}]:\n\n{answer}".replace("*", ""),
    })
    history.append({"author": "sponsor", "text": text})
    history.append({"author": role, "text": answer[:2000]})
    state.set_kv(key, {"role": role, "history": history[-20:], "model": model})
    state.record_metric("team_chat", {"role": role, "model": model, "how": how, "q": text[:150]}, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

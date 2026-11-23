"""Workload Guardian (v2.7.26) — godzinalny obchód fabryki.

Rola: workload-guardian (People Ops — strażnik obłożenia pracą).
Obowiązek (hourly): sprawdzić, czy każdy agent (rola z org configu) ma pracę
(RUNNING / READY / IN_REVIEW / ... ). Rola bezczynna > 30 min i bez żadnego
otwartego zadania dostaje zadanie-plan „zaplanuj sobie pracę wg obowiązków
stanowiska" — budowane z opisu duties/description w hierarchii org.
Wynik obchodu: powiadomienie systemowe (feed Opiekuna) + metryka.

Zasada Sponsora: Opiekun ma żyć łatwo — guardian działa w tle i tylko raportuje;
nie eskaluje, nie zatrzymuje pracy, jedynie domyka otwarte dziury w obłożeniu.
"""
from __future__ import annotations

import time

from engine.flow import org as org_mod
from engine.state import (
    AWAITING_OWNER,
    CHANGES_REQUESTED,
    COOLING,
    FAILED,
    IN_REVIEW,
    READY,
    RUNNING,
    State,
)

IDLE_S = 30 * 60  # 30 minut bezczynności = sygnał do zaplanowania pracy

# statusy oznaczające, że rola ma (lub zaraz dostanie) pracę
ACTIVE_STATUSES = (READY, RUNNING, IN_REVIEW, CHANGES_REQUESTED, AWAITING_OWNER, COOLING)

# max jedno otwarte zadanie IDLE-* na rolę; nowe po merge starego (dedup na dzień)
OPEN_IDLE_STATUSES = (READY, RUNNING, IN_REVIEW, CHANGES_REQUESTED, AWAITING_OWNER, COOLING, FAILED)


def agent_roles() -> list[dict]:
    """Role agentów z hierarchii org (kind=agent): role, tytuł, obowiązki."""
    cfg = org_mod.load_config()
    out = []
    for n in cfg.get("nodes", []):
        if n.get("kind") != "agent" or not n.get("role"):
            continue
        out.append({
            "role": n["role"],
            "title": n.get("title") or n.get("label") or n["role"],
            "duties": (n.get("duties") or n.get("description") or "").strip(),
        })
    return out


def find_idle_roles(state: State, idle_s: int = IDLE_S, now: float | None = None) -> list[dict]:
    """Role bezczynne: brak zadania w stanie aktywnym ORAZ ostatnia aktywność
    (max updated_at zadań roli) starsza niż idle_s. Rola bez żadnych zadań w historii
    jest bezczynna od zawsze."""
    now = now if now is not None else time.time()
    busy: dict[str, float] = {}   # role -> ostatnia aktywność
    active: set[str] = set()      # role, które mają pracę teraz
    for t in state.list_tasks():
        role = t.get("role") or ""
        if not role:
            continue
        if (t.get("status") or "") in ACTIVE_STATUSES:
            active.add(role)
        ts = t.get("updated_at") or 0
        if ts > busy.get(role, 0):
            busy[role] = ts
    idle = []
    for r in agent_roles():
        role = r["role"]
        if role in active:
            continue  # ma pracę teraz albo zaraz ją podejmie
        last = busy.get(role, 0)
        since = now - last
        if last and since < idle_s:
            continue  # niedawno kończył — odetchnij
        idle.append({**r, "idle_s": round(since) if last else None})
    return idle


def run_idle_check(state: State, idle_s: int = IDLE_S, create: bool = True,
                   now: float | None = None) -> dict:
    """Godzinalny obchód: wykryj bezczynne role, stwórz po jednym zadaniu-plan
    per rola (dedup), wyślij raport do feeda Opiekuna. Zwraca raport (dict)."""
    now = now if now is not None else time.time()
    idles = find_idle_roles(state, idle_s=idle_s, now=now)
    created: list[str] = []
    if create:
        all_tasks = state.list_tasks()
        for it in idles:
            role = it["role"]
            prefix = f"IDLE-{role.upper()}-"
            if any(t["id"].startswith(prefix) and (t.get("status") or "") in OPEN_IDLE_STATUSES
                   for t in all_tasks):
                continue  # już ma otwarte zadanie od guardiana
            day = time.strftime("%Y%m%d", time.localtime(now))
            tid = f"{prefix}{day}"
            if state.get_task(tid):
                continue
            duties = it["duties"] or "Utrzymuj swoją rolę w gotowości."
            state.add_task(
                tid, _ops_milestone(state), 9,
                f"[guardian] zaplanuj pracę roli {role} (bezczynność "
                f"{_fmt(it['idle_s'])})",
                role,
                acceptance_criteria=f"Obowiązki stanowiska: {duties[:400]}. "
                                    "Zaplanuj i realizuj zadania najlepsze dla organizacji.",
                status=READY, priority="P3",
                title_sponsor=f"🎯 {it['title']}: bezczynny {_fmt(it['idle_s'])} — zaplanuj pracę",
                project_id="mikowhy-teams",
            )
            state.add_comment(tid, "workload-guardian",
                              f"🎯 Cel: domknąć bezczynność roli {role} → ✅ Efekt: zadanie-plan "
                              f"stworzone wg obowiązków stanowiska (guarantor: workload-guardian)")
            state.record_metric("guardian_idle_task", {"role": role, "idle_s": it["idle_s"]}, tid)
            created.append(tid)
    report = {
        "checked_at": now,
        "idle_roles": [{"role": i["role"], "idle_s": i["idle_s"], "title": i["title"]} for i in idles],
        "created": created,
    }
    state.record_metric("guardian_round", {"idle": len(idles), "created": len(created)}, None)
    if idles:
        names = ", ".join(f"`{i['role']}`({_fmt(i['idle_s'])})" for i in idles)
        state.add_notification(
            "guardian",
            f"🕵️ Workload Guardian: bezczynne role: {names}"
            + (f" — utworzono {len(created)} zadanie(-a)-plan" if created else " — zadania już istnieją"))
    return report


def _ops_milestone(state: State) -> str:
    for m in state.list_milestones():
        if (m.get("id") or "").endswith("-OPS") and m.get("status") == "OPEN":
            return m["id"]
    state.add_milestone("FACTORY-OPS", "Operacje i naprawy fabryki (mikowhy-teams)",
                        "milestone/factory-ops", title="FACTORY-OPS")
    return "FACTORY-OPS"


def _fmt(idle_s: float | None) -> str:
    if not idle_s:
        return "brak aktywności w historii"
    m = int(idle_s // 60)
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60} min"

"""FLOW-001: config-based przepływ pracy zespołu (zakładka 🌊 Flow).

load_config()      — default_flow.json + deep-merge override .agents/flow/flow.config.json
validate_config()  — walidacja configu (etapy, przejścia vs VALID_TRANSITIONS, akcje)
build_flow()       — model diagramu dla /api/flow (etapy + zadania + dostępne akcje)
apply_action()     — wykonanie akcji Sponsora z configu na zadaniu (approve/decline/retry/…)
save_override()    — zapis override configu (po walidacji)
reset_override()   — usunięcie override (powrót do default_flow.json)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from engine.state import State, VALID_TRANSITIONS, substep_progress

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CFG = Path(__file__).resolve().parent / "default_flow.json"
OVERRIDE_CFG = ROOT / ".agents" / "flow" / "flow.config.json"

REQUIRED_KEYS = ("stages", "transitions", "actions", "layout", "theme")
KNOWN_STATUSES = set(VALID_TRANSITIONS.keys()) | {"MERGED"}
KNOWN_ACTION_TYPES = {"approve", "decline", "retry", "comment", "set_priority", "merge", "stop"}  # v2.7.26: + stop (⏹️ Zakończ po etapie)
PRIORITIES = ("P0", "P1", "P2", "P3")


def _deep_merge(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(a.get(k), v) if k in a else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        return b  # listy w override ZASTĘPUJĄ listy z defaultu (stages/transitions/actions)
    return b


def load_default() -> dict:
    return json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))


def load_override_raw() -> dict | None:
    if not OVERRIDE_CFG.exists():
        return None
    return json.loads(OVERRIDE_CFG.read_text(encoding="utf-8"))


def load_config() -> dict:
    """Default config + override z .agents/flow/flow.config.json. Zły override -> fallback do default."""
    cfg = load_default()
    ov = load_override_raw()
    if ov is not None:
        try:
            cfg = _deep_merge(cfg, ov)
        except (json.JSONDecodeError, OSError, TypeError):
            cfg = load_default()
    if any(k not in cfg for k in REQUIRED_KEYS):
        cfg = load_default()
    cfg["_override_active"] = OVERRIDE_CFG.exists()
    return cfg


def stage_of_status(cfg: dict, status: str) -> str | None:
    for st in cfg.get("stages", []):
        if status in (st.get("statuses") or []):
            return st.get("id")
    return None


def validate_config(cfg: dict) -> list[str]:
    """Walidacja configu (bez zależności zewnętrznych). Zwraca listę błędów (pusta = OK)."""
    errs: list[str] = []
    for k in REQUIRED_KEYS:
        if k not in cfg:
            errs.append(f"brak sekcji '{k}'")
    if errs:
        return errs
    stages = cfg.get("stages") or []
    if not isinstance(stages, list) or not stages:
        return ["'stages' musi być niepustą listą"]
    lanes = (cfg.get("layout") or {}).get("lanes") or {}
    ids: set[str] = set()
    seen_status: dict[str, str] = {}
    for st in stages:
        sid = st.get("id")
        if not sid:
            errs.append("stage bez 'id'")
            continue
        if sid in ids:
            errs.append(f"zduplikowane id etapu: '{sid}'")
        ids.add(sid)
        if st.get("lane") not in lanes:
            errs.append(f"stage '{sid}': lane='{st.get('lane')}' nie istnieje w layout.lanes ({sorted(lanes)})")
        for s in st.get("statuses") or []:
            if s not in KNOWN_STATUSES:
                errs.append(f"stage '{sid}': nieznany status '{s}' (znane: {sorted(KNOWN_STATUSES)})")
            elif s in seen_status:
                errs.append(f"status '{s}' jest w dwóch etapach: '{seen_status[s]}' i '{sid}'")
            else:
                seen_status[s] = sid
        for a in st.get("actions") or []:
            if a not in (cfg.get("actions") or {}):
                errs.append(f"stage '{sid}': akcja '{a}' niezdefiniowana w 'actions'")
    actions = cfg.get("actions") or {}
    for aid, a in actions.items():
        t = a.get("type")
        if t not in KNOWN_ACTION_TYPES:
            errs.append(f"action '{aid}': nieznany type '{t}' (znane: {sorted(KNOWN_ACTION_TYPES)})")
        for s in a.get("allowed_from") or []:
            if s not in KNOWN_STATUSES:
                errs.append(f"action '{aid}': allowed_from ma nieznany status '{s}'")
        tgt = a.get("target_status")
        if tgt:
            if tgt not in KNOWN_STATUSES:
                errs.append(f"action '{aid}': target_status '{tgt}' nieznany")
            for s in a.get("allowed_from") or []:
                if tgt != s and tgt not in VALID_TRANSITIONS.get(s, set()):
                    errs.append(f"action '{aid}': przejście {s} -> {tgt} niedozwolone (VALID_TRANSITIONS)")
        if t == "set_priority":
            for c in a.get("choices") or []:
                if c not in PRIORITIES:
                    errs.append(f"action '{aid}': choice '{c}' nie jest priorytetem P0..P3")
    for tr in cfg.get("transitions") or []:
        f, t = tr.get("from"), tr.get("to")
        if f not in ids:
            errs.append(f"transition: from='{f}' nie istnieje w stages")
        if t not in ids:
            errs.append(f"transition: to='{t}' nie istnieje w stages")
        if f in ids and t in ids:
            fst = next(s for s in stages if s["id"] == f)
            tst = next(s for s in stages if s["id"] == t)
            pairs = [(fs, ts) for fs in fst.get("statuses", []) for ts in tst.get("statuses", [])
                     if ts == fs or ts in VALID_TRANSITIONS.get(fs, set())]
            if not pairs:
                errs.append(f"transition {f} -> {t}: żadna para statusów niedozwolona "
                            f"({fst.get('statuses')} -> {tst.get('statuses')})")
        act = tr.get("action")
        if act and act not in actions:
            errs.append(f"transition {f} -> {t}: action '{act}' niezdefiniowany w 'actions'")
    return errs


def validate_action(cfg: dict, task_status: str, action_id: str) -> str | None:
    """None gdy akcja dozwolona dla statusu, inaczej komunikat błędu."""
    a = (cfg.get("actions") or {}).get(action_id)
    if not a:
        return f"nieznana akcja '{action_id}'"
    if task_status not in (a.get("allowed_from") or []):
        return f"akcja '{action_id}' niedozwolona z statusu {task_status} (allowed_from: {a.get('allowed_from')})"
    return None


def save_override(cfg_obj: Any) -> list[str]:
    """Waliduje (merge z default) i zapisuje override. Zwraca błędy (pusta lista = zapisano)."""
    if isinstance(cfg_obj, str):
        cfg_obj = json.loads(cfg_obj)
    merged = _deep_merge(load_default(), cfg_obj)
    errs = validate_config(merged)
    if errs:
        return errs
    OVERRIDE_CFG.parent.mkdir(parents=True, exist_ok=True)
    OVERRIDE_CFG.write_text(json.dumps(cfg_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return []


def reset_override() -> bool:
    """Usuwa override — powrót do default_flow.json. True gdy plik istniał."""
    if OVERRIDE_CFG.exists():
        OVERRIDE_CFG.unlink()
        return True
    return False


DELTA_WINDOW_S = 300  # okno delty na kafelkach flow: ostatnie 5 minut z historii (metryki task_event)


def stage_deltas(state: "State | None", statuses: list[str], window_s: int = DELTA_WINDOW_S) -> dict:
    """Delta per etap z ostatnich `window_s` sekund: ile zadań weszło (+) i wyszło (−).
    Źródło: metryki kind='task_event' (payload {from, to}) zapisywane przy każdej zmianie statusu."""
    if state is None:
        return {"delta_in": 0, "delta_out": 0, "delta_window_s": window_s}
    now = time.time()
    try:
        rows = state._conn().execute(
            "SELECT payload FROM metrics WHERE kind='task_event' AND ts>=?", (now - window_s,)).fetchall()
    except Exception:
        rows = []
    sset = set(statuses or [])
    din = dout = 0
    for r in rows:
        try:
            p = json.loads(r[0])
        except Exception:
            continue
        to, frm = p.get("to"), p.get("from")
        if to in sset:
            din += 1
        if frm in sset and to not in sset:
            dout += 1
    return {"delta_in": din, "delta_out": dout, "delta_window_s": window_s}


def build_flow(snapshot: dict, state: State | None = None) -> dict:
    """Model diagramu flow dla /api/flow: config + etapy z zadaniami + przejścia + akcje per zadanie.
    Każdy etap dostaje też delta_in/delta_out — ile zadań weszło/wyszło w ostatnich 5 min (metryki)."""
    cfg = load_config()
    errors = validate_config(cfg)
    tasks = snapshot.get("tasks", [])
    now = time.time()

    stages_out = []
    for st in cfg.get("stages", []):
        sid = st["id"]
        in_stage = [t for t in tasks if stage_of_status(cfg, t.get("status")) == sid]
        sla = st.get("sla_s") or 0
        overdue = sum(1 for t in in_stage if sla and (t.get("age") or 0) > sla)
        prio_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        in_stage.sort(key=lambda t: (prio_order.get(t.get("priority") or "P2", 2), -(t.get("age") or 0)))
        stage_actions = st.get("actions") or []
        tlist = []
        for t in in_stage:
            avail = [a for a in stage_actions
                     if not validate_action(cfg, t.get("status"), a)]
            tlist.append({
                "id": t.get("id"), "title": t.get("title_sponsor") or t.get("title") or "",
                "status": t.get("status"), "substep": t.get("substep") or "",
                "progress": t.get("progress") if t.get("progress") is not None else substep_progress(t.get("substep")),
                "priority": t.get("priority") or "P2", "role": t.get("role") or "",
                "persona": t.get("persona") or "", "project_id": t.get("project_id"),
                "pr": t.get("pr"), "parent_task": t.get("parent_task"), "depth": t.get("depth") or 0,
                "age": t.get("age") or max(0, now - (t.get("updated_at") or now)),
                "elapsed": t.get("elapsed"), "started_at": t.get("started_at"),
                "overdue": bool(sla and (t.get("age") or 0) > sla),
                "available_actions": avail,
            })
        stages_out.append({
            "id": sid, "label": st.get("label", sid), "icon": st.get("icon", "•"),
            "lane": st.get("lane", "main"), "order": st.get("order", 0),
            "statuses": st.get("statuses", []), "color": st.get("color", "#6e7681"),
            "hot": bool(st.get("hot")), "sla_s": sla, "description": st.get("description", ""),
            "count": len(tlist), "overdue": overdue, "tasks": tlist,
            "actions": stage_actions,
            **stage_deltas(state, st.get("statuses", [])),
        })
    stages_out.sort(key=lambda s: (cfg["layout"]["lanes"][s["lane"]]["y"] if s["lane"] in cfg["layout"].get("lanes", {}) else 9,
                                   s.get("order", 0)))
    transitions_out = []
    for tr in cfg.get("transitions", []):
        transitions_out.append({"from": tr.get("from"), "to": tr.get("to"),
                                "label": tr.get("label", ""), "icon": tr.get("icon", ""),
                                "kind": tr.get("kind", "auto"), "action": tr.get("action")})
    stats = {
        "total": len(tasks),
        "hot": sum(1 for s in stages_out if s["hot"] and s["count"]),
        "overdue": sum(s["overdue"] for s in stages_out),
        "per_stage": {s["id"]: s["count"] for s in stages_out},
    }
    return {
        "config": cfg,
        "stages": stages_out,
        "transitions": transitions_out,
        "actions": cfg.get("actions", {}),
        "stats": stats,
        "errors": errors,
        "override_active": cfg.get("_override_active", False),
        "ts": now,
    }


def apply_action(task_id: str, action_id: str, payload: dict | None = None,
                 state: State | None = None) -> dict:
    """Wykonuje akcję z configa na zadaniu. Rzuca ValueError przy złej akcji/statusie.

    Akcje typu approve/decline/retry robią to samo co komendy Sponsora w orchestratorze
    (to samo przejście pilnowane przez VALID_TRANSITIONS + metric), ale przez podany State —
    bez telegrama i bez globalnego połączenia (testowalne na tymczasowym DB).
    """
    payload = payload or {}
    s = state or State()
    cfg = load_config()
    errs = validate_config(cfg)
    if errs:
        raise ValueError("config flow ma błędy: " + "; ".join(errs[:5]))
    task = s.get_task(task_id)
    if not task:
        raise ValueError(f"nieznane zadanie: {task_id}")
    status = task["status"]
    verr = validate_action(cfg, status, action_id)
    if verr:
        raise ValueError(verr)
    a = cfg["actions"][action_id]
    atype = a.get("type")
    frm = status
    if atype == "approve":
        # substep bez zmiany: decyzja puszcza zadanie do kolejki, a worker wznawia od wypracowanego etapu
        s.transition(task_id, "READY",
                     review_notes=(task.get("review_notes") or "") + " | zaakceptowane przez Sponsora (flow)")
        s.record_metric("sponsor_approve", {"task": task_id, "via": "flow"}, task_id)
        to = "READY"
    elif atype == "decline":
        reason = str(payload.get("reason") or "odrzucone z flow").strip()
        s.transition(task_id, "BLOCKED", substep="OWNER_PACKET",
                     review_notes=f"ODRZUCONE przez Sponsora (flow): {reason}")
        s.record_metric("sponsor_decline", {"task": task_id, "reason": reason, "via": "flow"}, task_id)
        to = "BLOCKED"
    elif atype == "retry":
        if status not in ("FAILED", "BLOCKED", "ESCALATED"):
            raise ValueError(f"retry tylko z FAILED/BLOCKED/ESCALATED (jest {status})")
        s.transition(task_id, "READY", substep="1/5_PLAN", retry_count=0,
                     review_notes="requeue (retry z flow) — stare review_notes wyczyszczone")
        s.record_metric("sponsor_retry", {"task": task_id, "via": "flow"}, task_id)
        to = "READY"
    elif atype == "comment":
        text = str(payload.get("text") or payload.get("reason") or "").strip()
        if not text:
            raise ValueError("comment wymaga niepustego 'text'")
        s.add_comment(task_id, "sponsor/flow", text)
        s._conn().execute("UPDATE tasks SET review_notes=COALESCE(review_notes,'')||? , updated_at=? WHERE id=?",
                          ("\n[flow] " + text[:500], time.time(), task_id))
        s.record_metric("task_event", {"event": "comment", "via": "flow", "changes": {"comment": text[:120]}}, task_id)
        to = status
    elif atype == "merge":
        # v2.7.10: „🚀 Wdróż” — NIE mergujemy tu sami (git merge robi orchestrator z regression gates);
        # tylko flaga + komentarz, żeby _handle_merges wziął to w pierwszej kolejności
        if status != "READY_TO_MERGE":
            raise ValueError(f"merge tylko z READY_TO_MERGE (jest {status})")
        s.set_kv(f"sponsor_merge:{task_id}", time.time())
        s.add_comment(task_id, "sponsor/flow",
                      "🚀 Cel: wdrożyć zadanie (merge do gałęzi docelowej) → ✅ Efekt: zlecone — "
                      "orchestrator zmerguje w najbliższym ticku (rebase + regression gates + push)")
        s.record_metric("sponsor_merge", {"task": task_id, "via": "flow"}, task_id)
        to = status
    elif atype == "stop":
        # v2.7.26: „⏹️ Zakończ po etapie" — NIE zmieniamy statusu (zadanie zostaje RUNNING,
        # worker dokończy bieżący etap, a na granicy etapu sam zrobi RUNNING → READY).
        # status musi pozostać RUNNING, bo VALID_TRANSITIONS nie ma prosta ścieżki z przerywania —
        # pauzy pilnuje work_lifecycle._stage_stop().
        import time as _t
        s.request_stop(task_id)
        s.add_comment(task_id, "sponsor/flow",
                      "⏹️ Cel: zakończyć pracę po bieżącym etapie → ✅ Efekt: worker dokończy aktualny krok i pauzuje "
                      "(status READY, bez podejmowania kolejnego etapu); wznowienie = standardowa kolejka")
        s.record_metric("sponsor_stage_stop", {"task": task_id, "at": _t.time(), "via": "flow"}, task_id)
        to = status
    elif atype == "set_priority":
        prio = str(payload.get("priority") or payload.get("value") or "").strip()
        if prio not in (a.get("choices") or PRIORITIES):
            raise ValueError(f"zły priorytet '{prio}' (dozwolone: {a.get('choices') or list(PRIORITIES)})")
        s.set_priority(task_id, prio)
        to = status
    else:
        raise ValueError(f"nieobsługiwany type akcji: {atype}")
    return {"ok": True, "task_id": task_id, "action": action_id, "from": frm, "to": to}

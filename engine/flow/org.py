"""ORG-001: flow stanowisk (zakładka 🌊 Flow → pod-zakładka „🏗️ Flow stanowisk").

Hierarchia zespołu: kto jest czyim szefem (reports_to) — config-based jak flow pracy:

load_config()      — default_org.json + deep-merge override .agents/flow/org.config.json
validate_config()  — walidacja hierarchii (jeden root, brak cykli, reports_to istnieje,
                     role agentów znane w rejestrze .agents/registry.json, węzły dosięgalne z root)
build_org()        — model org chart dla /api/org (węzły + persony + żywe zadania + statystyki)
save_override()    — zapis override configu (po walidacji)
reset_override()   — usunięcie override (powrót do default_org.json)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CFG = Path(__file__).resolve().parent / "default_org.json"
OVERRIDE_CFG = ROOT / ".agents" / "flow" / "org.config.json"

REQUIRED_KEYS = ("nodes", "layout", "theme")
KNOWN_KINDS = {"agent", "human"}


def _deep_merge(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(a.get(k), v) if k in a else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        return b  # listy w override ZASTĘPUJĄ listy z defaultu (nodes)
    return b


def load_default() -> dict:
    return json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))


def load_override_raw() -> dict | None:
    if not OVERRIDE_CFG.exists():
        return None
    try:
        return json.loads(OVERRIDE_CFG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None  # uszkodzony/niepełny override -> fallback do default


def load_config() -> dict:
    """Default config + override z .agents/flow/org.config.json. Zły override -> fallback do default."""
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


def known_roles() -> set[str]:
    """Role zarejestrowane w fabryce (.agents/registry.json) — fallback przy braku rejestru."""
    try:
        from engine import skill_compiler
        reg = skill_compiler.load_registry()
        roles = set((reg.get("roles") or {}).keys())
        if roles:
            return roles
    except Exception:
        pass
    return {n.get("role") for n in load_default().get("nodes", []) if n.get("kind") == "agent"}


def _depths(nodes: list[dict]) -> dict[str, int]:
    """Głębokość węzła = liczba przodków (root=0). Przy cyklu zwraca {} — wykryje to walidacja."""
    by_id = {n.get("id"): n for n in nodes}
    depth: dict[str, int] = {}

    def dep(nid: str, seen: set[str]) -> int | None:
        if nid in depth:
            return depth[nid]
        if nid in seen:
            return None  # cykl
        seen.add(nid)
        rt = by_id[nid].get("reports_to")
        if rt is None:
            d = 0
        elif rt not in by_id:
            return None
        else:
            pd = dep(rt, seen)
            if pd is None:
                return None
            d = pd + 1
        depth[nid] = d
        return d

    for n in nodes:
        if dep(n.get("id"), set()) is None:
            return {}
    return depth


def validate_config(cfg: dict) -> list[str]:
    """Walidacja hierarchii. Zwraca listę błędów (pusta = OK)."""
    errs: list[str] = []
    for k in REQUIRED_KEYS:
        if k not in cfg:
            errs.append(f"brak sekcji '{k}'")
    if errs:
        return errs
    nodes = cfg.get("nodes") or []
    if not isinstance(nodes, list) or not nodes:
        return ["'nodes' musi być niepustą listą"]
    ids: set[str] = set()
    roots: list[str] = []
    roles_seen: dict[str, str] = {}
    known = known_roles()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            errs.append("node bez 'id'")
            continue
        if nid in ids:
            errs.append(f"zduplikowane id węzła: '{nid}'")
        ids.add(nid)
        kind = n.get("kind")
        if kind not in KNOWN_KINDS:
            errs.append(f"node '{nid}': kind '{kind}' nieznany (znane: {sorted(KNOWN_KINDS)})")
        if kind == "agent":
            role = n.get("role")
            if not role:
                errs.append(f"node '{nid}': kind=agent wymaga 'role'")
            elif role in roles_seen:
                errs.append(f"rola '{role}' jest w dwóch węzłach: '{roles_seen[role]}' i '{nid}'")
            else:
                roles_seen[role] = nid
                if role not in known:
                    errs.append(f"node '{nid}': nieznana rola '{role}' (znane: {sorted(known)})")
        if n.get("reports_to") is None:
            roots.append(nid)
        elif n.get("reports_to") not in ids and n.get("reports_to") not in {x.get("id") for x in nodes}:
            errs.append(f"node '{nid}': reports_to='{n.get('reports_to')}' nie istnieje w nodes")
    if len(roots) == 0:
        errs.append("brak root — dokładnie jeden węzeł musi mieć reports_to=null")
    elif len(roots) > 1:
        errs.append(f"wiele rootów ({roots}) — musi być dokładnie jeden (reports_to=null)")
    depths = _depths(nodes)
    if not depths and len(ids) == len(nodes):
        errs.append("cykl w reports_to (lub niedosięgalny reports_to) — hierarchia musi być drzewem z jednym rootem")
    if roots and depths:
        unreachable = [nid for nid in ids if nid not in depths]
        if unreachable:
            errs.append(f"węzły niedosięgalne z root '{roots[0]}': {unreachable}")
    return errs


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
    """Usuwa override — powrót do default_org.json. True gdy plik istniał."""
    if OVERRIDE_CFG.exists():
        OVERRIDE_CFG.unlink()
        return True
    return False


def build_org(snapshot: dict, state: "Any | None" = None) -> dict:
    """Model org chart dla /api/org: config + węzły (persona + żywe dane) + statystyki.

    Każdy węzeł kind=agent dostaje personę z engine/personas (imię, stanowisko, ikona, kolor)
    oraz dane z aktualnych zadań: current (RUNNING), by_status, tasks, merged.
    """
    from engine import personas

    cfg = load_config()
    errors = validate_config(cfg)
    tasks = snapshot.get("tasks", []) or []
    now = time.time()
    depths = _depths(cfg.get("nodes", []))

    nodes_out = []
    for n in cfg.get("nodes", []):
        nid = n.get("id")
        node = {
            "id": nid,
            "kind": n.get("kind", "agent"),
            "role": n.get("role") if n.get("kind") == "agent" else None,
            "label": n.get("label", nid),
            "icon": n.get("icon", "•"),
            "title": n.get("title", ""),
            "description": n.get("description", ""),
            "reports_to": n.get("reports_to"),
            "depth": depths.get(nid, 0),
            "reports": [],
        }
        if n.get("kind") == "agent" and n.get("role"):
            role = n["role"]
            p = personas.persona_full(role)
            rt = [t for t in tasks if (t.get("role") or "worker") == role]
            by_status: dict[str, int] = {}
            for t in rt:
                by_status[t["status"]] = by_status.get(t["status"], 0) + 1
            cur = next((t for t in rt if t.get("status") == "RUNNING"), None)
            node.update({
                "persona": p,
                "name": p["name"],
                "position": p["position"],
                "busy": cur is not None,
                "tasks": len(rt),
                "merged": by_status.get("MERGED", 0),
                "by_status": by_status,
                "current": ({
                    "id": cur["id"],
                    "title": cur.get("title_sponsor") or cur.get("title") or "",
                    "status": cur.get("status"),
                    "priority": cur.get("priority") or "P2",
                    "started_at": cur.get("started_at"),
                    "elapsed": cur.get("elapsed") or max(0, now - (cur.get("started_at") or now)),
                    "project_id": cur.get("project_id"),
                } if cur else None),
            })
        else:
            node.update({
                "persona": None,
                "name": n.get("label", nid),
                "position": n.get("title", ""),
                "busy": False,
                "tasks": 0,
                "merged": 0,
                "by_status": {},
                "current": None,
            })
        nodes_out.append(node)

    by_id = {n["id"]: n for n in nodes_out}
    for n in nodes_out:
        rt = n.get("reports_to")
        if rt and rt in by_id:
            by_id[rt]["reports"].append(n["id"])

    # kolejność: głębokość, potem reports_to, potem id — stabilne poziomy w SVG
    nodes_out.sort(key=lambda n: (n["depth"], n.get("reports_to") or "", n["id"]))

    stats = {
        "total": len(nodes_out),
        "agents": sum(1 for n in nodes_out if n["kind"] == "agent"),
        "humans": sum(1 for n in nodes_out if n["kind"] == "human"),
        "busy": sum(1 for n in nodes_out if n.get("busy")),
        "depth_max": max((n["depth"] for n in nodes_out), default=0),
        "per_depth": {},
    }
    for n in nodes_out:
        stats["per_depth"][n["depth"]] = stats["per_depth"].get(n["depth"], 0) + 1

    return {
        "config": cfg,
        "nodes": nodes_out,
        "stats": stats,
        "errors": errors,
        "override_active": cfg.get("_override_active", False),
        "ts": now,
    }

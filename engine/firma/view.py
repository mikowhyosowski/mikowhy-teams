"""FIRMA-001/002: config widoku + agregacja „świata” (osiedle→biura→pokoje→biurka).

load_config()  — default_view.json + deep-merge override .agents/firma/view.config.json
build_world()  — model świata dla /api/firma (używa snapshotu dashboardu + State)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_CFG = Path(__file__).resolve().parent / "default_view.json"
OVERRIDE_CFG = ROOT / ".agents" / "firma" / "view.config.json"

# status zadania -> strefa biura (teczka na odpowiedniej półce)
ZONE = {
    "READY": "tray",
    "RUNNING": "desk",          # biurko w pokoju agenta (pozycja liczona po stronie JS)
    "AWAITING_APPROVAL": "sign",
    "IN_REVIEW": "review",
    "READY_TO_MERGE": "outbox",
    "MERGED": "archive",
    "FAILED": "alarm",
    "ESCALATED": "alarm",
    "BLOCKED": "locked",
}

REQUIRED_KEYS = ("estate", "office", "zones", "rooms", "desks", "statusStyle", "prioScale", "colors")


def _deep_merge(a: Any, b: Any) -> Any:
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(a.get(k), v) if k in a else v
        return out
    return b


def load_config() -> dict:
    """Default config + override z .agents/firma/view.config.json. Zły override -> fallback do default."""
    cfg = json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))
    if OVERRIDE_CFG.exists():
        try:
            ov = json.loads(OVERRIDE_CFG.read_text(encoding="utf-8"))
            cfg = _deep_merge(cfg, ov)
        except (json.JSONDecodeError, OSError):
            pass  # fallback: default
    missing = [k for k in REQUIRED_KEYS if k not in cfg]
    if missing:
        cfg = json.loads(DEFAULT_CFG.read_text(encoding="utf-8"))  # uszkodzony config -> default
    cfg["_override_active"] = OVERRIDE_CFG.exists()
    return cfg


def validate_config(cfg: dict) -> list[str]:
    """Prosta walidacja (bez zależności): zwraca listę błędów."""
    errs = []
    for k in REQUIRED_KEYS:
        if k not in cfg:
            errs.append(f"brak sekcji '{k}'")
    st = cfg.get("statusStyle", {})
    zones = cfg.get("zones", {})
    for status, s in st.items():
        if s.get("zone") not in zones and s.get("zone") != "desk":
            errs.append(f"statusStyle.{status}.zone='{s.get('zone')}' nie ma odpowiednika w zones")
    return errs


def _proj_color(pid: str) -> str:
    h = 0
    for ch in str(pid):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return f"hsl({h % 360},70%,45%)"


def build_world(snapshot: dict, state=None) -> dict:
    """Buduje model świata z snapshotu dashboardu (/api/state).

    offices[] = projekty (biura); rooms[] = agenci wg roli; desks[] = zadania (biurka).
    """
    cfg = load_config()
    tasks = snapshot.get("tasks", [])
    projects = snapshot.get("projects", [])
    team = {m.get("role"): m for m in snapshot.get("team", [])}
    milestones = []
    if state is not None:
        try:
            milestones = state.list_milestones()
        except Exception:
            milestones = []

    offices = []
    for p in projects:
        pid = p.get("project_id")
        ptasks = [t for t in tasks if (t.get("project_id") or "") == pid]
        roles: dict[str, list[dict]] = {}
        for t in ptasks:
            roles.setdefault(t.get("role") or "worker", []).append(t)
        rooms = []
        for role, ts in sorted(roles.items(), key=lambda kv: -len(kv[1])):
            m = team.get(role, {})
            cur = next((t for t in ts if t.get("status") == "RUNNING"), None)
            rooms.append({
                "role": role,
                "name": m.get("name") or role,
                "position": m.get("position") or role,
                "avatar": m.get("avatar") or "🧑‍💻",
                "gender": m.get("gender") or "",
                "res": (cur or {}).get("res"),
                "current": ({"id": cur["id"], "started_at": cur.get("started_at"), "priority": cur.get("priority")} if cur else None),
                "taskCount": len(ts),
                "threadCount": 0,
            })
        # agenci, którzy odpowiadali w wątkach (nie mają zadań, ale "brali udział") — lądują w biurze fabryki
        if pid == "mikowhy-teams" and state is not None:
            try:
                c = state._conn()
                c.row_factory = __import__("sqlite3").Row
                have = {r["role"] for r in rooms}
                for row in c.execute(
                        "SELECT t.role, COUNT(m.id) n FROM threads t JOIN thread_messages m ON m.thread_id=t.id "
                        "WHERE m.author=t.role GROUP BY t.role ORDER BY n DESC").fetchall():
                    if row["role"] in have:
                        next(r for r in rooms if r["role"] == row["role"])["threadCount"] = row["n"]
                    else:
                        m = team.get(row["role"], {})
                        rooms.append({"role": row["role"], "name": m.get("name") or row["role"],
                                      "position": m.get("position") or row["role"],
                                      "avatar": m.get("avatar") or "🧑‍💻", "gender": m.get("gender") or "",
                                      "res": None, "current": None, "taskCount": 0, "threadCount": row["n"]})
            except Exception:
                pass
        desks = [{
            "id": t.get("id"), "title": t.get("title") or "", "title_sponsor": t.get("title_sponsor") or "",
            "status": t.get("status"), "zone": ZONE.get(t.get("status"), "tray"),
            "priority": t.get("priority") or "P2", "substep": t.get("substep") or "",
            "role": t.get("role") or "worker", "persona": t.get("persona") or "",
            "parent_task": t.get("parent_task"), "pr": t.get("pr"),
            "started_at": t.get("started_at"), "elapsed": t.get("elapsed"), "res": t.get("res"),
            "updated_at": t.get("updated_at"), "age": t.get("age"),
        } for t in ptasks]
        offices.append({
            "project_id": pid,
            "color": _proj_color(pid),
            "read_only": bool(p.get("read_only")),
            "policy": p.get("approval_policy", "manual"),
            "target": p.get("target"),
            "milestones": [{"id": m.get("id"), "title": m.get("title") or "", "status": m.get("status"),
                            "branch": m.get("branch")} for m in milestones if m.get("project_id") == pid],
            "rooms": rooms,
            "desks": desks,
        })
    return {
        "config": cfg,
        "estate": {"name": cfg.get("meta", {}).get("name", "Osiedle"), "host": snapshot.get("host"),
                   "sys": snapshot.get("sys"), "ts": time.time()},
        "offices": offices,
    }

"""Multi-project workspace — każdy projekt ma własny pipeline i target_repo_path.

Struktura:
  .agents/active_project.json            -> {"active": "<project_id>"}
  .agents/projects/<project_id>/project.json   -> {"project_id", "name", "target_repo_path", "read_only", ...}
  .agents/projects/<project_id>/pipeline.json  -> snapshot DAG tego projektu

Projekt "mikowhy-teams" (self) ma target_repo_path = katalog silnika.
Projekty z "read_only": true (np. news-video-generator w fazie audytu) są
STRICT READ-ONLY — git_guard.assert_writable() blokuje każdą mutację.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # katalog silnika (fabryki)
AGENTS = ROOT / ".agents"
SELF_ID = "mikowhy-teams"


class ReadOnlyProject(RuntimeError):
    pass


def active_project_id() -> str:
    p = AGENTS / "active_project.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8")).get("active", SELF_ID)
    return SELF_ID


def set_active_project(project_id: str) -> None:
    AGENTS.mkdir(parents=True, exist_ok=True)
    (AGENTS / "active_project.json").write_text(
        json.dumps({"active": project_id}, indent=2) + "\n", encoding="utf-8")


def project_config(project_id: str) -> dict:
    p = AGENTS / "projects" / project_id / "project.json"
    if p.exists():
        # projekt self też może mieć project.json (np. approval_policy=auto) — czytaj z dysku
        cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg.setdefault("project_id", project_id)
        if project_id == SELF_ID:
            cfg.setdefault("name", "mikowhy-teams (silnik)")
            cfg.setdefault("target_repo_path", str(ROOT))
            cfg.setdefault("read_only", False)
        return cfg
    if project_id == SELF_ID:
        return {"project_id": SELF_ID, "name": "mikowhy-teams (silnik)",
                "target_repo_path": str(ROOT), "read_only": False}
    raise KeyError(f"Nieznany projekt: {project_id} (brak {p})")


def register_project(project_id: str, name: str, target_repo_path: str, read_only: bool = False) -> dict:
    cfg = {"project_id": project_id, "name": name, "target_repo_path": target_repo_path,
           "read_only": read_only,
           "created": __import__("time").time(),
           "pipeline_snapshot": f".agents/projects/{project_id}/pipeline.json"}
    d = AGENTS / "projects" / project_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "project.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cfg


POLICIES = ("manual", "auto", "auto_readonly")


def is_active(project_id: str) -> bool:
    """Czy projekt jest aktywny (nie wstrzymany przez opiekuna). Domyślnie tak."""
    try:
        return bool(project_config(project_id).get("active", True))
    except KeyError:
        return True


def set_active(project_id: str, active: bool) -> dict:
    p = AGENTS / "projects" / project_id / "project.json"
    cfg = project_config(project_id) if p.exists() or project_id == SELF_ID else None
    if cfg is None:
        raise KeyError(project_id)
    cfg = dict(cfg)
    cfg["active"] = bool(active)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cfg


def approval_policy(project_id: str) -> str:
    """Polityka akceptacji zadań AWAITING_APPROVAL dla projektu (config w project.json):
    - 'manual'        — wszystko czeka na klik Sponsora (domyślnie)
    - 'auto'          — wszystkie zadania auto-approve -> READY
    - 'auto_readonly' — auto-approve tylko zadania read_only (audyt/decyzja/plan, zero commitów);
                        zadania z kodem nadal czekają na Sponsora"""
    try:
        cfg = project_config(project_id)
    except KeyError:
        return "manual"
    return cfg.get("approval_policy", "manual")


def set_approval_policy(project_id: str, policy: str) -> dict:
    """Ustawia approval_policy w .agents/projects/<pid>/project.json (walidacja wartości)."""
    if policy not in POLICIES:
        raise ValueError(f"policy musi być jednym z {POLICIES}, dostano: {policy!r}")
    p = AGENTS / "projects" / project_id / "project.json"
    if p.exists():
        cfg = json.loads(p.read_text(encoding="utf-8"))
    else:
        cfg = project_config(project_id) if project_id != SELF_ID else {
            "project_id": project_id, "name": project_id, "target_repo_path": str(ROOT), "read_only": False}
        cfg.setdefault("project_id", project_id)
    cfg["approval_policy"] = policy
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cfg


def list_projects() -> list[dict]:
    out, seen = [], set()
    pdir = AGENTS / "projects"
    if pdir.exists():
        for d in sorted(pdir.iterdir()):
            if (d / "project.json").exists():
                cfg = json.loads((d / "project.json").read_text(encoding="utf-8"))
                pid = cfg.get("project_id", d.name)
                if pid in seen:
                    continue
                seen.add(pid)
                out.append(cfg)
    if SELF_ID not in seen:
        out.insert(0, project_config(SELF_ID))
    return out


def repo_for_task(task: dict, state=None) -> Path:
    """Repo, w którym żyje worktree danego zadania.
    task['repo'] == 'factory' -> katalog silnika (np. audyt piszący tylko do mikowhy-teams);
    inaczej -> target_repo_path projektu."""
    if task.get("repo") == "factory":
        return ROOT
    pid = task.get("project_id") or active_project_id()
    return Path(project_config(pid)["target_repo_path"])


def is_read_only(path: Path | str) -> bool:
    rp = str(Path(path).resolve())
    return any(p.get("read_only") and str(Path(p["target_repo_path"]).resolve()) == rp
               for p in list_projects() if p["project_id"] != SELF_ID)


def assert_writable(repo: Path | str) -> None:
    """STRICT READ-ONLY: rzuca, gdy repo jest celem projektu read_only."""
    if is_read_only(repo):
        raise ReadOnlyProject(
            f"{repo} jest STRICT READ-ONLY (projekt read_only) — żadnych zapisów, branchy, checkoutów. "
            f"Raporty/plany tylko w katalogu silnika.")


def snapshot_path_for(project_id: str) -> Path:
    if project_id == SELF_ID:
        return AGENTS / "pipeline.json"
    return AGENTS / "projects" / project_id / "pipeline.json"

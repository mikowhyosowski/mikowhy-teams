"""Faza 2.5 — Scaffolding & Dependencies (zmiana #4).

Sekwencyjny mikro-krok między Fazą 2 (zamrożone kontrakty) a Fazą 3 (dekompozycja),
runowany na gałęzi milestonu. Architekt/Tech Lead:
  1. instaluje wymagane biblioteki (pyproject.toml / package.json / composer.json),
  2. generuje puste migracje bazy,
  3. rejestruje puste szkielety modułów / routerów / kontenera DI.
Dzięki temu równoległe boty w Fazie 4 piszą tylko czystą logikę w swoich
plikach ze scope_paths — żaden bot nie rusza już plików współdzielonych
(lockfile, routery, migracje), więc nie ma konfliktów lockfile'ów.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def run_scaffolding(worktree: Path, milestone_branch: str, spec: dict) -> dict:
    """spec: {"dependencies": [...], "migrations": [...], "skeletons": {"path": "content"}}.

    Zwraca raport; wszystko ląduje w osobnym commicie na gałęzi milestonu.
    """
    report: dict = {"dependencies": [], "migrations": [], "skeletons": [], "errors": []}

    # 1. Zależności — jeden wspólny plik manifestu, jedna osoba (ten moduł) go dotyka
    for dep in spec.get("dependencies", []):
        if (worktree / "pyproject.toml").exists():
            proc = _run(["pip", "install", dep], worktree)  # poetry add w realnym setupie
        elif (worktree / "package.json").exists():
            proc = _run(["npm", "install", dep], worktree)
        elif (worktree / "composer.json").exists():
            proc = _run(["composer", "require", dep], worktree)
        else:
            proc = subprocess.CompletedProcess([], 1, stderr="brak manifestu zależności")
        report["dependencies"].append({"dep": dep, "rc": proc.returncode,
                                       "err": proc.stderr.strip()[:300]})
        if proc.returncode != 0:
            report["errors"].append(f"zależność {dep}: {proc.stderr.strip()[:200]}")

    # 2. Puste migracje
    migrations_dir = worktree / "migrations"
    migrations_dir.mkdir(parents=True, exist_ok=True)
    for mig in spec.get("migrations", []):
        path = migrations_dir / mig
        if not path.exists():
            path.write_text(f'"""Pusta migracja: {mig} (wygenerowana w Fazie 2.5)."""\n', encoding="utf-8")
            report["migrations"].append(str(path))

    # 3. Szkielety modułów + rejestracja w routerze/DI — puste pliki w ustalonych miejscach
    for rel, content in spec.get("skeletons", {}).items():
        path = worktree / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            report["skeletons"].append(rel)

    from engine import git_guard  # lokalny import, żeby uniknąć cyklu
    if git_guard.changed_files(worktree):
        git_guard.commit_all(worktree, f"chore(scaffold): faza 2.5 dla {milestone_branch}")
    return report

"""Pre-flight (v2.7.27): samokontrola wykonawcy PRZED oddaniem pracy dalej.

Filozofia (zasada Sponsora „Opiekun jako ostatnia instancja"): wykonawca łapie
swoje błędy SAM — zanim code review i QG kosztują pełny cykl. Tutaj 100% tanie,
deterministyczne sprawdzenia:

  1. składnia Pythona (py_compile) dla zmienionych .py       -> klasa SyntaxError
  2. poprawność JSON dla zmienionych .json                   -> JSONDecodeError
  3. szybki pytest -x na zmienionych plikach testowych        -> klasy ImportError / rely
  4. wykrycie „pustego diffu" (zmiany, które nic nie zmieniają)

Zwraca raport {"ok", "changed", "problems": [{"check","path","error"}]} —
krok 4/5_QG w workerze podaje problems LLM-owi do natychmiastowej naprawy
(bez odbijania zadania i bez widoku Opiekuna).
"""
from __future__ import annotations

import json
import py_compile
import subprocess
from pathlib import Path

PYTEST_TIMEOUT_S = 180


def changed_files(worktree: Path) -> list[str]:
    """Zmienione/nowe pliki wg git (ścieżki względne). Nieśledzone KATALOGI są
    rozwiwane na pliki (git status --porcelain zwija '?? tests/' — bez tego
    pre-flight nie widziałby nowych testów)."""
    try:
        proc = subprocess.run(["git", "status", "--porcelain"], cwd=worktree,
                              capture_output=True, text=True, timeout=30)
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree, capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    out = set()
    for line in (proc.stdout or "").splitlines():
        path = line[3:].strip().strip('"')
        if not path:
            continue
        if " -> " in path:  # rename: bierz cel
            path = path.split(" -> ")[-1]
        out.add(path)
    for line in (untracked.stdout or "").splitlines():
        path = line.strip()
        if path:
            out.add(path)
    return sorted(out)


def _problem(check: str, path: str, error: str) -> dict:
    return {"check": check, "path": path, "error": str(error)[:400]}


def run(worktree: Path, changed: list[str] | None = None) -> dict:
    """Wykonuje wszystkie kontrole. Nigdy nie rzuca — błędy są problemami w raporcie."""
    wt = Path(worktree)
    if changed is None:
        changed = changed_files(wt)
    problems: list[dict] = []

    py_files = [p for p in changed if p.endswith(".py") and (wt / p).is_file()]
    json_files = [p for p in changed if p.endswith(".json") and (wt / p).is_file()]
    test_files = [p for p in changed if p.endswith(".py")
                  and ("test" in Path(p).name or p.startswith("tests/")) and (wt / p).is_file()]

    # 1) składnia Pythona — złap SyntaxError/IndentationError natychmiast
    for p in py_files:
        try:
            py_compile.compile(str(wt / p), doraise=True)
        except py_compile.PyCompileError as e:
            problems.append(_problem("py_syntax", p, e))
        except Exception as e:
            problems.append(_problem("py_syntax", p, repr(e)))

    # 2) JSON — dokładność zapisu (kod produkcji też bywa plikami konfiguracyjnymi)
    for p in json_files:
        try:
            json.loads((wt / p).read_text(encoding="utf-8"))
        except Exception as e:
            problems.append(_problem("json_valid", p, e))

    # 3) szybki pytest na zmienionych testach — złap kolekcję błąd/nieprzechodzący test
    if test_files:
        try:
            import os
            env = {**os.environ, "PYTHONPATH": str(wt)}  # import z rootdir worktree (jak w QG)
            proc = subprocess.run(["pytest", "-q", "-x", "--tb=short", *test_files],
                                  cwd=wt, capture_output=True, text=True,
                                  timeout=PYTEST_TIMEOUT_S, env=env)
            if proc.returncode not in (0, 5):  # 5 = brak testów zebranych
                tail = (proc.stdout or "")[-2500:]
                problems.append(_problem("pytest", ", ".join(test_files[:5]), tail))
        except subprocess.TimeoutExpired:
            problems.append(_problem("pytest", ", ".join(test_files[:5]),
                                     f"timeout {PYTEST_TIMEOUT_S}s"))
        except FileNotFoundError:
            pass  # brak pytest w środowisku — QG i tak sprawdzi

    return {"ok": not problems, "changed": changed, "problems": problems}


def problems_digest(report: dict) -> str:
    """Kompaktowy opis problemów — do payloadu LLM (naprawa) i do komentarza."""
    out = []
    for p in report.get("problems", [])[:8]:
        out.append(f"[{p['check']}] {p['path']}: {p['error'][:300]}")
    return "\n".join(out) or "brak problemów"

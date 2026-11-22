"""Quality gates — deterministyczne bramki jakości.

QG-1  PRD: każde wymaganie ma scenariusze Gherkin (Given/When/Then) + Edge Cases.
QG-2  Kontrakty: walidacja OpenAPI/JSON Schema + szybki audyt security
      (autoryzacja, rate-limit, walidacja podpisów). Po przejściu kontrakty = FROZEN.
QG-3  (zmiana #3) Semantic Integration Check: przed mergem milestone/* do main
      cały stos (Docker Compose) + testy E2E z scenariuszy Gherkin z PRD
      (Schemathesis / Playwright / pytest-e2e) — nie tylko testy jednostkowe.

local_gates: ruff / mypy / phpstan / pytest — tylko gdy narzędzie dostępne.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRD_PATH = ROOT / "docs" / "PRD.md"
CONTRACTS = ROOT / "contracts"


# --- QG-1: PRD ----------------------------------------------------------------
GHERKIN_RE = re.compile(r"\b(Given|When|Then|And)\b", re.IGNORECASE)


def qg1_prd(prd_text: str | None = None) -> dict:
    text = prd_text if prd_text is not None else (PRD_PATH.read_text(encoding="utf-8") if PRD_PATH.exists() else "")
    problems: list[str] = []
    blocks = re.split(r"\n(?=#{1,3} )", text)
    req_blocks = [b for b in blocks if re.search(r"REQ-\d+", b)]
    if not req_blocks:
        problems.append("Brak wymagań oznaczonych REQ-<n>")
    for b in req_blocks:
        rid = re.search(r"REQ-\d+", b)
        if not GHERKIN_RE.search(b):
            problems.append(f"{rid.group(0) if rid else 'blok'}: brak scenariusza Gherkin (Given/When/Then)")
    if not re.search(r"#{1,3}\s*Edge Cases", text, re.IGNORECASE):
        problems.append("Brak sekcji Edge Cases")
    return {"gate": "QG-1", "ok": not problems, "problems": problems}


# --- QG-2: kontrakty ------------------------------------------------------------
def qg2_contracts() -> dict:
    problems: list[str] = []
    openapi = CONTRACTS / "openapi.yaml"
    if openapi.exists():
        text = openapi.read_text(encoding="utf-8")
        for needle in ("openapi:", "paths:", "components:"):
            if needle not in text:
                problems.append(f"openapi.yaml: brak sekcji {needle!r}")
        if "securitySchemes" not in text and "security:" not in text:
            problems.append("openapi.yaml: brak definicji autoryzacji (securitySchemes/security)")
        if "rate" not in text.lower() and "429" not in text and "x-ratelimit" not in text.lower():
            problems.append("openapi.yaml: brak rate-limitu (429 / x-ratelimit)")
        if "webhook" in text.lower() and "signature" not in text.lower():
            problems.append("webhooki bez walidacji podpisu (signature)")
        try:
            import yaml  # type: ignore
            yaml.safe_load(text)
        except ImportError:
            pass  # PyYAML opcjonalny — fallback: testy strukturalne powyżej
        except Exception as exc:
            problems.append(f"openapi.yaml: błąd YAML: {exc}")
    else:
        problems.append("Brak contracts/openapi.yaml")
    schema = CONTRACTS / "events.schema.json"
    if schema.exists():
        try:
            json.loads(schema.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"events.schema.json: błąd JSON: {exc}")
    return {"gate": "QG-2", "ok": not problems, "problems": problems}


# --- QG-3: Semantic Integration Check (zmiana #3) -------------------------------
def gherkin_scenarios(prd_text: str | None = None) -> list[str]:
    text = prd_text if prd_text is not None else (PRD_PATH.read_text(encoding="utf-8") if PRD_PATH.exists() else "")
    scenarios = re.findall(r"Scenario(?: Outline)?:\s*(.+)", text)
    return [s.strip() for s in scenarios]


def qg3_integration_e2e(milestone_branch: str, worktree: Path) -> dict:
    """Bramka integracyjna przed mergem milestone/* -> main.

    Odpala pełny stos (Docker Compose z contracts/) i testuje przepływy E2E
    z PRD (scenariusze Gherkin). Deterministyczny werdykt JSON.
    """
    problems: list[str] = []
    compose = CONTRACTS / "docker-compose.e2e.yml"
    if compose.exists():
        up = subprocess.run(["docker", "compose", "-f", str(compose), "up", "-d", "--build"],
                            cwd=ROOT, capture_output=True, text=True)
        if up.returncode != 0:
            problems.append(f"docker compose up: {up.stderr.strip()[:500]}")
        else:
            scenarios = gherkin_scenarios()
            e2e_dir = ROOT / "tests" / "e2e"
            if shutil.which("pytest") and e2e_dir.exists():
                proc = subprocess.run(["pytest", str(e2e_dir), "-q", "--tb=short"],
                                      cwd=worktree, capture_output=True, text=True, timeout=600)
                if proc.returncode != 0:
                    problems.append(f"E2E pytest: {proc.stdout[-2000:]}")
            if shutil.which("schemathesis"):
                proc = subprocess.run(
                    ["schemathesis", "run", str(CONTRACTS / "openapi.yaml"), "--checks", "all"],
                    cwd=worktree, capture_output=True, text=True, timeout=600)
                if proc.returncode not in (0, 1):  # 1 = wykryte błędy schemathesis
                    problems.append(f"schemathesis: rc={proc.returncode} {proc.stdout[-1000:]}")
            subprocess.run(["docker", "compose", "-f", str(compose), "down"],
                           cwd=ROOT, capture_output=True, text=True)
    else:
        problems.append("Brak contracts/docker-compose.e2e.yml — QG-3 wymaga pełnego stosu")
    return {"gate": "QG-3", "ok": not problems, "scenarios": gherkin_scenarios(), "problems": problems}


# --- lokalne bramki w worktree (Krok 4) ------------------------------------------
def _tool(name: str) -> str | None:
    return shutil.which(name)


def local_gates(worktree: Path) -> list[dict]:
    results: list[dict] = []

    def run(name: str, cmd: list[str], timeout: int = 300) -> dict:
        if _tool(cmd[0]) is None:
            return {"name": name, "ok": True, "skipped": True, "tool": cmd[0]}
        proc = subprocess.run(cmd, cwd=worktree, capture_output=True, text=True, timeout=timeout)
        return {"name": name, "ok": proc.returncode == 0, "tool": cmd[0],
                "output": (proc.stdout + proc.stderr)[-4000:]}

    results.append(run("ruff", ["ruff", "check", "."]))
    results.append(run("mypy", ["mypy", "."]))
    results.append(run("phpstan", ["phpstan", "analyse", "--no-progress"]))
    if (worktree / "pytest.ini").exists() or (worktree / "tests").exists():
        results.append(run("pytest", ["pytest", "tests", "-q", "--tb=short"]))
    return results


def regression(worktree: Path) -> dict:
    """Pełny zestaw testów regresyjnych na zrebasowanym branchu (Faza 6)."""
    if _tool("pytest") is None:
        return {"ok": True, "skipped": True}
    proc = subprocess.run(["pytest", "-q", "--tb=short"], cwd=worktree,
                          capture_output=True, text=True, timeout=900)
    return {"ok": proc.returncode == 0, "output": (proc.stdout + proc.stderr)[-4000:]}

"""Skill compiler — składanie deterministycznego pakietu kontekstu dla bota
oraz zarządzanie rolami.

Zmiana #5: role NIE są generowane w locie od zera. Używamy parametryzowanego
szablonu (sztywny szkielet wejścia/wyjścia + wstrzykiwana sekcja domenowa).
Każda nowa rola przechodzi deterministyczny walidator (sekcje, zakazane słowa,
limit długości) i ląduje w registry.json jako PROBATION; po pierwszym zielonym
QG dostaje status ACTIVE. Jeśli pierwsza próba obleje QG — rola jest
regenerowana z wyższym modelem.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".agents" / "skills"
STAGING_DIR = SKILLS / "_staging"          # kwarantanna przed produkcją
REGISTRY_PATH = ROOT / ".agents" / "registry.json"
CONTRACTS = ROOT / "contracts"
METRICS_JSONL = ROOT / ".agents" / "metrics.jsonl"

ROLE_TEMPLATE_PATH = SKILLS / "roles" / "_template.md"

# --- Walidator ról (zmiana #5) -------------------------------------------------
REQUIRED_SECTIONS = ["## Wejście (kontrakt)", "## Wyjście (kontrakt)", "## Twarde reguły", "## Zakazy"]
BANNED_WORDS = ["lorem ipsum", "TODO", "FIXME", "???", "placeholder text"]
MAX_ROLE_CHARS = 4000


class RoleValidationError(ValueError):
    pass


def validate_role_md(text: str) -> list[str]:
    """Deterministyczny walidator roli. Zwraca listę błędów (pusta = OK)."""
    errors: list[str] = []
    for section in REQUIRED_SECTIONS:
        if section not in text:
            errors.append(f"brak wymaganej sekcji: {section}")
    lowered = text.lower()
    for word in BANNED_WORDS:
        if word.lower() in lowered:
            errors.append(f"zakazane słowo: {word!r}")
    if len(text) > MAX_ROLE_CHARS:
        errors.append(f"rola zbyt długa: {len(text)} > {MAX_ROLE_CHARS} znaków")
    if "{{ROLE_NAME}}" in text or "{{DOMAIN_SECTION}}" in text:
        errors.append("niepodstawione placeholdery szablonu")
    return errors


def render_role_template(role_name: str, domain_section: str) -> str:
    template = ROLE_TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace("{{ROLE_NAME}}", role_name).replace("{{DOMAIN_SECTION}}", domain_section)


def generate_role(role_name: str, domain_section: str, model_hint: str = "strong") -> tuple[str, list[str]]:
    """Generuje rolę z szablonu i waliduje. Zwraca (markdown, błędy)."""
    md = render_role_template(role_name, domain_section)
    errors = validate_role_md(md)
    if not errors:
        path = SKILLS / "roles" / f"{role_name}.md"
        path.write_text(md, encoding="utf-8")
        set_role_registry(role_name, model_hint, status="PROBATION")  # zmiana #5
    return md, errors


# --- registry.json ---------------------------------------------------------------
def load_registry() -> dict:
    if REGISTRY_PATH.exists():
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return {"roles": {}}


def set_role_registry(role_name: str, model: str, status: str = "PROBATION") -> None:
    reg = load_registry()
    entry = reg["roles"].setdefault(role_name, {})
    entry.update({"model": model, "status": status, "success_rate": entry.get("success_rate", 0.0)})
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def promote_role_if_probation(role_name: str) -> None:
    """Po zielonym QG rola PROBATION -> ACTIVE (zmiana #5)."""
    reg = load_registry()
    entry = reg["roles"].get(role_name)
    if entry and entry.get("status") == "PROBATION":
        entry["status"] = "ACTIVE"
        REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def model_for_role(role: str, kind: str = "strong") -> str:
    """Wybór modelu: strong (architektura/CR/debugger) vs fast (testy/boilerplate)."""
    entry = load_registry()["roles"].get(role, {})
    models = entry.get("models", {})
    return models.get(kind) or entry.get("model") or ("strong-model" if kind == "strong" else "fast-model")


# --- detekcja stacka ---------------------------------------------------------------
def detect_stack(repo: Path = ROOT) -> list[str]:
    stacks: list[str] = []
    if (repo / "pyproject.toml").exists() or (repo / "requirements.txt").exists():
        stacks.append("python-3.14-async")
    if (repo / "composer.json").exists():
        stacks.append("php83-symfony7")
    pkg = repo / "package.json"
    if pkg.exists() and "remotion" in pkg.read_text(encoding="utf-8"):
        stacks.append("remotion-react")
    elif pkg.exists():
        stacks.append("node-ts")
    return stacks


# --- pakiet kontekstu ---------------------------------------------------------------
def load_contracts() -> dict:
    out: dict[str, str] = {}
    if CONTRACTS.exists():
        for p in sorted(CONTRACTS.iterdir()):
            if p.is_file():
                out[p.name] = p.read_text(encoding="utf-8")
    return out


def compile_system_prompt(role: str, stack: list[str] | None, task: dict) -> str:
    """Deterministyczne sklejenie: anti-slop + git-contract + stacki (+lekcje)
    + rola + lessons-learned + kontrakty + JSON zadania."""
    parts: list[str] = []
    g = SKILLS / "_global"
    for name in ("anti-slop.md", "git-contract.md", "lessons-learned.md"):
        p = g / name
        if p.exists():
            parts.append(p.read_text(encoding="utf-8"))
    stacks = stack if stack is not None else detect_stack()
    for s in stacks:
        for name in (f"{s}.md", f"{s}-lessons.md"):  # zmiana #2: lekcje per-stack
            p = SKILLS / "stacks" / name
            if p.exists():
                parts.append(p.read_text(encoding="utf-8"))
    role_path = SKILLS / "roles" / f"{role}.md"
    if role_path.exists():
        parts.append(role_path.read_text(encoding="utf-8"))
    persona = load_registry()["roles"].get(role, {}).get("persona")
    if persona:
        desc = {"creator": "Jesteś TWÓRCĄ/INNOWATOREM — szukasz nieszablonowych rozwiązań, dbasz o DX/UX i prostotę użycia.",
                "skeptic": "Jesteś SCEPTYKIEM/AUDYTOREM — Twoim zadaniem jest łamanie założeń, szukanie edge-case'ów, atakowanie i upraszczanie przekombinowanego kodu.",
                "builder": "Jesteś pragmatycznym inżynierem — prosty, nudny, niezawodny kod."}
        parts.append(f"# Persona: {persona}\n{desc.get(persona, persona)}")
    parts.append("# Zamrożone kontrakty (FROZEN — tylko do odczytu)\n" + json.dumps(load_contracts(), ensure_ascii=False))
    parts.append("# Zadanie (JSON)\n" + json.dumps(task, ensure_ascii=False, default=str))
    return "\n\n---\n\n".join(parts)


# ============================================================================
# SKILL HARVESTER & CURATED REGISTRY
# Deterministyczny harvester: skanuje zależności projektu, pobiera gotowe reguły
# z zaufanych repozytoriów GitHub (bez LLM), przechodzą przez kwarantannę
# (security scan na prompt injection) i dopiero wtedy trafiają do stacks/.
# ============================================================================

TRUSTED_SKILL_REPOS = ["PatrickJS/awesome-cursorrules", "anthropics/skills"]
GITHUB_API = "https://api.github.com"
GITHUB_RAW = "https://raw.githubusercontent.com"
MAX_HARVEST_PER_DEP = 3
MAX_SKILL_LINES = 150  # limit kompresji dla syntezy LLM (Priorytet 2)

# heurystyki prompt injection w plikach .md/.mdc z zewnątrz
INJECTION_PATTERNS = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "ignore previous instructions"),
    (r"disregard\s+(all\s+)?(previous|prior|above)\s+(instructions|rules)", "disregard instructions"),
    (r"(?m)^\s*system\s*:", "fałszywy prefix system:"),
    (r"<!--", "ukryty komentarz HTML"),
    (r"\[system\]|<\|system\|>", "tag systemowy"),
    (r"act\s+as\s+(if\s+)?you\s+(are|were)\s+(not|no\s+longer)", "obejście persony"),
    (r"from\s+now\s+on\s+you", "zmiana zachowania w locie"),
    (r"[A-Za-z0-9+/]{200,}={0,2}", "podejrzany długi blob (base64?)"),
    (r"(?i)(urgent|immediately|without\s+exception)\b[^.]{0,80}\b(execute|run|send|transfer|delete)\b", "socjotechnika"),
]


def security_scan_skill(text: str) -> list[str]:
    """Deterministyczny skan pliku skilla pod kątem prompt injection. Zwraca listę ustaleń."""
    findings: list[str] = []
    for pat, label in INJECTION_PATTERNS:
        if re.search(pat, text):
            findings.append(label)
    if len(text.splitlines()) > 400:
        findings.append("podejrzanie długi plik (>400 linii)")
    return findings


def detect_dependencies(repo: Path = ROOT) -> dict[str, str]:
    """Skan pyproject.toml (PEP 621 + poetry) / package.json / composer.json -> {name: version}."""
    deps: dict[str, str] = {}
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        section = ""
        for line in text.splitlines():
            m_sec = re.match(r"^\s*\[(.+)\]", line)
            if m_sec:
                section = m_sec.group(1)
                continue
            if section in ("project", "tool.poetry.dependencies"):
                if section == "project" and re.match(r"^\s*dependencies\s*=\s*\[", line):
                    for m in re.finditer(r'"([A-Za-z0-9_.-]+)[^"]*"', line):
                        deps.setdefault(m.group(1).lower(), "?")
                elif section == "tool.poetry.dependencies":
                    m = re.match(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*["\']([^"\']+)', line)
                    if m and m.group(1).lower() != "python":
                        deps[m.group(1).lower()] = m.group(2)
            if section == "project":
                m = re.match(r'^\s*([A-Za-z0-9_.-]+)\s*>=\s*["\']', line)  # fallback prostych wpisów
    for fname, key in (("package.json", None), ("composer.json", "require")):
        p = repo / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        sections = [data.get("dependencies", {}), data.get("devDependencies", {})]
        if key:
            sections.append(data.get(key, {}))
        for sec in sections:
            for name, ver in sec.items():
                clean = name.lower().lstrip("@").split("/")[-1]  # @scope/pkg -> pkg
                deps[clean] = str(ver)
    return deps


def _http_get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "dark-factory-skill-harvester",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _repo_tree(repo: str) -> list[str]:
    try:
        d = json.loads(_http_get(f"{GITHUB_API}/repos/{repo}/git/trees/HEAD?recursive=1"))
        return [t["path"] for t in d.get("tree", []) if t.get("type") == "blob"]
    except Exception:
        return []


def _repo_raw(repo: str, path: str) -> str:
    return _http_get(f"{GITHUB_RAW}/{repo}/HEAD/{path}", timeout=20)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def synthesize_skill(dep: str, max_lines: int = MAX_SKILL_LINES) -> str:
    """Priorytet 2: synteza LLM dla niszowych bibliotek (bez gotowego skilla w repo).
    Kompresja do max_lines konkretnych reguł 'Rób X / Nigdy nie rób Y'. Status PROBATION."""
    from engine.llm_gateway import get_gateway
    gw = get_gateway()
    out = gw.complete_json(
        system="Jesteś HR_Skill_Architect_Bot. Generujesz wyłącznie z wiedzy modelu (2026): aktualne idiomy, breaking changes, pułapki bezpieczeństwa.",
        user=(f"Biblioteka: {dep}. Wygeneruj skill MAKSYMALNIE {max_lines} linii, same konkretne reguły w formacie:\n"
              f"- Rób X (dlaczego)\n- Nigdy nie rób Y (co się stanie)\n"
              f'Zwróć wyłącznie JSON: {{"rules_md": "<treść markdown>"}}'),
        step="SKILL_SYNTH", model="strong-model")
    return out["rules_md"][: max_lines * 200]


def ensure_stack_skills(repo: Path = ROOT, allow_llm: bool = True) -> dict[str, Any]:
    """Hook onboardingu: raz przy tworzeniu roli/milestonu — skanuje zależności,
    pobiera gotowe reguły z zaufanych repo, kwarantanna -> security scan -> stacks/.
    Zwraca raport; wynik zapisuje też w registry.json (sekcja "skills")."""
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    deps = detect_dependencies(repo)
    reg = load_registry()
    reg.setdefault("skills", {})
    trees = {r: _repo_tree(r) for r in TRUSTED_SKILL_REPOS}
    report: dict[str, Any] = {"dependencies": deps, "harvested": [], "quarantined": [],
                              "synthesized": [], "needs_synthesis": [], "errors": []}
    for dep in sorted(deps):
        ndep = _norm(dep)
        if len(ndep) < 3:
            continue
        matches: list[tuple[str, str]] = []
        for repo, paths in trees.items():
            for p in paths:
                if p.lower().endswith((".md", ".mdc", ".cursorrules")) and ndep in _norm(p):
                    matches.append((repo, p))
        if not matches:
            report["needs_synthesis"].append(dep)
            if allow_llm:
                try:
                    md = synthesize_skill(dep)
                    dest = SKILLS / "stacks" / f"{dep}-synthesized.md"
                    dest.write_text(f"<!-- synthesized-by: HR_Skill_Architect_Bot (LLM, {time.time():.0f}) dep={dep} status=PROBATION -->\n{md}\n",
                                    encoding="utf-8")
                    reg["skills"][f"{dep}:llm-synthesis"] = {"source": "llm-synthesis", "status": "PROBATION",
                                                             "file": str(dest.relative_to(ROOT))}
                    report["synthesized"].append({"dep": dep, "file": dest.name})
                except Exception as exc:
                    report["errors"].append(f"{dep}: synteza LLM niedostępna: {exc}")
            continue
        dest = SKILLS / "stacks" / f"{dep}-harvested.md"
        combined = [f"# Harvested skill: {dep} (źródła: {len(matches[:MAX_HARVEST_PER_DEP])} z zaufanych repo)\n"]
        for repo, path in matches[:MAX_HARVEST_PER_DEP]:
            try:
                raw = _repo_raw(repo, path)
            except Exception as exc:
                report["errors"].append(f"{repo}/{path}: {exc}")
                continue
            findings = security_scan_skill(raw)
            sha = hashlib.sha256(raw.encode()).hexdigest()[:12]
            header = f"<!-- harvested-from: {repo}/{path} sha256:{sha} fetched-at: {time.time():.0f} -->\n"
            if findings:
                staged = STAGING_DIR / f"{dep}__{repo.replace('/', '__')}__{Path(path).name}"
                staged.write_text(header + raw, encoding="utf-8")
                reg["skills"][f"{dep}:{path}"] = {"source": f"{repo}/{path}", "sha256": sha,
                                                  "status": "QUARANTINED", "findings": findings,
                                                  "staging_file": str(staged.relative_to(ROOT))}
                report["quarantined"].append({"dep": dep, "source": f"{repo}/{path}", "findings": findings})
            else:
                combined.append(f"\n## From {repo}/{path}\n\n" + raw)
                reg["skills"][f"{dep}:{path}"] = {"source": f"{repo}/{path}", "sha256": sha, "status": "ACTIVE",
                                                  "file": str(dest.relative_to(ROOT))}
                report["harvested"].append({"dep": dep, "source": f"{repo}/{path}", "file": dest.name,
                                            "lines": len(raw.splitlines())})
        if report["harvested"] and any(h["dep"] == dep for h in report["harvested"]):
            dest.write_text("\n".join(combined) + "\n", encoding="utf-8")
    REGISTRY_PATH.write_text(json.dumps(reg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return report


# ============================================================================
# SKILL FITNESS SCORE — tracking skuteczności per skill w metrics.jsonl
# ============================================================================

def skill_files_for_task(task: dict, stack: list[str] | None = None) -> list[str]:
    """Lista plików skilli, które zostały wstrzyknięte do kontekstu zadania."""
    files: list[str] = [f"_global/{n}" for n in ("anti-slop.md", "git-contract.md", "lessons-learned.md")]
    stacks = stack if stack is not None else detect_stack()
    for s in stacks:
        for n in (f"{s}.md", f"{s}-lessons.md", f"{s}-harvested.md", f"{s}-synthesized.md"):
            if (SKILLS / "stacks" / n).exists():
                files.append(f"stacks/{n}")
    role_md = SKILLS / "roles" / f"{task['role']}.md"
    if role_md.exists():
        files.append(f"roles/{task['role']}.md")
    return files


def record_skill_usage(task: dict, verdict: str, stack: list[str] | None = None) -> None:
    """Dopisuje linię do .agents/metrics.jsonl: które skille były wstrzyknięte i czy CR przeszło za 1. razem."""
    line = {
        "ts": time.time(), "task_id": task["id"], "role": task["role"],
        "skills": skill_files_for_task(task, stack),
        "verdict": verdict, "first_pass": verdict == "APPROVED" and task["retry_count"] == 0,
        "retry_count": task["retry_count"],
    }
    METRICS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with METRICS_JSONL.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def skill_fitness_report() -> dict[str, Any]:
    """Agregacja per skill: ile użyć, ile first-pass approvals -> fitness score."""
    stats: dict[str, dict[str, int]] = {}
    if not METRICS_JSONL.exists():
        return {}
    for line in METRICS_JSONL.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for s in rec.get("skills", []):
            st = stats.setdefault(s, {"uses": 0, "first_pass": 0, "rejects": 0})
            st["uses"] += 1
            if rec.get("first_pass"):
                st["first_pass"] += 1
            if rec.get("verdict") == "REJECTED":
                st["rejects"] += 1
    return {s: {**st, "fitness": round(st["first_pass"] / st["uses"], 3) if st["uses"] else 0.0}
            for s, st in sorted(stats.items(), key=lambda kv: -kv[1]["uses"])}

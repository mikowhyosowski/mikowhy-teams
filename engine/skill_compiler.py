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

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".agents" / "skills"
REGISTRY_PATH = ROOT / ".agents" / "registry.json"
CONTRACTS = ROOT / "contracts"

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
    parts.append("# Zamrożone kontrakty (FROZEN — tylko do odczytu)\n" + json.dumps(load_contracts(), ensure_ascii=False))
    parts.append("# Zadanie (JSON)\n" + json.dumps(task, ensure_ascii=False, default=str))
    return "\n\n---\n\n".join(parts)

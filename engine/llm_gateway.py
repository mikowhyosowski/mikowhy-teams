"""Jedyna bramka silnika do LLM. Sam silnik pozostaje deterministyczny (0 tokenów) —
wszystkie wywołania modeli dzieją się wyłącznie przez ten moduł, w mikro-etapach
workera/CR, nigdy w orchestratorze.

Provider wybierany zmienną środowiskową LLM_PROVIDER:
    vibe  — lokalny CLI Mistral Vibe w trybie headless (domyślny, gdy binary `vibe` jest w PATH)
    stub  — deterministyczny błąd "provider not configured"

Modele (zmienne środowiskowe, opcjonalnie):
    VIBE_FAST_MODEL   — szybki model do testów/boilerplate'u (default: zai-glm-5-3-flash, fast SWE)
    VIBE_STRONG_MODEL — mocny model do architektury/CR/debuggera (default: mistral-large-4-0)
Role z registry.json używają nazw semantycznych "fast-model"/"strong-model" —
gateway mapuje je na aliasy CLI.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / ".agents" / "runtime"

MODEL_ALIASES = {
    "fast-model": os.environ.get("VIBE_FAST_MODEL", "zai-glm-5-3-flash"),
    "strong-model": os.environ.get("VIBE_STRONG_MODEL", "mistral-large-4-0"),
}


class LLMGateway:
    def complete(self, *, system: str, user: str, step: str, model: str) -> str:
        raise NotImplementedError

    def complete_json(self, *, system: str, user: str, step: str, model: str) -> dict[str, Any]:
        raw = self.complete(system=system, user=user, step=step, model=model)
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError(f"LLM nie zwrócił JSON w kroku {step}: {raw[:200]!r}")
        return json.loads(raw[start : end + 1])


class StubGateway(LLMGateway):
    def complete(self, *, system: str, user: str, step: str, model: str) -> str:
        raise RuntimeError(
            f"LLM provider nie skonfigurowany (LLM_PROVIDER={os.environ.get('LLM_PROVIDER', 'stub')!r}). "
            f"Podłącz provider w engine/llm_gateway.py, aby uruchomić mikro-etap {step}."
        )


class VibeCliGateway(LLMGateway):
    """Provider `vibe`: lokalny CLI Mistral Vibe w trybie headless.

    Wywołanie: vibe --trust --max-turns 1 --model <alias> --output text
               --enabled-tools __none__ --system-prompt-file <sys> --prompt-file <user>
    `--enabled-tools __none__` wyłącza wszystkie narzędzia — bot może tylko odpowiedzieć tekstem
    (żadnych komend, żadnej edycji plików; tym zajmuje się deterministyczny silnik).
    """

    def __init__(self, binary: str = "vibe", timeout: int = 300):
        self.binary = binary
        self.timeout = timeout

    def complete(self, *, system: str, user: str, step: str, model: str) -> str:
        alias = MODEL_ALIASES.get(model, model)  # "fast-model"/"strong-model" -> alias CLI
        RUNTIME.mkdir(parents=True, exist_ok=True)
        safe_step = "".join(c if c.isalnum() or c in "-_" else "_" for c in step)
        with tempfile.TemporaryDirectory(dir=RUNTIME, prefix=f"gw-{safe_step}-") as tmp:
            sys_file = Path(tmp) / "system.md"
            user_file = Path(tmp) / "user.md"
            sys_file.write_text(system, encoding="utf-8")
            user_file.write_text(user, encoding="utf-8")
            cmd = [
                self.binary, "--trust", "--max-turns", "1",
                "--model", alias, "--output", "text",
                "--enabled-tools", "__none__",
                "--system-prompt-file", str(sys_file),
                "--prompt-file", str(user_file),
                "--time-limit", str(self.timeout),
            ]
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=self.timeout + 60)
        if proc.returncode != 0:
            raise RuntimeError(
                f"vibe CLI rc={proc.returncode} (model={alias}, step={step}): {proc.stderr.strip()[-800:]}"
            )
        return proc.stdout.strip()


_REGISTRY: dict[str, type[LLMGateway]] = {"stub": StubGateway, "vibe": VibeCliGateway}


def register_gateway(name: str, cls: type[LLMGateway]) -> None:
    _REGISTRY[name] = cls


def get_gateway() -> LLMGateway:
    name = os.environ.get("LLM_PROVIDER")
    if not name:
        name = "vibe" if shutil.which("vibe") else "stub"  # vibe CLI domyślnie, gdy dostępny
    cls = _REGISTRY.get(name)
    if cls is None:
        raise RuntimeError(f"Nieznany LLM_PROVIDER={name!r}; dostępne: {sorted(_REGISTRY)}")
    return cls()

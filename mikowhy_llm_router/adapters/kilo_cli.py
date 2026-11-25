"""KiloCliAdapter — provider using Kilo CLI (kilo.ai) as LLM backend."""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any, Type

from engine.ports import ReasoningEnginePort


class KiloCliAdapter:
    """
    Adapter ReasoningEnginePort używający Kilo CLI.
    
    Wymaga: `kilo` CLI zainstalowany i skonfigurowany (kilo.ai)
    Konfiguracja przez env vars:
    - KILO_MODEL: model do użycia (domyślnie: auto)
    - KILO_TIMEOUT: timeout w sekundach (domyślnie: 120)
    
    Użycie:
        LLM_PROVIDER=kilo_cli python -m engine.orchestrator run
    """

    def __init__(
        self,
        model: str | None = None,
        timeout: float = 120.0,
    ):
        import os
        self.model = model or os.environ.get("KILO_MODEL", "")
        self.timeout = timeout
        self._cli_path: str | None = None  # Lazy init

    def _ensure_cli(self) -> str:
        """Znajdź kilo CLI w PATH (lazy init)."""
        if self._cli_path is None:
            cli = shutil.which("kilo")
            if not cli:
                raise RuntimeError("Kilo CLI not found in PATH. Install with: pip install kilo-cli")
            self._cli_path = cli
        return self._cli_path

    def complete_text(self, *, system: str, user: str, step: str, model: str = "") -> str:
        """Wykonaj complete_text przez Kilo CLI."""
        prompt = self._build_prompt(system, user, step)
        return self._run_kilo(prompt, model or self.model)

    def complete_structured(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any:
        """Wykonaj complete_structured przez Kilo CLI z wymuszeniem JSON."""
        # Pobierz nazwy pól z schematu jeśli to Pydantic model
        field_names = ""
        if isinstance(schema, type) and hasattr(schema, "model_fields"):
            field_names = ", ".join(schema.model_fields.keys())
        
        json_instruction = (
            f"\n\nODPOWIEDZ WYŁĄCZNIE POPRAWNYM JSON-em z polami: {field_names if field_names else 'wymaganymi przez schemat'}. "
            "Żadnego dodatkowego tekstu, komentarzy ani formatowania markdown. "
            "Nie dodawaj pól, których nie ma w schemacie."
        )
        prompt = self._build_prompt(system + json_instruction, user, step)
        raw_output = self._run_kilo(prompt, model or self.model)
        
        try:
            raw = json.loads(raw_output)
        except json.JSONDecodeError:
            # Fallback: spróbuj wyciągnąć JSON z outputu
            import re
            json_match = re.search(r'\{.*\}', raw_output, re.DOTALL)
            if json_match:
                raw = json.loads(json_match.group())
            else:
                raise ValueError(f"Kilo CLI nie zwrócił poprawnego JSON: {raw_output[:200]}")
        
        if schema is dict or schema is Any:
            return raw
        if isinstance(schema, type) and hasattr(schema, "model_validate"):
            return schema.model_validate(raw)
        return schema(**raw)

    def health_check(self) -> bool:
        """Sprawdź czy Kilo CLI działa."""
        try:
            cli = self._ensure_cli()
            result = subprocess.run(
                [cli, "--version"],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _build_prompt(self, system: str, user: str, step: str) -> str:
        """Zbuduj prompt dla Kilo CLI."""
        return f"""<system>
{system}
</system>

<user>
{user}
</user>

<step>
{step}
</step>"""

    def _run_kilo(self, prompt: str, model: str = "") -> str:
        """Uruchom Kilo CLI z promptem."""
        cli = self._ensure_cli()
        # Używamy `kilo run` zamiast `kilo chat` - `run` akceptuje wiadomość jako argument pozycyjny
        cmd = [cli, "run", prompt]
        if model:
            cmd.extend(["--model", model])
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            if result.returncode != 0:
                raise RuntimeError(f"Kilo CLI failed (rc={result.returncode}): {result.stderr}")
            # Usuwamy linię z modelem (np. "> code · nvidia/nemotron-3-ultra-550b-a55b:free")
            lines = result.stdout.strip().split('\n')
            if lines and lines[0].startswith('> code'):
                lines = lines[1:]
            return '\n'.join(lines).strip()
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Kilo CLI timeout after {self.timeout}s")
        except FileNotFoundError:
            raise RuntimeError("Kilo CLI not found. Install with: curl -fsSL https://kilo.ai/cli/install | bash")


# Entry point registration (dodawane do pyproject.toml mikowhy-llm-router):
# "llm:kilo_cli" = "mikowhy_llm_router.adapters.kilo_cli:KiloCliAdapter"
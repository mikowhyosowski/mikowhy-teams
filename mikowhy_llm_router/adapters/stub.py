"""StubLlmAdapter — provider stub (deterministyczny echo do testów, bez LLM)."""
from __future__ import annotations

import json
from typing import Any, Type

from engine.ports import ReasoningEnginePort


class StubLlmAdapter:
    """Provider stub — deterministyczny echo do testów (bez LLM)."""

    def complete_text(self, *, system: str, user: str, step: str, model: str = "") -> str:
        return json.dumps({"step": step, "echo": user[:200]}, ensure_ascii=False)

    def complete_structured(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any:
        # Zwracamy słownik z polami, które Pydantic model może oczekiwać
        raw = {"step": step, "echo": user[:200], "value": 42}
        if isinstance(schema, type) and hasattr(schema, "model_validate"):
            # Spróbuj zvalidować, jeśli brakuje pól, dodaj domyślne
            try:
                return schema.model_validate(raw)
            except Exception:
                # Jeśli validacja się nie uda, zwróć raw jako dict
                return raw
        if schema is dict or schema is Any:
            return raw
        return schema(**raw)

    def health_check(self) -> bool:
        return True
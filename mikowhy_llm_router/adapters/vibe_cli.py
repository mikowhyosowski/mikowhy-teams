"""VibeCliAdapter — provider vibe_cli delegujący do engine/llm_gateway.py."""
from __future__ import annotations

from typing import Any, Type

from engine.ports import ReasoningEnginePort


class VibeCliAdapter:
    """Provider vibe_cli — deleguje do rejestru gatewayów w engine/llm_gateway.py."""

    def __init__(self):
        from engine import llm_gateway
        self._gw = llm_gateway.get_gateway()

    def complete_text(self, *, system: str, user: str, step: str, model: str = "") -> str:
        return self._gw.complete(system=system, user=user, step=step, model=model)

    def complete_structured(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any:
        raw = self._gw.complete_json(system=system, user=user, step=step, model=model)
        if schema is dict or schema is Any:
            return raw
        if isinstance(schema, type) and hasattr(schema, "model_validate"):
            return schema.model_validate(raw)
        return schema(**raw)

    def health_check(self) -> bool:
        try:
            from engine import llm_gateway
            return llm_gateway.get_gateway() is not None
        except Exception:
            return False
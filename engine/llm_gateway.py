"""Jedyna bramka silnika do LLM. Sam silnik pozostaje deterministyczny (0 tokenów) —
wszystkie wywołania modeli dzieją się wyłącznie przez ten moduł, w mikro-etapach
workera/CR, nigdy w orchestratorze.

Provider wybierany zmienną środowiskową LLM_PROVIDER:
    stub (domyślnie) — deterministyczny błąd "provider not configured"
    dowolny inny     — kontrakt: klasa z metodą complete(system, user, step, model) -> str
"""
from __future__ import annotations

import json
import os
from typing import Any


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


_REGISTRY: dict[str, type[LLMGateway]] = {"stub": StubGateway}


def register_gateway(name: str, cls: type[LLMGateway]) -> None:
    _REGISTRY[name] = cls


def get_gateway() -> LLMGateway:
    name = os.environ.get("LLM_PROVIDER", "stub")
    cls = _REGISTRY.get(name)
    if cls is None:
        raise RuntimeError(f"Nieznany LLM_PROVIDER={name!r}; dostępne: {sorted(_REGISTRY)}")
    return cls()

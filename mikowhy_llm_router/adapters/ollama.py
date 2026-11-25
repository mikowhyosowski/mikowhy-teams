"""OllamaAdapter — provider lokalny Ollama (HTTP API)."""
from __future__ import annotations

import json
import os
from typing import Any, Type

import httpx
from pydantic import BaseModel

from engine.ports import ReasoningEnginePort


class OllamaAdapter:
    """
    Adapter ReasoningEnginePort używający lokalnego Ollama.
    
    Wymaga: OLLAMA_BASE_URL (domyślnie http://localhost:11434)
    Opcjonalnie: OLLAMA_MODEL (domyślny model), OLLAMA_TIMEOUT
    
    Ollama API: /api/generate, /api/chat
    """

    def __init__(
        self,
        base_url: str | None = None,
        default_model: str | None = None,
        timeout: float = 120.0,
    ):
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.default_model = default_model or os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
            )
        return self._client

    async def _close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def complete_text(self, *, system: str, user: str, step: str, model: str = "") -> str:
        import asyncio
        return asyncio.run(self._complete_text_async(system=system, user=user, step=step, model=model))

    async def _complete_text_async(self, *, system: str, user: str, step: str, model: str = "") -> str:
        client = self._get_client()
        
        # Ollama używa /api/chat z formatem messages
        payload = {
            "model": model or self.default_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        resp = await client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["message"]["content"]

    def complete_structured(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any:
        import asyncio
        return asyncio.run(self._complete_structured_async(schema=schema, system=system, user=user, step=step, model=model))

    async def _complete_structured_async(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any:
        client = self._get_client()
        
        json_instruction = (
            "\n\nODPOWIEDZ WYŁĄCZNIE POPRAWNYM JSON-em zgodnym ze schematem. "
            "Żadnego dodatkowego tekstu, komentarzy ani formatowania markdown."
        )
        
        payload = {
            "model": model or self.default_model,
            "messages": [
                {"role": "system", "content": system + json_instruction},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
            "format": "json",  # Ollama format=json wymusza JSON output
        }
        resp = await client.post("/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data["message"]["content"]
        
        raw = json.loads(content)
        if schema is dict or schema is Any:
            return raw
        if isinstance(schema, type) and hasattr(schema, "model_validate"):
            return schema.model_validate(raw)
        return schema(**raw)

    def health_check(self) -> bool:
        import asyncio
        return asyncio.run(self._health_check_async())

    async def _health_check_async(self) -> bool:
        try:
            client = self._get_client()
            resp = await client.get("/api/tags", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
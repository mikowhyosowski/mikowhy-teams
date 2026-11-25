"""LiteLLMAdapter — provider LiteLLM (HTTP API do wielu modeli: OpenAI, Anthropic, Azure, Bedrock, itd.)."""
from __future__ import annotations

import json
import os
from typing import Any, Type

import httpx
from pydantic import BaseModel

from engine.ports import ReasoningEnginePort


class LiteLLMAdapter:
    """
    Adapter ReasoningEnginePort używający LiteLLM proxy/server.
    
    Wymaga: LITELLM_BASE_URL (np. http://localhost:4000) lub LITELLM_API_KEY
    Opcjonalnie: LITELLM_MODEL (domyślny model), LITELLM_TIMEOUT
    
    LiteLLM obsługuje format OpenAI-compatible: /chat/completions
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        default_model: str | None = None,
        timeout: float = 60.0,
    ):
        self.base_url = (base_url or os.environ.get("LITELLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LITELLM_API_KEY", "")
        self.default_model = default_model or os.environ.get("LITELLM_MODEL", "gpt-4o-mini")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout,
            )
        return self._client

    async def _close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def complete_text(self, *, system: str, user: str, step: str, model: str = "") -> str:
        """Synchroniczna wersja — używa httpx w bloku (dla prostoty, w prod warto async)."""
        import asyncio
        return asyncio.run(self._complete_text_async(system=system, user=user, step=step, model=model))

    async def _complete_text_async(self, *, system: str, user: str, step: str, model: str = "") -> str:
        client = self._get_client()
        payload = {
            "model": model or self.default_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        resp = await client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    def complete_structured(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any:
        import asyncio
        return asyncio.run(self._complete_structured_async(schema=schema, system=system, user=user, step=step, model=model))

    async def _complete_structured_async(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "") -> Any:
        client = self._get_client()
        
        # Dodaj instrukcję JSON do system prompt
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
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        resp = await client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        
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
            resp = await client.get("/health", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            # Fallback: spróbuj prosty request
            try:
                client = self._get_client()
                resp = await client.post(
                    "/chat/completions",
                    json={"model": self.default_model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                    timeout=10.0,
                )
                return resp.status_code == 200
            except Exception:
                return False
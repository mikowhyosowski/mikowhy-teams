"""Mikowhy LLM Router - adapters + router."""
from __future__ import annotations

from .router import LLMRouter, SyncLLMRouter
from .adapters.vibe_cli import VibeCliAdapter
from .adapters.stub import StubLlmAdapter
from .adapters.litellm import LiteLLMAdapter
from .adapters.ollama import OllamaAdapter

__all__ = [
    "LLMRouter",
    "SyncLLMRouter",
    "VibeCliAdapter",
    "StubLlmAdapter",
    "LiteLLMAdapter",
    "OllamaAdapter",
]
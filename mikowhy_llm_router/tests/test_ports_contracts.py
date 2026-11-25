"""Testy kontraktowe ReasoningEnginePort (stub — pełne, inne — import + health_check)."""
from __future__ import annotations

import pytest

from engine.ports import ReasoningEnginePort


def make_stub_adapter():
    from mikowhy_llm_router.adapters.stub import StubLlmAdapter
    return StubLlmAdapter()


def make_vibe_cli_adapter():
    from mikowhy_llm_router.adapters.vibe_cli import VibeCliAdapter
    return VibeCliAdapter()


def make_litellm_adapter():
    from mikowhy_llm_router.adapters.litellm import LiteLLMAdapter
    return LiteLLMAdapter(base_url="http://localhost:4000", api_key="test")


def make_ollama_adapter():
    from mikowhy_llm_router.adapters.ollama import OllamaAdapter
    return OllamaAdapter(base_url="http://localhost:11434")


# Pełne testy tylko dla stub (działa bez zewnętrznych serwerów)
class TestStubAdapter:
    """Pełne testy kontraktowe dla StubLlmAdapter."""
    
    @pytest.fixture
    def llm(self):
        return make_stub_adapter()
    
    def test_implements_port(self, llm):
        assert isinstance(llm, ReasoningEnginePort)
    
    def test_health_check(self, llm):
        assert llm.health_check() is True
    
    def test_complete_text(self, llm):
        result = llm.complete_text(system="sys", user="user", step="test_step", model="test-model")
        assert isinstance(result, str)
        assert "test_step" in result
    
    def test_complete_structured_dict(self, llm):
        result = llm.complete_structured(dict, system="sys", user="user", step="test_step", model="test-model")
        assert isinstance(result, dict)
        assert result["step"] == "test_step"
    
    def test_complete_structured_pydantic(self, llm):
        from pydantic import BaseModel
        
        class TestSchema(BaseModel):
            step: str
            value: int
        
        result = llm.complete_structured(TestSchema, system="sys", user="user", step="test_step", model="test-model")
        assert isinstance(result, TestSchema)
        assert result.step == "test_step"
        assert result.value == 42


# Testy importu i health_check dla pozostałych adapterów (bez wywoływania LLM)
class TestOtherAdaptersImport:
    """Testy że adaptery się importują i mają health_check."""
    
    @pytest.mark.parametrize("make_adapter,name", [
        (make_vibe_cli_adapter, "vibe_cli"),
        (make_litellm_adapter, "litellm"),
        (make_ollama_adapter, "ollama"),
    ])
    def test_import_and_port(self, make_adapter, name):
        llm = make_adapter()
        assert isinstance(llm, ReasoningEnginePort), f"{name} nie implementuje ReasoningEnginePort"
    
    @pytest.mark.parametrize("make_adapter,name", [
        (make_vibe_cli_adapter, "vibe_cli"),
        (make_litellm_adapter, "litellm"),
        (make_ollama_adapter, "ollama"),
    ])
    def test_health_check_no_crash(self, make_adapter, name):
        """health_check nie powinien crashować (może zwrócić False)."""
        llm = make_adapter()
        result = llm.health_check()
        assert isinstance(result, bool), f"{name}.health_check() zwróciło {type(result)}"
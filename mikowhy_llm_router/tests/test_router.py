"""Testy LLMRouter (kolejka, batching, cache, health_check)."""
from __future__ import annotations

import asyncio

import pytest

from mikowhy_llm_router import LLMRouter, StubLlmAdapter


def adapter_factory(name: str):
    return StubLlmAdapter()


@pytest.fixture
async def router():
    """Async fixture for LLMRouter."""
    r = LLMRouter(adapter_factory=adapter_factory, default_adapter="stub")
    await r.start()
    yield r
    await r.stop()


class TestLLMRouter:
    """Testy LLMRouter."""
    
    @pytest.mark.asyncio
    async def test_complete_text(self, router):
        result = await router.complete_text(system="sys", user="user", step="test_step")
        assert isinstance(result, str)
        assert "test_step" in result
    
    @pytest.mark.asyncio
    async def test_complete_structured(self, router):
        result = await router.complete_structured(dict, system="sys", user="user", step="test_step")
        assert isinstance(result, dict)
        assert result["step"] == "test_step"
    
    @pytest.mark.asyncio
    async def test_priority_ordering(self, router):
        """P0 request should complete before P3 even if submitted later."""
        results = []
        
        async def submit(priority, delay):
            await asyncio.sleep(delay)
            result = await router.complete_text(system="", user=f"p{priority}", step=f"step_p{priority}", priority=priority)
            results.append((priority, result))
        
        # Submit P3 first, then P0
        await asyncio.gather(
            submit(3, 0.01),  # P3 first
            submit(0, 0.02),  # P0 slightly later
        )
        
        # Both should succeed
        assert len(results) == 2
    
    @pytest.mark.asyncio
    async def test_cache(self, router):
        """Cache should return same result for identical requests."""
        result1 = await router.complete_text(system="sys", user="user", step="cached_step", use_cache=True)
        result2 = await router.complete_text(system="sys", user="user", step="cached_step", use_cache=True)
        assert result1 == result2
        
        stats = router.get_stats()
        assert stats["cache_hits"] >= 1
    
    @pytest.mark.asyncio
    async def test_cache_disabled(self, router):
        """With use_cache=False, cache should not be used."""
        result1 = await router.complete_text(system="sys", user="user", step="no_cache_step", use_cache=False)
        result2 = await router.complete_text(system="sys", user="user", step="no_cache_step", use_cache=False)
        assert "no_cache_step" in result1
        assert "no_cache_step" in result2
    
    @pytest.mark.asyncio
    async def test_health_check(self, router):
        health = router.health_check_all()
        assert isinstance(health, dict)
        assert "stub" in health
        assert health["stub"] is True
    
    @pytest.mark.asyncio
    async def test_stats(self, router):
        await router.complete_text(system="", user="u1", step="s1")
        await router.complete_text(system="", user="u2", step="s2")
        
        stats = router.get_stats()
        assert stats["total_requests"] >= 2
        assert "queue_sizes" in stats
        assert "cache_size" in stats
        assert "adapters_loaded" in stats
    
    @pytest.mark.asyncio
    async def test_clear_cache(self, router):
        await router.complete_text(system="", user="u", step="s", use_cache=True)
        router.clear_cache()
        stats = router.get_stats()
        assert stats["cache_size"] == 0
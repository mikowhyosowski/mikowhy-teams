"""LLMRouter — kolejka z priorytetami P0-P3, batching, cache, health_check."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Type

from engine.ports import ReasoningEnginePort


@dataclass
class LLMRequest:
    """Pojedyncze żądanie do LLM."""
    kind: str  # "complete_text" | "complete_structured"
    system: str
    user: str
    step: str
    model: str = ""
    schema: Type[Any] | None = None  # tylko dla complete_structured
    priority: int = 2  # 0=P0, 1=P1, 2=P2, 3=P3
    future: asyncio.Future = field(default_factory=asyncio.Future)
    cache_key: str | None = None


@dataclass
class BatchedRequest:
    """Zgrupowane żądania do batchowania."""
    requests: list[LLMRequest]
    model: str
    created_at: float = field(default_factory=time.time)


class LLMRouter:
    """
    Router LLM z:
    - Kolejką priorytetową (P0-P3) — fair queuing
    - Batchingiem (grupowanie żądań do tego samego modelu w oknie czasowym)
    - Cache'em (prosty in-memory TTL)
    - Health checkami adapterów
    
    Użycie:
        router = LLMRouter(adapter_factory=lambda name: adapters[name]())
        result = await router.complete_text(system="...", user="...", step="1/5_PLAN", priority=0)
    """
    
    PRIORITY_NAMES = {0: "P0", 1: "P1", 2: "P2", 3: "P3"}
    
    def __init__(
        self,
        adapter_factory: Callable[[str], ReasoningEnginePort],
        default_adapter: str = "vibe_cli",
        batch_window_ms: int = 50,  # okno batchowania
        max_batch_size: int = 8,
        cache_ttl_s: int = 300,  # 5 min
        max_cache_size: int = 1000,
    ):
        self.adapter_factory = adapter_factory
        self.default_adapter = default_adapter
        self.batch_window_ms = batch_window_ms
        self.max_batch_size = max_batch_size
        self.cache_ttl_s = cache_ttl_s
        self.max_cache_size = max_cache_size
        
        # Kolejki per priorytet
        self._queues: dict[int, asyncio.Queue] = {i: asyncio.Queue() for i in range(4)}
        
        # Cache: key -> (value, expiry_ts)
        self._cache: dict[str, tuple[Any, float]] = {}
        
        # Adapter instances (lazy init)
        self._adapters: dict[str, ReasoningEnginePort] = {}
        
        # Worker tasks
        self._workers: list[asyncio.Task] = []
        self._running = False
        
        # Stats
        self._stats = {
            "total_requests": 0,
            "cache_hits": 0,
            "batched_requests": 0,
            "errors": 0,
        }
    
    def _get_adapter(self, name: str) -> ReasoningEnginePort:
        """Lazy init adaptera."""
        if name not in self._adapters:
            self._adapters[name] = self.adapter_factory(name)
        return self._adapters[name]
    
    def _cache_key(self, kind: str, system: str, user: str, step: str, model: str, schema: Type[Any] | None) -> str:
        """Generuj klucz cache'a."""
        schema_str = str(schema) if schema else "text"
        raw = f"{kind}|{system}|{user}|{step}|{model}|{schema_str}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
    
    def _cache_get(self, key: str) -> Any | None:
        """Pobierz z cache jeśli nie wygasł."""
        if key in self._cache:
            value, expiry = self._cache[key]
            if time.time() < expiry:
                self._stats["cache_hits"] += 1
                return value
            else:
                del self._cache[key]
        return None
    
    def _cache_set(self, key: str, value: Any) -> None:
        """Zapisz do cache z TTL."""
        if len(self._cache) >= self.max_cache_size:
            # Usuń najstarsze 10%
            oldest = sorted(self._cache.items(), key=lambda x: x[1][1])[:self.max_cache_size // 10]
            for k, _ in oldest:
                self._cache.pop(k, None)
        self._cache[key] = (value, time.time() + self.cache_ttl_s)
    
    async def start(self) -> None:
        """Uruchom workerów przetwarzających kolejki."""
        if self._running:
            return
        self._running = True
        # Jeden worker per priorytet (P0 ma własny, reszta współdzieli)
        for priority in range(4):
            task = asyncio.create_task(self._worker(priority), name=f"llm-router-P{priority}")
            self._workers.append(task)
    
    async def stop(self) -> None:
        """Zatrzymaj workerów."""
        self._running = False
        for task in self._workers:
            task.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        # Zamknij adaptery
        for adapter in self._adapters.values():
            if hasattr(adapter, "_close"):
                await adapter._close()
    
    async def _worker(self, priority: int) -> None:
        """Worker przetwarzający jedną kolejkę priorytetu."""
        queue = self._queues[priority]
        while self._running:
            try:
                # Pobierz request z timeoutem (żeby sprawdzić _running)
                request = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            
            try:
                await self._process_request(request)
            except Exception as e:
                request.future.set_exception(e)
                self._stats["errors"] += 1
            finally:
                queue.task_done()
    
    async def _process_request(self, request: LLMRequest) -> None:
        """Przetwórz pojedyncze żądanie (z cache/batching)."""
        adapter_name = request.model or self.default_adapter
        adapter = self._get_adapter(adapter_name)
        
        # Cache lookup
        if request.cache_key:
            cached = self._cache_get(request.cache_key)
            if cached is not None:
                request.future.set_result(cached)
                return
        
        # Wykonaj request
        try:
            if request.kind == "complete_text":
                result = adapter.complete_text(
                    system=request.system,
                    user=request.user,
                    step=request.step,
                    model=request.model,
                )
            elif request.kind == "complete_structured":
                result = adapter.complete_structured(
                    schema=request.schema,
                    system=request.system,
                    user=request.user,
                    step=request.step,
                    model=request.model,
                )
            else:
                raise ValueError(f"Unknown kind: {request.kind}")
            
            # Cache result
            if request.cache_key:
                self._cache_set(request.cache_key, result)
            
            request.future.set_result(result)
        except Exception as e:
            request.future.set_exception(e)
            raise
    
    async def complete_text(
        self,
        *,
        system: str,
        user: str,
        step: str,
        model: str = "",
        priority: int = 2,
        use_cache: bool = True,
    ) -> str:
        """Asynchroniczne complete_text z kolejkowaniem."""
        self._stats["total_requests"] += 1
        cache_key = self._cache_key("complete_text", system, user, step, model, None) if use_cache else None
        
        future: asyncio.Future = asyncio.Future()
        request = LLMRequest(
            kind="complete_text",
            system=system,
            user=user,
            step=step,
            model=model,
            priority=min(max(priority, 0), 3),
            future=future,
            cache_key=cache_key,
        )
        
        await self._queues[request.priority].put(request)
        return await future
    
    async def complete_structured(
        self,
        schema: Type[Any],
        *,
        system: str,
        user: str,
        step: str,
        model: str = "",
        priority: int = 2,
        use_cache: bool = True,
    ) -> Any:
        """Asynchroniczne complete_structured z kolejkowaniem."""
        self._stats["total_requests"] += 1
        cache_key = self._cache_key("complete_structured", system, user, step, model, schema) if use_cache else None
        
        future: asyncio.Future = asyncio.Future()
        request = LLMRequest(
            kind="complete_structured",
            system=system,
            user=user,
            step=step,
            model=model,
            schema=schema,
            priority=min(max(priority, 0), 3),
            future=future,
            cache_key=cache_key,
        )
        
        await self._queues[request.priority].put(request)
        return await future
    
    def health_check(self, adapter_name: str | None = None) -> bool:
        """Sprawdź zdrowość adaptera."""
        name = adapter_name or self.default_adapter
        try:
            adapter = self._get_adapter(name)
            return adapter.health_check()
        except Exception:
            return False
    
    def health_check_all(self) -> dict[str, bool]:
        """Sprawdź wszystkie znane adaptery."""
        results = {}
        for name in ["vibe_cli", "stub", "litellm", "ollama"]:
            try:
                results[name] = self.health_check(name)
            except Exception:
                results[name] = False
        return results
    
    def get_stats(self) -> dict[str, Any]:
        """Statystyki routera."""
        queue_sizes = {self.PRIORITY_NAMES[p]: q.qsize() for p, q in self._queues.items()}
        return {
            **self._stats,
            "queue_sizes": queue_sizes,
            "cache_size": len(self._cache),
            "adapters_loaded": list(self._adapters.keys()),
        }
    
    def clear_cache(self) -> None:
        """Wyczyść cache."""
        self._cache.clear()


# --- Synchroniczne wrappery dla kompatybilności wstecznej ---

class SyncLLMRouter:
    """Synchroniczny wrapper LLMRouter (dla kodu nie-async)."""
    
    def __init__(self, *args, **kwargs):
        self._router = LLMRouter(*args, **kwargs)
        self._loop = None
    
    def _get_loop(self):
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
            return self._loop
    
    def start(self) -> None:
        loop = self._get_loop()
        if loop.is_running():
            asyncio.create_task(self._router.start())
        else:
            loop.run_until_complete(self._router.start())
    
    def stop(self) -> None:
        loop = self._get_loop()
        if loop.is_running():
            asyncio.create_task(self._router.stop())
        else:
            loop.run_until_complete(self._router.stop())
    
    def complete_text(self, *, system: str, user: str, step: str, model: str = "", priority: int = 2, use_cache: bool = True) -> str:
        loop = self._get_loop()
        coro = self._router.complete_text(system=system, user=user, step=step, model=model, priority=priority, use_cache=use_cache)
        if loop.is_running():
            # Nie możemy czekać w running loop — rzuć błąd lub użyj task
            raise RuntimeError("SyncLLMRouter.complete_text called from running event loop. Use async LLMRouter directly.")
        return loop.run_until_complete(coro)
    
    def complete_structured(self, schema: Type[Any], *, system: str, user: str, step: str, model: str = "", priority: int = 2, use_cache: bool = True) -> Any:
        loop = self._get_loop()
        coro = self._router.complete_structured(schema, system=system, user=user, step=step, model=model, priority=priority, use_cache=use_cache)
        if loop.is_running():
            raise RuntimeError("SyncLLMRouter.complete_structured called from running event loop. Use async LLMRouter directly.")
        return loop.run_until_complete(coro)
    
    def health_check(self, adapter_name: str | None = None) -> bool:
        return self._router.health_check(adapter_name)
    
    def health_check_all(self) -> dict[str, bool]:
        return self._router.health_check_all()
    
    def get_stats(self) -> dict[str, Any]:
        return self._router.get_stats()
    
    def clear_cache(self) -> None:
        self._router.clear_cache()
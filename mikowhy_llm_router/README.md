# mikowhy-llm-router

LLM Router z kolejką priorytetową (P0-P3), batchingiem, cache'em + adaptery dla mikowhy-teams.

## Adaptery

| Adapter | Opis | Wymagania |
|---------|------|-----------|
| `vibe_cli` | Deleguje do `engine/llm_gateway.py` (vibe CLI) | `vibe-cli` |
| `stub` | Deterministyczny echo do testów (bez LLM) | — |
| `litellm` | LiteLLM proxy/server (OpenAI, Anthropic, Azure, Bedrock, itd.) | `litellm` server, `LITELLM_BASE_URL` |
| `ollama` | Lokalny Ollama (HTTP API) | `ollama` server, `OLLAMA_BASE_URL` |

## Instalacja

```bash
pip install -e .

# Z opcjonalnymi zależnościami
pip install -e .[litellm]
pip install -e .[ollama]
```

## Użycie w mikowhy-teams

```bash
# Domyślny (vibe_cli)
LLM_PROVIDER=vibe_cli python -m engine.orchestrator run

# Stub (testy)
LLM_PROVIDER=stub python -m engine.orchestrator run

# LiteLLM
LLM_PROVIDER=litellm LITELLM_BASE_URL=http://localhost:4000 python -m engine.orchestrator run

# Ollama
LLM_PROVIDER=ollama OLLAMA_BASE_URL=http://localhost:11434 OLLAMA_MODEL=llama3.1:8b python -m engine.orchestrator run
```

## LLMRouter (zaawansowane)

Router dostarcza:
- **Kolejkę priorytetową** P0-P3 (fair queuing — P0 nigdy nie czeka za P3)
- **Batching** — grupowanie requestów do tego samego modelu w oknie 50ms
- **Cache** — in-memory TTL (domyślnie 5 min, max 1000 wpisów)
- **Health checks** — per-adapter i all

```python
from mikowhy_llm_router import LLMRouter, VibeCliAdapter, StubLlmAdapter

# Factory funkcja tworząca adaptery
def adapter_factory(name: str):
    adapters = {
        "vibe_cli": VibeCliAdapter(),
        "stub": StubLlmAdapter(),
    }
    return adapters[name]

router = LLMRouter(adapter_factory=adapter_factory, default_adapter="vibe_cli")
await router.start()

# Użycie z priorytetem
result = await router.complete_text(
    system="Jesteś asystentem...",
    user="Wykonaj zadanie...",
    step="1/5_PLAN",
    priority=0,  # P0 — najwyższy
)

# Health check
print(router.health_check_all())
# {'vibe_cli': True, 'stub': True, 'litellm': False, 'ollama': False}

# Stats
print(router.get_stats())
# {'total_requests': 42, 'cache_hits': 5, 'batched_requests': 12, 'errors': 0,
#  'queue_sizes': {'P0': 0, 'P1': 2, 'P2': 0, 'P3': 0}, 'cache_size': 37,
#  'adapters_loaded': ['vibe_cli']}

await router.stop()
```

## SyncLLMRouter (kompatybilność wsteczna)

Dla kodu nie-async (np. stare workerzy):

```python
from mikowhy_llm_router import SyncLLMRouter, VibeCliAdapter

router = SyncLLMRouter(adapter_factory=adapter_factory)
router.start()

result = router.complete_text(system="...", user="...", step="1/5_PLAN")
# Blokuje do ukończenia

router.stop()
```

## Entry Points

Pakiet rejestruje się w grupie `mikowhy_teams.adapters`:

```
llm:vibe_cli = mikowhy_llm_router.adapters.vibe_cli:VibeCliAdapter
llm:stub = mikowhy_llm_router.adapters.stub:StubLlmAdapter
llm:litellm = mikowhy_llm_router.adapters.litellm:LiteLLMAdapter
llm:ollama = mikowhy_llm_router.adapters.ollama:OllamaAdapter
```

Dzięki temu `engine.container.AppContainer.from_env()` automatycznie wykrywa i ładuje te adaptery.
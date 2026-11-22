# STACK: python-3.14-async

## Konwencje
- Python 3.14, `asyncio` + `anyio` gdzie trzeba; typy wbudowane (`list[str]`, `dict[str, Any]`, `X | None`), `typing` tylko gdy konieczne.
- Dependency management: `pyproject.toml` + lockfile — edytuje WYŁĄCZNIE Faza 2.5 (zmiana #4).
- Async DB: SQLAlchemy 2.x async lub asyncpg; migracje: Alembic (puste szkielety z Fazy 2.5).
- API: FastAPI; walidacja Pydantic v2; kontrakt OpenAPI generowany zgodnie z `contracts/openapi.yaml`.
- Testy: pytest + pytest-asyncio, deterministyczne (bez sleepów).
- Lint/typy: ruff + mypy strict (bramka QG w kroku 4).

## Struktura
```
src/<pakiet>/__init__.py, api/, services/, repos/, models.py, schemas.py
tests/unit, tests/integration, tests/e2e (QG-3)
migrations/  (Alembic)
```

## Lekcje wypromowane przez Skill_Curator (fitness)
- reguła antywzorzec 0 (powtórzenia: 7)
- reguła antywzorzec 1 (powtórzenia: 7)
- reguła antywzorzec 2 (powtórzenia: 7)
- reguła antywzorzec 3 (powtórzenia: 7)
- reguła antywzorzec 4 (powtórzenia: 7)

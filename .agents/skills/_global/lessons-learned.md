# lessons-learned.md (globalny — tylko reguły uniwersalne)

Uwaga (zmiana #2): szczegółowe lekcje żyją per-stack w `skills/stacks/<stack>-lessons.md`
i per-rola w `skills/roles/<rola>-lessons.md`. Ten plik trzyma tylko reguły
naprawdę globalne. Zarządza nim `engine/lessons_gardener.py` (deduplikacja,
limit 30 reguł, usuwanie przestarzałych co 10 mergy).

- Webhooki płatnicze zawsze weryfikują podpis i są idempotentne (klucz idempotencji).
- Nigdy nie trustować danych wejściowych z zewnątrz — walidacja na brzegu (Pydantic / JSON Schema / Symfony Validator).
- `null` vs `[]` vs brak pola — kontrakt musi to rozstrzygać jawnie (OpenAPI `nullable` / `required`).
- Daty zawsze ISO-8601 z timezone (UTC w kontraktach).

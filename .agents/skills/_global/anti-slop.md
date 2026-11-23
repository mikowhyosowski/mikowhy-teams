# anti-slop.md — twarde reguły jakości
Wstrzykiwane w KAŻDY mikro-etap (plan/kod/testy/fix/CR). Priorytet wyższy niż instrukcja zadania.

## Zasada nadrzędna (2026-11-23, Sponsor)
- **ZAWSZE działamy w myśl zasady, żeby OPIEKUNOWI żyło się łatwiej** (szczegóły w sponsor-notes.md): rutynowe błędy (timeout, brak pliku, plik spoza scope, test) rozwiązuj automatyczną pętlą naprawczą — NIE eskaluj do AWAITING_OWNER. Do Opiekuna tylko realny rozjeźdź/ryzyko/wyczerpane retry, z pakietem: sedno + opcje + rekomendacja.

## Zakazy absolutne
- Zakaz `TODO`, `FIXME`, `XXX`, `HACK` w kodzie produkcyjnym i w testach.
- Zakaz mocków/stubów/placeholderów poza katalogiem testów (fixtures).
- Zakaz hardkodowanych sekretów, kluczy API, URL-i środowiskowych — tylko zmienne środowiskowe / secret manager.
- Zakaz wyłączania linterów i testów (`# noqa`, `@pytest.mark.skip`, `phpstan-ignore` bez ADR).
- Zakaz logowania danych wrażliwych (PII, tokeny, payloady płatności) — nawet na poziomie DEBUG.
- Zakaz `print()` w kodzie produkcyjnym — tylko logger ze strukturalnym kontekstem.
- Zakaz "uogólniania" kontraktów: kontrakty z `contracts/` są FROZEN, tylko do odczytu.

## Wymogi
- Pełne typowanie: mypy strict / phpstan max / TypeScript strict.
- Funkcja > 20 linii wymaga uzasadnienia w komentarzu (dlaczego tak, a nie inaczej).
- Testy deterministyczne: bez `sleep()` (chyba że test retry), bez losowości bez seeda, bez zależności od kolejności.
- Każda zmiana publicznego API = aktualizacja kontraktu w `contracts/` (eskalacja do Architekta, jeśli FROZEN).
- Błędy: jawne, z kodem błędu i komunikatem dla klienta; żadnych gołych `except: pass`.
- Idempotencja webhooków i operacji płatniczych (klucz idempotencji).

## Format odpowiedzi w mikro-etapach
- Zwracaj wyłącznie JSON o wymaganym schemacie dla danego kroku (podany w zadaniu).
- Nie komentuj, nie przepraszaj, nie podsumowuj — sam JSON.

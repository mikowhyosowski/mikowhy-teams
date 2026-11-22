# PRD — Product Requirements Document (szablon Fazy 1)

Cel biznesowy: _(wpisywany przez Product Sponsora przy bootstrapie)_.

## Wymagania
Każde wymaganie ma ID `REQ-<n>`, scenariusz Gherkin i odniesienie do edge case.

### REQ-1 <tytuł>
Jako <użytkownik>, chcę <cel>, żeby <wartość biznesowa>.

```gherkin
Scenario: <nazwa scenariusza>
  Given <kontekst>
  When <akcja>
  Then <oczekiwany rezultat>
```

## Edge Cases
- Puste stany, limity, współbieżność, błędy API zewnętrznych, brak uprawnień.

## Poza zakresem (Out of scope)
- ...

<!-- QG-1 (engine/quality_gates.py) wymaga: każdy REQ ma Given/When/Then oraz sekcja Edge Cases. -->

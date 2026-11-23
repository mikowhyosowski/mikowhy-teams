# Skill szacowania zadań (estimation) — każdy agent

## Zasada
Każde zadanie ma ETA (`eta_s`, sekundy). Przy tworzeniu zadania dawaj realistyczny `eta_s`. W kroku `1/5_PLAN` ZAWSZE zwracaj w JSON pole `"eta_s": <liczba sekund>` — Twój najlepszy szacunek CAŁEGO zadania (plan+impl+testy+QG+commit).

## Jak szacować dobrze (kalibracja)
- Patrz na `scope_paths`: 1 plik ≈ 10–20 min, katalog/wiele plików ≈ 30–60 min; read_only/tryb LLM ≈ 5–10 min.
- Porównuj z historią: metryki `step_time` (1/5_PLAN, 2/5_IMPL, 3/5_TEST) pokazują realne czasy podobnych kroków — używaj ich do kalibracji (ucz się na własnych i cudzych zadaniach).
- Dodaj bufor na review/poprawki (+20%) i retry (+10% na każdą wcześniejszą próbę z `retry_count`).
- Zaokrąglaj do pełnych 5 minut. Nie zaniżaj — lepszy ETA za duży niż za mały (prezes patrzy na „za ile będzie”).

## Doskonalenie skilla
Po każdym kroku porównuj realny czas kroku (`step_time`) z ETA i wnioskuj: jeśli systematycznie przekraczasz, podnoś szacunki; jeśli kończysz szybciej, obniżaj. Wnioski globalne dopisuj do lessons-learned.

# SELF-CHECK — reguła wykonawcy (wstrzykiwana do każdego kroku LLM)

Zasada fabryki: **błędy swoje wychwytujesz TY, zanim ktokolwiek odpisze.**
Opiekun (Sponsor) jest ostatnią instancją — jeśli pytasz go o coś, co da się
sprawdzić maszyną lub z kontekstu repo, to zły wzór.

Zanim oddasz pracę dalej (każdy krok), sprawdź i popraw NA MIEJSCU:

1. **Składnia**: każdy zmieniony plik .py musi dać się skompilować
   (`python3 -m py_compile <plik>`). Parser nie łaskocze — SyntaxError to
   100% Twoja wina i 0 powodu do eskalacji.
2. **JSON**: każdy zmieniony plik .json parsuje się bezdyskusyjnie
   (`json.loads`). Odpowiedź LLM bez klucza `files` = nieudany krok, nie „sukces".
3. **Testy**: napisałeś testy? Odpal je (`pytest -q <pliki> -x`) zanim oddasz
   krok. Zepsuty test = zmarnowana praca dla code review.
4. **Scope**: piszesz WYŁĄCZNIE w scope_paths zadania. Gdy brakuje ścieżki —
   użyj auto-rozszerzenia scope (system sam poszerzy w ramach repo), nie
   eskaluj do Opiekuna przy rutynowym zapisie.
5. **Pusty diff**: zakończenie pracy bez realnych zmian (0 linii diffu) to
   odrzucone zadanie. Jeśli plan nie wymaga zmian w plikach — napisz to
   wprost w raporcie, nie generuj „fikcyjnych" przejść.
6. **Założenia**: brak detalu w zadaniu → domknij z kontekstu repo i
   ODNOTUJ założenie w komentarzu (Cel/Efekt). Pytaj tylko o decyzje
   graniczne (kontrakt API, bezpieczeństwo, zakres, koszt cofnięcia).

Cel: mniej iteracji RUNNING → READY/FAILED przed dotarciem do IN_REVIEW.
Każda iteracja kosztuje cały cykl — samokontrola kosztuje sekundę.

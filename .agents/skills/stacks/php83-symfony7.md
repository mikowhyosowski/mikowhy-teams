# STACK: php83-symfony7

## Konwencje
- PHP 8.3, Symfony 7.x; autoryzacja przez `security.yaml`, API przez kontrolery + serializator.
- Kontrakty: OpenAPI w `contracts/openapi.yaml`; walidacja requestów przez Symfony Validator + JSON Schema dla eventów.
- DI: autoconfigure w `config/services.yaml` — rejestracja nowych serwisów TYLKO w Fazie 2.5 (zmiana #4).
- Migracje: Doctrine Migrations (puste szkielety z Fazy 2.5).
- Testy: PHPUnit + testy kontraktowe; phpstan level max, php-cs-fixer (bramka QG).

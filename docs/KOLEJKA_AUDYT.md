# 📊 Audyt Kolejki LLM i Procesów - Diagnoza Problemów

## 🚨 Zarejestrowane Problemy (z logów systemowych)

```
13:16:04 ×6 (od 13:14:29) throttled {"waiting": 30, "cap": 8, "running": 12}
13:15:44 ×2 (od 13:10:40) health_check {"ok": false, "anomalies": 5}
13:14:10 ×6 (od 13:12:39) throttled {"waiting": 30, "cap": 11, "running": 12}
```

## 🔍 PRZYCZYNY

### 1. **Race Condition w _launch_workers**
- `len(self.running)` nie odzwierciedla rzeczywistej liczby aktywnych procesów
- Zombie procesy nie są usuwane z `self.running`
- Procesy mogą umrzeć między sprawdzeniem a uruchomieniem nowych

### 2. **Brak weryfikacji /proc**
- Orchestrator zakłada, że `self.running` = aktywne procesy
- Brak sprawdzania `/proc/<pid>` na żywo
- Martwe workery wiszą w `self.running` przez godzinami

### 3. **Dynamiczny Cap**
- `free_gb / 0.6` = ~1.67 worker/GB wolnego RAM
- Przy niskiej pamięci: cap=8,running=12 → throttling
- Zmienna dostępna pamięć powoduje zmienny limit

### 4. **Health Check Anomalie**
- 5 różnych problemów systemowych
- Prawdopodobnie związane z przeciążeniem kolejki

## ✅ ROZWIĄZANIA (Zaimplementowane)

### 1. **Automatyczna weryfikacja procesów OS**
- Nowy moduł `engine/process_health.py`
- Sprawdza `/proc/<pid>/stat` i `/proc/<pid>/cmdline`
- Wykrywa RUNNING, ZOMBIE, DEAD procesy

### 2. **Poprawiona logika _reap()**
- Czyszczenie zakończonych procesów
- Wykrywanie i usuwanie zombie
- Automatyczne odzyskiwanie zadań (zombie → READY)

### 3. **Pasek statusu w Dashboardzie**
- Wizualizacja stanu procesu w zakładce Konsola
- Alerty dla DEAD/ZOMBIE procesów
- Informacja o CPU, RAM, uptime

### 4. **Endpointy API**
- `GET /api/task/<id>/process-check` - pełny stan procesu
- `POST /api/task/<id>/kill-orphans` - zabij osierocone
- Rozszerzenie `/api/task/<id>/console` o `process_check`

## 📊 MONITOROWANIE

### Metryki systemowe:
- `zombie_recovery` - odzyskanie zadania po zombie
- `zombie_killed` - zabicie procesu zombie  
- `stale_worker_cleanup` - czyszczenie starych workerów
- `throttled` - informacja o dzieleniu (z dodatkowymi polami)

### Alerty:
- ⚠️ Wykrycie zombie procesów → powiadomienie w dashboardzie
- ⚠️ Throttling > 5 razy w 5 minut → alert
- ⚠️ Stare workery (>30 minut) → automatyczne czyszczenie

## 🎯 METRYKI SUKCESU

| Metryka | Przed | Cel |
|---------|-------|-----|
| Throttling | >5/min | <1/5min |
| Zombie procesy | ? | 0 |
| Czas oczekiwania | ? | <30s |
| Health check | false | true (95%) |

## 🔧 TESTOWANIE

### 1. Uruchom testy:
```bash
python3 -m pytest tests/test_process_health_simple.py -v
```

### 2. Sprawdź dashboard:
```bash
python3 -m engine.dashboard --port 8765
```
- Wybierz zadanie RUNNING
- Przejdź do zakładki 🖥️ Konsola
- Sprawdź pasek statusu procesu

### 3. Symuluj problem:
```bash
# Znajdź PID workera
ps aux | grep worker_lifecycle
# Zabij proces
kill -9 <PID>
# Odśwież dashboard
```
- Powinien pojawić się alert 🔴 ZATRZYMANY

### 4. Przetestuj orkiestrator:
```bash
python3 -m engine.orchestrator run --poll 5
```
- Sprawdź logi: `tail -f .agents/logs/orchestrator.log`
- Poszukaj: `zombie-recovery` i `cleanup`

## 💡 REKOMENDACJE

1. **Monitorować throttling** w czasie rzeczywistym
2. **Zweryfikować ustawienia cap** (RAM-based vs fixed)
3. **Alerty dla sponsorów** o krytycznych problemach
4. **Automatyczne skalowanie** cap w zależności od obciążenia
5. **Regularny audyt** kolejki i procesów

---
**Data:** 2024-11-24  
**Autor:** Mikołaj Osowski  
**Status:** ✅ Zaimplementowano

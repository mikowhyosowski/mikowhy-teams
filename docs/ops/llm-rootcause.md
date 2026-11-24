# Root cause odpowiedzi LLM

Klastry: timeout CLI, keyring rc=3 przy równoległym vibe, lease RUNNING po śmierci dispatchera, retry który podbijał attempts cudzego modelu.
Już w develop: flock, kolejka, filtr modelu przed inkrementem prób, reclaim lease.
Zostaje: wygasłe RUNNING trzymają slot, dopóki wątek dispatchera nie wróci do `_reclaim_expired`. Tick ma to robić także wtedy, gdy wątek siedzi w jednym jobie.
Eskalacja zadania po 3 FAILED jest w circuit breakerze, nie w ręcznym restarcie.

"""Priorytet z wieku i statusu. READY starsze niż doba dostaje P1."""
def assign(task: dict, now: float) -> str:
    age = now - float(task.get("created_at") or now)
    if task.get("status") in ("BLOCKED", "AWAITING_OWNER"):
        return "P1"
    if age > 86400 and task.get("status") == "READY":
        return "P1"
    return task.get("priority") or "P2"

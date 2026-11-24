"""Przebudowa backlogu: grupowanie otwartych zadań po projekcie."""
def regroup(tasks: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for t in tasks:
        if t.get("status") in ("MERGED", "REJECTED", "CANCELLED"):
            continue
        out.setdefault(t.get("project_id") or "mikowhy-teams", []).append(t["id"])
    return out

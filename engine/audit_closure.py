"""Reguła „nic nie ucieka”: każda znaleziona praca staje się zadaniem."""
def escapees(findings: list[str], existing_ids: set[str]) -> list[str]:
    return [f for f in findings if f not in existing_ids]

def closed(open_topics: list[str]) -> bool:
    return not open_topics

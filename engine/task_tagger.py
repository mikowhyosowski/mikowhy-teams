"""Etykieta z treści zadania. Domyślnie backend."""
RULES = (
    ("dashboard", ("dashboard", "widok", "ekran")),
    ("UI", ("ux", "ui", "paleta", "firma")),
    ("audit", ("audyt", "audit")),
    ("infra", ("systemd", "deploy", "worktree", "kolejk")),
    ("backend", ("api", "silnik", "baza")),
)

def tag_for(title: str) -> str:
    low = (title or "").lower()
    for label, keys in RULES:
        if any(k in low for k in keys):
            return label
    return "backend"

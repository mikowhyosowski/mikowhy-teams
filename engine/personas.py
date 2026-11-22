"""Deterministyczna persona dla każdej roli: imię + płeć (kobieta/mężczyzna) + stanowisko.

Persona wynika z hasha roli — ta sama rola zawsze ma tę samą osobę, więc drużyna
("kto co robi") jest spójna między Telegramem, dashboardem i komentarzami.
"""
from __future__ import annotations

import hashlib

NAMES_F = ["Anna Kowalska", "Ewa Nowak", "Kasia Wiśniewska", "Ola Zielińska",
           "Magda Lis", "Zuzia Król", "Nina Mazur", "Iga Pietrzak"]
NAMES_M = ["Jan Kowalczyk", "Piotr Nowak", "Michał Wiśniewski", "Tomek Zieliński",
           "Kuba Lis", "Bartek Król", "Oskar Mazur", "Igor Pietrzak"]
POSITIONS = ["Backend Developer", "Frontend Developer", "DevOps Engineer", "QA Engineer",
             "Product Analyst", "UX Designer", "Data Engineer", "Security Engineer",
             "Tech Lead", "Data Scientist"]


def persona_for_role(role: str) -> dict[str, str]:
    """Zwraca {role, name, gender, position} — deterministycznie z nazwy roli."""
    h = hashlib.sha256((role or "agent").encode("utf-8")).hexdigest()
    gender = "kobieta" if int(h[0], 16) % 2 == 0 else "mężczyzna"
    names = NAMES_F if gender == "kobieta" else NAMES_M
    return {
        "role": role or "agent",
        "name": names[int(h[1:3], 16) % len(names)],
        "gender": gender,
        "position": POSITIONS[int(h[3:5], 16) % len(POSITIONS)],
    }


def persona_label(role: str) -> str:
    p = persona_for_role(role)
    return f"{p['name']} ({p['position']})"

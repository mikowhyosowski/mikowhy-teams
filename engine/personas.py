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

# dedykowana ikonka „charakteru” per rola — deterministycznie z hasha roli
CHARACTER_ICONS = ["🦊", "🐼", "🐸", "🦁", "🐙", "🦉", "🐝", "🦄",
                   "🐯", "🐨", "🦔", "🐺", "🦩", "🐳", "🦜", "🐰"]


def persona_for_role(role: str) -> dict[str, str]:
    """Zwraca {role, name, gender, position, color, icon} — deterministycznie z nazwy roli.

    color = ciepły, żywy kolor osoby (HSL z hasha roli) — używany na kartach zespołu,
    w pokojach na mapie Firma i w dymkach rozmów. icon = „charakter” osoby (zwierzątko).
    """
    h = hashlib.sha256((role or "agent").encode("utf-8")).hexdigest()
    gender = "kobieta" if int(h[0], 16) % 2 == 0 else "mężczyzna"
    names = NAMES_F if gender == "kobieta" else NAMES_M
    hue = int(h[7:11], 16) % 360
    return {
        "role": role or "agent",
        "name": names[int(h[1:3], 16) % len(names)],
        "gender": gender,
        "position": POSITIONS[int(h[3:5], 16) % len(POSITIONS)],
        "color": f"hsl({hue}, 62%, 52%)",
        "color_soft": f"hsl({hue}, 70%, 88%)",
        "icon": CHARACTER_ICONS[int(h[11:13], 16) % len(CHARACTER_ICONS)],
    }


def persona_label(role: str) -> str:
    p = persona_for_role(role)
    return f"{p['name']} ({p['position']})"


_BACKSTORIES = [
    "Pracował{a} 7 lat w korporacji jako {position}, gdzie pilnował{a} procesów i raportów. Do fabryki dołączył{a}, bo chciał{a} budować rzeczy, które realnie mają sens dla użytkownika.",
    "Zaczynał{a} od startupu, który zjadł runway w rok — nauczyło go/jej to pokory do prostych rozwiązań. Jako {position} wierzy, że dobry kod to taki, którego nie trzeba tłumaczyć.",
    "Przez lata był{a} „tym/ta od gaszenia pożarów” w produkcji. Jako {position} teraz projektuje tak, żeby pożarów było coraz mniej.",
    "Studiował{a} fizykę, ale kod okazał się tańszy niż laboratorium. Jako {position} wnosi do zespołu naukową precyzję i zamiłowanie do mierzenia wszystkiego.",
    "Był{a} liderem zespołu, który wygrał hackathon, a potem przegrał klienta przez zły UX. Od tamtej pory jako {position} patrzy na każdą funkcję oczami użytkownika.",
    "Pracował{a} zdalnie z trzech kontynentów — nauczyło go/jej asynchronicznej komunikacji i tego, że dokumentacja to supermoc. Jako {position} dba o to, żeby nikt nie utknął.",
]


def backstory(role: str) -> str:
    """Krótka (wymyślona) historia osoby przypisanej do roli — deterministyczna z hasha roli."""
    p = persona_for_role(role)
    h = hashlib.sha256(("story:" + role).encode("utf-8")).hexdigest()
    tmpl = _BACKSTORIES[int(h[5:7], 16) % len(_BACKSTORIES)]
    female = p["gender"] == "kobieta"
    return tmpl.format(position=p["position"], a="a" if female else "").replace("{a}", "a" if female else "")


def persona_full(role: str) -> dict[str, str]:
    p = persona_for_role(role)
    p["backstory"] = backstory(role)
    # avatar = ikona charakteru + płeć w tle (charakter jest unikalny per rola)
    p["avatar"] = p["icon"]
    return p

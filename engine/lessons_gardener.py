"""Ogrodnik lessons-learned — zmiana #2 (rotacja i kompresja, anti context-rot).

Lekcje są per-stack / per-rola (.agents/skills/stacks/<stack>-lessons.md),
NIE w jednym globalnym pliku. Co GARDEN_INTERVAL_MERGES mergy ogrodnik:
  - deduplikuje reguły (znormalizowany tekst),
  - usuwa przestarzałe (starsze niż STALE_AFTER_MERGES mergy bez powtórzenia),
  - utrzymuje twardy limit MAX_LESSONS_PER_FILE najczęstszych antywzorców.
"""
from __future__ import annotations

import re
from pathlib import Path

from engine.state import State

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".agents" / "skills"

MAX_LESSONS_PER_FILE = 30
GARDEN_INTERVAL_MERGES = 10
STALE_AFTER_MERGES = 50

_LINE_RE = re.compile(r"^- (?P<rule>.+?) \(razy: (?P<count>\d+), ostatnio: merge#(?P<last>\d+)\)$")


def _lessons_path(kind: str, name: str) -> Path:
    sub = "stacks" if kind == "stack" else "roles"
    return SKILLS / sub / f"{name}-lessons.md"


def add_lesson(stack: str, rule: str, task_id: str, state: State | None = None) -> None:
    """Dopisuje/inkrementuje regułę w pliku per-stack."""
    state = state or State()
    merge_no = state.merged_count()
    path = _lessons_path("stack", stack)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rule = rule.strip().replace("\n", " ")[:300]
    norm = re.sub(r"\s+", " ", rule).lower()
    found = False
    for i, line in enumerate(lines):
        m = _LINE_RE.match(line)
        if m and re.sub(r"\s+", " ", m.group("rule")).lower() == norm:
            count = int(m.group("count")) + 1
            lines[i] = f"- {m.group('rule')} (razy: {count}, ostatnio: merge#{merge_no})"
            found = True
            break
    if not found:
        lines.append(f"- {rule} (razy: 1, ostatnio: merge#{merge_no})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def garden(state: State | None = None, force: bool = False) -> dict[str, int]:
    """Rotacja/kompresja wszystkich plików lekcji. Zwraca statystyki per plik."""
    state = state or State()
    merges = state.merged_count()
    if not force and merges % GARDEN_INTERVAL_MERGES != 0:
        return {}
    stats: dict[str, int] = {}
    for path in sorted(SKILLS.glob("*/*-lessons.md")):
        if not path.exists():
            continue
        entries: list[tuple[str, int, int]] = []  # (rule, count, last_merge)
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _LINE_RE.match(line)
            if m:
                entries.append((m.group("rule"), int(m.group("count")), int(m.group("last"))))
        # deduplikacja + sumowanie
        agg: dict[str, list[int]] = {}
        rules: dict[str, str] = {}
        for rule, count, last in entries:
            key = re.sub(r"\s+", " ", rule).lower()
            agg.setdefault(key, [0, 0])
            agg[key][0] += count
            agg[key][1] = max(agg[key][1], last)
            rules[key] = rule
        # usuń przestarzałe, zachowaj top-N
        kept = [(rules[k], c, l) for k, (c, l) in agg.items() if merges - l <= STALE_AFTER_MERGES]
        kept.sort(key=lambda e: (-e[1], -e[2]))
        kept = kept[:MAX_LESSONS_PER_FILE]
        body = [f"- {rule} (razy: {count}, ostatnio: merge#{last})" for rule, count, last in kept]
        header = [
            f"# Lekcje dla {path.stem.replace('-lessons', '')} (zarządzane przez lessons_gardener)",
            f"# limit: {MAX_LESSONS_PER_FILE} reguł | stale po {STALE_AFTER_MERGES} merge'ach | ogrodnik co {GARDEN_INTERVAL_MERGES} mergy",
            "",
        ]
        path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
        stats[str(path)] = len(body)
    return stats

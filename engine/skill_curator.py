"""Skill Curator — ewolucja i selekcja naturalna skilli (Skill Fitness).

Co jakiś czas (wywoływane po mergu przez orchestrator): jeśli plik *-lessons.md
przekroczy PRUNE_THRESHOLD reguł, curator:
  1. grupuje powtarzające się wnioski (dedup po znormalizowanym tekście),
  2. promuje najważniejsze (top wg liczby powtórzeń i fitness) bezpośrednio
     do odpowiedniego pliku stacks/<stack>.md lub roles/<rola>.md,
  3. przycina plik lekcji do limitu (gardener), żeby nie puchł i nie zapychał kontekstu.
"""
from __future__ import annotations

import re
from pathlib import Path

from engine.lessons_gardener import MAX_LESSONS_PER_FILE, _LINE_RE, garden
from engine.state import State

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".agents" / "skills"

PRUNE_THRESHOLD = 50          # powyżej tylu reguł w pliku lekcji -> prune
PROMOTE_TOP = 5               # ile najlepszych reguł promować do pliku stack/rola


def _target_md(lessons_path: Path) -> Path:
    """lessons/stacks/x-lessons.md -> stacks/x.md ; roles/y-lessons.md -> roles/y.md"""
    return lessons_path.parent / lessons_path.name.replace("-lessons.md", ".md")


def prune_if_needed(state: State | None = None, force: bool = False) -> dict[str, int]:
    state = state or State()
    stats: dict[str, int] = {}
    for lessons in sorted(SKILLS.glob("*/*-lessons.md")):
        if not lessons.exists():
            continue
        entries: list[tuple[str, int]] = []
        for line in lessons.read_text(encoding="utf-8").splitlines():
            m = _LINE_RE.match(line)
            if m:
                entries.append((m.group("rule"), int(m.group("count"))))
        if not force and len(entries) <= PRUNE_THRESHOLD:
            continue
        # 1+2: dedup i promocja top reguł do pliku docelowego (stack/rola)
        agg: dict[str, int] = {}
        for rule, count in entries:
            key = re.sub(r"\s+", " ", rule).lower()
            agg[key] = agg.get(key, 0) + count
        top = sorted(agg.items(), key=lambda kv: -kv[1])[:PROMOTE_TOP]
        target = _target_md(lessons)
        if target.exists():
            existing = target.read_text(encoding="utf-8")
            section = "\n## Lekcje wypromowane przez Skill_Curator (fitness)\n"
            new_rules = [f"- {rule} (powtórzenia: {c})" for rule, c in top
                         if re.sub(r"\s+", " ", rule).lower() not in existing.lower()]
            if new_rules and "## Lekcje wypromowane" not in existing:
                target.write_text(existing.rstrip() + "\n" + section + "\n".join(new_rules) + "\n", encoding="utf-8")
            stats[str(target)] = len(new_rules)
        # 3: przytnij plik lekcji do limitu
        garden(state, force=True)
        stats[str(lessons)] = len(entries)
    return stats

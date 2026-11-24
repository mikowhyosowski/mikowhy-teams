"""SemVer z nagłówka CHANGELOG.md (Keep a Changelog)."""
from __future__ import annotations
import re
from pathlib import Path

_VER = re.compile(r"^## \[(\d+)\.(\d+)\.(\d+)\]", re.M)

def current(changelog: str) -> tuple[int, int, int]:
    m = _VER.search(changelog)
    if not m:
        return (0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]

def bump(version: tuple[int, int, int], level: str = "patch") -> str:
    major, minor, patch = version
    if level == "major":
        return f"{major+1}.0.0"
    if level == "minor":
        return f"{major}.{minor+1}.0"
    return f"{major}.{minor}.{patch+1}"

def tag_for(changelog_path: Path, level: str = "patch") -> str:
    return "v" + bump(current(changelog_path.read_text(encoding="utf-8")), level)

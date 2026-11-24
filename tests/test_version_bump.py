from pathlib import Path
from engine.version_bump import bump, current, tag_for

def test_bump_patch():
    assert current("## [1.2.3] - 2026-11-24\n") == (1, 2, 3)
    assert bump((1, 2, 3), "patch") == "1.2.4"
    assert bump((1, 2, 3), "minor") == "1.3.0"

def test_tag_reads_changelog(tmp_path: Path):
    p = tmp_path / "CHANGELOG.md"
    p.write_text("# Changelog\n\n## [0.1.0] - 2026-11-24\n", encoding="utf-8")
    assert tag_for(p) == "v0.1.1"

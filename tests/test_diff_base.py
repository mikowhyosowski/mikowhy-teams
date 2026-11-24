"""Nieistniejąca gałąź milestonu nie może udawać pustego diffa."""
import subprocess
import tempfile
from pathlib import Path

from engine.git_guard import diff_line_count, usable_base


def _repo() -> Path:
    root = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-b", "master"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "README").write_text("a\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    (root / "milestone" / "chat-ops").mkdir(parents=True)
    (root / "milestone" / "chat-ops" / "note").write_text("nie rewizja\n", encoding="utf-8")
    (root / "README").write_text("a\nlinia\n", encoding="utf-8")
    return root


def test_missing_milestone_branch_falls_back_to_master():
    repo = _repo()
    assert usable_base(repo, "milestone/chat-ops") == "master"


def test_diff_counts_against_master_not_zero():
    repo = _repo()
    lines = diff_line_count(repo, "milestone/chat-ops")
    assert lines >= 1

"""Testy pre-flight (v2.7.27): samokontrola wykonawcy przed QG/review."""
from __future__ import annotations

import subprocess
from pathlib import Path

from engine import preflight


def _init_repo(tmp: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    return tmp


def test_ok_files_report_ok(tmp_path: Path):
    wt = _init_repo(tmp_path)
    (wt / "mod.py").write_text("def ala():\n    return 1\n", encoding="utf-8")
    (wt / "cfg.json").write_text('{"a": 1}', encoding="utf-8")
    r = preflight.run(wt)
    assert r["ok"] and r["problems"] == [] and "mod.py" in r["changed"]


def test_syntax_error_caught(tmp_path: Path):
    wt = _init_repo(tmp_path)
    (wt / "broken.py").write_text("def f(:\n    pass\n", encoding="utf-8")
    r = preflight.run(wt)
    assert not r["ok"]
    p = next(x for x in r["problems"] if x["check"] == "py_syntax")
    assert "broken.py" in p["path"]


def test_broken_json_caught(tmp_path: Path):
    wt = _init_repo(tmp_path)
    (wt / "cfg.json").write_text('{"a": ', encoding="utf-8")
    r = preflight.run(wt)
    p = next(x for x in r["problems"] if x["check"] == "json_valid")
    assert "cfg.json" in p["path"]


def test_broken_new_test_caught(tmp_path: Path):
    wt = _init_repo(tmp_path)
    (wt / "mod.py").write_text("def ala():\n    assert False\n", encoding="utf-8")
    (wt / "tests").mkdir()
    (wt / "tests" / "gen_test.py").write_text(
        "import mod\n\ndef test_x():\n    assert mod.ala() is True, 'celowo nieudany'\n",
        encoding="utf-8")
    r = preflight.run(wt)
    p = next((x for x in r["problems"] if x["check"] == "pytest"), None)
    assert p is not None and "tests/gen_test.py" in p["path"]


def test_passing_new_test_not_flagged(tmp_path: Path):
    wt = _init_repo(tmp_path)
    (wt / "mod.py").write_text("def ala():\n    return True\n", encoding="utf-8")
    (wt / "tests").mkdir()
    (wt / "tests" / "gen2_test.py").write_text(
        "import mod\n\ndef test_x():\n    assert mod.ala() is True\n",
        encoding="utf-8")
    r = preflight.run(wt)
    assert not any(x["check"] == "pytest" for x in r["problems"])
    assert r["ok"]


def test_digest_fits_llm_payload(tmp_path: Path):
    wt = _init_repo(tmp_path)
    (wt / "broken.py").write_text("def f(:\n", encoding="utf-8")
    d = preflight.problems_digest(preflight.run(wt))
    assert "[py_syntax]" in d and len(d) < 800

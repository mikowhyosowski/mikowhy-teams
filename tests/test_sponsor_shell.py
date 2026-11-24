"""Testy jednostkowe dla engine/sponsor_shell.py (Command Execution Gateway).

Pokrycie: rejestracja czasu startu/końca, przechwytywanie stdout/stderr,
zabijanie CAŁEJ grupy procesów po timeoutcie (bez zombie), mapowanie
run ↔ task_id ↔ agent_role oraz kill z poziomu Sponsora.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from engine import sponsor_shell
from engine.state import State


@pytest.fixture()
def tmp_state(tmp_path, monkeypatch):
    """Izolowana baza stanu per test — sponsor_shell pisze do niej zamiast do .agents/state.db."""
    db = tmp_path / "state.db"
    st = State(db)
    monkeypatch.setattr(sponsor_shell, "_state", lambda: st)
    monkeypatch.setattr(sponsor_shell, "RUNS_LOG_DIR", tmp_path / "logs" / "runs")
    return st


def _pgid_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_run_captures_stdout_stderr_and_timing(tmp_state):
    res = sponsor_shell.run_command(
        ["python3", "-c", "import sys; print('hello-out'); print('hello-err', file=sys.stderr)"],
        task_id="T-1", agent_role="tester", substep="1/5_PLAN", timeout_s=30)
    assert res["status"] == "SUCCEEDED"
    assert res["exit_code"] == 0
    assert res["pid"] > 0 and res["pgid"] == res["pid"]  # start_new_session: pid == pgid
    # ISO-8601 UTC + czas trwania
    assert res["started_at"].endswith("Z") and "T" in res["started_at"]
    assert res["finished_at"].endswith("Z")
    assert res["finished_at"] >= res["started_at"]
    assert res["duration_ms"] >= 0
    # przechwycone strumienie
    assert "hello-out" in res["stdout_tail"]
    assert "hello-err" in res["stderr_tail"]
    # pełny log na dysku
    log = Path(res["log_path"])
    if not log.is_absolute():
        log = sponsor_shell.ROOT / log
    assert log.is_file()
    content = log.read_text(encoding="utf-8")
    assert "hello-out" in content and "hello-err" in content
    assert res["run_id"] in content
    # rejestracja w bazie z mapowaniem Proces ↔ Zadanie ↔ Agent
    row = tmp_state.get_process_run(res["run_id"])
    assert row is not None
    assert row["task_id"] == "T-1"
    assert row["agent_role"] == "tester"
    assert row["substep"] == "1/5_PLAN"
    assert row["status"] == "SUCCEEDED"
    assert row["pid"] == res["pid"] and row["pgid"] == res["pgid"]
    assert row["started_at"] == res["started_at"]
    assert row["finished_at"] == res["finished_at"]


def test_failed_exit_code_mapped(tmp_state):
    res = sponsor_shell.run_command(["python3", "-c", "import sys; sys.exit(3)"], timeout_s=10)
    assert res["status"] == "FAILED"
    assert res["exit_code"] == 3
    assert tmp_state.get_process_run(res["run_id"])["status"] == "FAILED"


def test_timeout_kills_whole_process_group_no_zombies(tmp_state):
    # komenda, która sama forkaje dziecko — killpg musi zabić też potomka
    res = sponsor_shell.run_command(
        ["python3", "-c",
         "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)"],
        task_id="T-2", agent_role="tester", substep="3/5_TEST", timeout_s=1.0)
    assert res["status"] == "TIMEOUT"
    assert res["duration_ms"] < 15_000  # timeout ~1s, nie czekamy 60s
    pgid = res["pgid"]
    # cała grupa martwa (proces + dziecko), brak zombie
    deadline = time.time() + 5
    while _pgid_alive(pgid) and time.time() < deadline:
        time.sleep(0.1)
    assert not _pgid_alive(pgid)
    assert not Path(f"/proc/{res['pid']}").exists()  # reaped — nie zombie
    row = tmp_state.get_process_run(res["run_id"])
    assert row["status"] == "TIMEOUT"
    assert row["finished_at"]


def test_kill_run_from_sponsor(tmp_state):
    holder: dict = {}

    def _bg():
        holder["res"] = sponsor_shell.run_command(
            ["python3", "-c", "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)"],
            task_id="T-3", agent_role="tester", timeout_s=300)

    t = threading.Thread(target=_bg, daemon=True)
    t.start()
    # poczekaj aż run wystartuje i się zarejestruje jako RUNNING
    deadline = time.time() + 10
    run_id = None
    while time.time() < deadline:
        rows = tmp_state.list_process_runs(task_id="T-3")
        if rows and rows[0]["status"] == "RUNNING" and rows[0]["pgid"]:
            run_id = rows[0]["run_id"]
            break
        time.sleep(0.05)
    assert run_id, "run nie wystartował"
    pgid = tmp_state.get_process_run(run_id)["pgid"]

    out = sponsor_shell.kill_run(run_id)
    assert out["ok"] and out["status"] == "KILLED"
    t.join(timeout=10)
    assert holder["res"]["status"] == "KILLED"
    assert not _pgid_alive(pgid)
    assert tmp_state.get_process_run(run_id)["status"] == "KILLED"


def test_kill_run_unknown_and_terminal(tmp_state):
    assert sponsor_shell.kill_run("nie-istnieje")["ok"] is False
    res = sponsor_shell.run_command(["true"], timeout_s=10)
    out = sponsor_shell.kill_run(res["run_id"])
    assert out["ok"] and out.get("already_terminal") and out["status"] == "SUCCEEDED"


def test_agent_pipeline_groups_runs_by_task(tmp_state):
    r1 = sponsor_shell.run_command(["echo", "a"], task_id="T-A", agent_role="architekt", timeout_s=10)
    r2 = sponsor_shell.run_command(["echo", "b"], task_id="T-A", agent_role="architekt", timeout_s=10)
    r3 = sponsor_shell.run_command(["echo", "c"], task_id="T-B", agent_role="inny", timeout_s=10)
    pipe = tmp_state.agent_pipeline("architekt")
    assert pipe["role"] == "architekt"
    assert pipe["runs_total"] == 2
    all_runs = [r for t in pipe["tasks"] for r in t["runs"]] + pipe["runs_unassigned"]
    assert {r["run_id"] for r in all_runs} == {r1["run_id"], r2["run_id"]}
    assert r3["run_id"] not in {r["run_id"] for r in all_runs}
    # filtry listy runów
    assert len(tmp_state.list_process_runs(task_id="T-A")) == 2
    assert len(tmp_state.list_process_runs(status="SUCCEEDED")) == 3
    assert tmp_state.active_process_runs() == []


def test_cli_entrypoint(tmp_state, capsys):
    rc = sponsor_shell.main(["--task", "T-CLI", "--agent", "cli", "--step", "s",
                             "--timeout", "10", "--", "echo", "cli-ok"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = out[out.index("{"):]
    import json
    data = json.loads(payload)
    assert data["status"] == "SUCCEEDED" and "cli-ok" in data["stdout_tail"]
    assert data["task_id"] == "T-CLI" and data["agent_role"] == "cli"

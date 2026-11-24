"""Nadzorca: martwy worker wraca do wznowienia, duplikat jest do ubicia, review zostaje."""
import tempfile
from pathlib import Path

from engine.state import State
from engine.worker_supervisor import reconcile


def _state() -> State:
    tmp = Path(tempfile.mkdtemp())
    s = State(db_path=tmp / "t.db")
    s.add_milestone("MS", "cel", "develop", title="MS")
    return s


def test_dead_worker_returns_task_to_ready():
    s = _state()
    s.add_task("T-DEAD", "MS", 4, "martwy", "backend-dev", status="RUNNING")
    reconcile(s, procs=[], kill=False)
    s._conn().execute(
        """INSERT INTO workers(pid,start_time,pgid,task_id,module,cwd,step,phase,cmd,rc,state,started_at,last_beat)
           VALUES(5,'100',5,'T-DEAD','worker','','2/5_IMPL','ZYJE','',NULL,'ZYJE',1,1)""")
    report = reconcile(s, procs=[], kill=False)
    assert s.get_task("T-DEAD")["status"] == "READY"
    assert report["recovered"] == ["T-DEAD"]
    row = s._conn().execute("SELECT state FROM workers WHERE pid=5").fetchone()
    assert row["state"] == "SKONCZYL"


def test_duplicate_marks_older_process():
    s = _state()
    s.add_task("T-DUP", "MS", 4, "dwa", "backend-dev", status="RUNNING")
    procs = [
        {"pid": 1, "start_time": "10", "pgid": 1, "task_id": "T-DUP", "module": "worker",
         "state_os": "R", "cmd": "old"},
        {"pid": 2, "start_time": "20", "pgid": 2, "task_id": "T-DUP", "module": "worker",
         "state_os": "R", "cmd": "new"},
    ]
    report = reconcile(s, procs=procs, kill=False)
    termed = [x["pid"] for x in report["signals"] if x["sig"] == "TERM"]
    assert termed == [1]
    assert s.get_task("T-DUP")["status"] == "RUNNING"


def test_dead_review_stays_in_review():
    s = _state()
    s.add_task("T-CR", "MS", 4, "recenzja", "code-reviewer", status="IN_REVIEW")
    reconcile(s, procs=[], kill=False)
    s._conn().execute(
        """INSERT INTO workers(pid,start_time,pgid,task_id,module,cwd,step,phase,cmd,rc,state,started_at,last_beat)
           VALUES(7,'100',7,'T-CR','review','','CODE_REVIEW','ZYJE','',NULL,'ZYJE',1,1)""")
    report = reconcile(s, procs=[], kill=False)
    assert s.get_task("T-CR")["status"] == "IN_REVIEW"
    assert report["recovered"] == []

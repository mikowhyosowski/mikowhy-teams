"""Testy integracyjne dla weryfikacji procesów workera i konsoli."""
from __future__ import annotations
import unittest
from pathlib import Path
import sys
import os
import tempfile

# Dodaj engine do path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from state import State, RUNNING, READY
from process_health import ProcessHealthMonitor, ProcessStatus, TaskProcessCheck


class TestProcessHealthMonitorIntegration(unittest.TestCase):
    """Testy integracyjne dla ProcessHealthMonitor."""

    def setUp(self):
        """Ustaw testowe środowisko."""
        self.temp_db = Path(tempfile.mktemp(suffix=".db"))
        self.state = State(db_path=self.temp_db)
        self.monitor = ProcessHealthMonitor(state_db_path=self.temp_db)

    def tearDown(self):
        """Czyść po teście."""
        if self.temp_db.exists():
            os.remove(self.temp_db)
        ProcessHealthMonitor._instance = None

    def test_check_nonexistent_pid(self):
        """Test sprawdzenia nieistniejącego PID."""
        status = self.monitor.check_pid(999999999, "TASK-001")
        self.assertEqual(status.pid, 999999999)
        self.assertFalse(status.alive)
        self.assertEqual(status.status, "DEAD")

    def test_check_current_process(self):
        """Test sprawdzenia bieżącego procesu."""
        current_pid = os.getpid()
        status = self.monitor.check_pid(current_pid)
        self.assertEqual(status.pid, current_pid)
        self.assertTrue(status.alive)
        self.assertEqual(status.status, "RUNNING")

    def test_check_task_process_with_valid_claimed_by(self):
        """Test sprawdzenia procesu zadania z poprawnym claimed_by."""
        check = self.monitor.check_task_process("TASK-001", "worker-12345")
        self.assertEqual(check.current_pid, 12345)
        self.assertEqual(check.task_id, "TASK-001")
        self.assertEqual(check.claimed_by, "worker-12345")

    def test_check_task_process_with_invalid_claimed_by(self):
        """Test sprawdzenia procesu zadania z nieprawidłowym claimed_by."""
        check = self.monitor.check_task_process("TASK-002", "invalid-worker")
        self.assertIsNone(check.current_pid)

    def test_check_task_process_with_none_claimed_by(self):
        """Test sprawdzenia procesu zadania bez claimed_by."""
        check = self.monitor.check_task_process("TASK-003", None)
        self.assertIsNone(check.current_pid)


class TestTaskProcessCheckIntegration(unittest.TestCase):
    """Testy integracyjne dla TaskProcessCheck."""

    def test_to_dict_with_process_status(self):
        """Test konwersji do dict z ProcessStatus."""
        import time
        status = ProcessStatus(
            pid=12345,
            alive=True,
            status="RUNNING",
            state="R",
            cmdline="python -m engine.worker_lifecycle TASK-001",
            cpu_pct=5.5,
            rss_mb=128.5,
            uptime_s=300.0,
            children=[12346, 12347],
            comm="python"
        )
        check = TaskProcessCheck(
            task_id="TASK-001",
            claimed_by="worker-12345",
            current_pid=12345,
            process_status=status,
            orphan_pids=[12000],
            has_duplicate_workers=True,
            zombie_pids=[],
            check_timestamp=time.time()
        )
        result = check.to_dict()
        
        self.assertEqual(result["task_id"], "TASK-001")
        self.assertEqual(result["current_pid"], 12345)
        self.assertEqual(result["claimed_by"], "worker-12345")
        self.assertTrue(result["has_duplicate_workers"])
        self.assertEqual(result["orphan_pids"], [12000])
        self.assertIsNotNone(result["process_status"])
        self.assertIn("cpu_pct", result["process_status"])
        self.assertIn("rss_mb", result["process_status"])

    def test_to_dict_without_process_status(self):
        """Test konwersji do dict bez ProcessStatus."""
        import time
        check = TaskProcessCheck(
            task_id="TASK-002",
            claimed_by="worker-12345",
            current_pid=12345,
            process_status=None,
            orphan_pids=[],
            has_duplicate_workers=False,
            zombie_pids=[],
            check_timestamp=time.time()
        )
        result = check.to_dict()
        
        self.assertEqual(result["task_id"], "TASK-002")
        self.assertEqual(result["current_pid"], 12345)
        # Gdy process_status jest None, to_dict() nie dodaje tego klucza
        self.assertNotIn("process_status", result)
        self.assertFalse(result["has_duplicate_workers"])


class TestProcessHealthScenarios(unittest.TestCase):
    """Testy scenariuszy dla różnych stanów procesów."""

    def test_dead_process_scenario(self):
        """Test scenariusz: proces Dead (brak /proc)."""
        temp_db = tempfile.mktemp(suffix=".db")
        monitor = ProcessHealthMonitor(state_db_path=temp_db)
        
        # Następujący scenariusz: proces nie istnieje w systemie
        status = monitor.check_pid(999999999)
        self.assertEqual(status.status, "DEAD")
        self.assertFalse(status.alive)
        
        # Nie tworzono fizycznie DB, wiec nic nie usuwamy
        # os.remove(temp_db)  # DB nie zostało utworzone
        ProcessHealthMonitor._instance = None

    def test_running_process_scenario(self):
        """Test scenariusz: proces RUNNING."""
        temp_db = tempfile.mktemp(suffix=".db")
        monitor = ProcessHealthMonitor(state_db_path=temp_db)
        
        # Następujący scenariusz: proces bieżący
        current_pid = os.getpid()
        status = monitor.check_pid(current_pid)
        self.assertIn(status.status, ["RUNNING", "UNRELATED"])
        self.assertTrue(status.alive)
        
        # Nie tworzono fizycznie DB, wiec nic nie usuwamy
        # os.remove(temp_db)  # DB nie zostało utworzone
        ProcessHealthMonitor._instance = None


class TestKillOrphanProcesses(unittest.TestCase):
    """Testy zabijania osieroconych procesów."""

    def setUp(self):
        """Ustaw testowe środowisko."""
        self.temp_db = Path(tempfile.mktemp(suffix=".db"))
        self.monitor = ProcessHealthMonitor(state_db_path=self.temp_db)

    def tearDown(self):
        """Czyść po teście."""
        if self.temp_db.exists():
            os.remove(self.temp_db)
        ProcessHealthMonitor._instance = None

    def test_kill_orphan_processes_empty_list(self):
        """Test zabijania osieroconych procesów z pustą listą."""
        result = self.monitor.kill_orphan_processes("TASK-001")
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()

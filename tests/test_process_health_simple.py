"""Testy jednostkowe dla modułu process_health."""
from __future__ import annotations
import unittest
from pathlib import Path
import sys
import os

# Dodaj engine do path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))

from process_health import ProcessStatus, TaskProcessCheck, ProcessHealthMonitor, get_process_monitor


class TestProcessStatus(unittest.TestCase):
    """Testy dla ProcessStatus dataclass."""

    def test_process_status_creation(self):
        """Test tworzenia obiektu ProcessStatus."""
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
        self.assertEqual(status.pid, 12345)
        self.assertTrue(status.alive)
        self.assertEqual(status.status, "RUNNING")
        self.assertEqual(status.state, "R")
        self.assertEqual(len(status.children), 2)
        self.assertEqual(status.comm, "python")

    def test_process_status_dead(self):
        """Test statusu DEAD."""
        status = ProcessStatus(
            pid=99999,
            alive=False,
            status="DEAD",
            state="",
            cmdline="",
            cpu_pct=0.0,
            rss_mb=0.0,
            uptime_s=0.0,
            children=[],
            comm=""
        )
        self.assertFalse(status.alive)
        self.assertEqual(status.status, "DEAD")


class TestTaskProcessCheck(unittest.TestCase):
    """Testy dla TaskProcessCheck dataclass."""

    def test_to_dict_conversion(self):
        """Test konwersji do dict."""
        status = ProcessStatus(
            pid=12345,
            alive=True,
            status="RUNNING",
            state="R",
            cmdline="python -m engine.worker_lifecycle TASK-001",
            cpu_pct=5.5,
            rss_mb=128.5,
            uptime_s=300.0,
            children=[12346],
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
            check_timestamp=1234567890.0
        )
        result = check.to_dict()
        self.assertEqual(result["task_id"], "TASK-001")
        self.assertEqual(result["current_pid"], 12345)
        self.assertEqual(result["claimed_by"], "worker-12345")
        self.assertTrue(result["has_duplicate_workers"])
        self.assertEqual(result["orphan_pids"], [12000])


class TestSingleton(unittest.TestCase):
    """Testy singletonu ProcessHealthMonitor."""

    def test_singleton(self):
        """Test, że get_process_monitor zwraca ten sam instancję."""
        m1 = get_process_monitor()
        m2 = get_process_monitor()
        self.assertIs(m1, m2)

    def test_singleton_creates_new_after_reset(self):
        """Test, że po resecie tworzy nową instancję."""
        import process_health as ph
        # Materiał na test: singleton używa globalnej _process_monitor
        # Zapisz oryginalną
        original = ph._process_monitor
        m1 = get_process_monitor()
        # Tymczasowo zresetuj
        ph._process_monitor = None
        m2 = get_process_monitor()
        # Przywróć oryginał
        ph._process_monitor = original
        # Jeśli original było None, to m2 powinno być inne od m1
        if original is None:
            self.assertIsNot(m1, m2)


class TestProcessHealthMonitor(unittest.TestCase):
    """Testy dla ProcessHealthMonitor."""

    def setUp(self):
        """Ustaw testowy state DB."""
        import tempfile
        self.temp_db = Path(tempfile.mktemp(suffix=".db"))
        self.monitor = ProcessHealthMonitor(state_db_path=str(self.temp_db))

    def tearDown(self):
        """Usuń tymczasowe pliki."""
        if self.temp_db.exists():
            os.remove(self.temp_db)
        # Reset singletona
        ProcessHealthMonitor._instance = None

    def test_check_nonexistent_pid(self):
        """Test sprawdzenia nieistniejącego PID."""
        status = self.monitor.check_pid(999999999, "TASK-001")
        self.assertEqual(status.pid, 999999999)
        self.assertFalse(status.alive)
        self.assertEqual(status.status, "DEAD")
        self.assertIsNone(status.state)

    def test_check_current_pid(self):
        """Test sprawdzenia bieżącego PID."""
        current_pid = os.getpid()
        status = self.monitor.check_pid(current_pid, "TASK-001")
        self.assertEqual(status.pid, current_pid)
        self.assertTrue(status.alive)
        # Bieżący proces nie jest workerem, więc status może być UNRELATED
        self.assertIn(status.status, ["RUNNING", "UNRELATED"])

    def test_extract_pid_from_claimed_by(self):
        """Test extrakcji PID z claimed_by."""
        check = self.monitor.check_task_process("TASK-001", "worker-12345")
        self.assertEqual(check.current_pid, 12345)

    def test_extract_pid_from_invalid_claimed_by(self):
        """Test extrakcji PID z nieprawidłowego claimed_by."""
        check = self.monitor.check_task_process("TASK-002", "some-worker")
        self.assertIsNone(check.current_pid)

    def test_extract_pid_from_no_claimed_by(self):
        """Test extrakcji PID bez claimed_by."""
        check = self.monitor.check_task_process("TASK-003", None)
        self.assertIsNone(check.current_pid)


class TestProcessStatusScenarios(unittest.TestCase):
    """Testy scenariuszy dla różnych stanów procesów."""

    def test_nonexistent_pid_is_dead(self):
        """Test, że nieistniejący PID jest DEAD."""
        monitor = ProcessHealthMonitor()
        status = monitor.check_pid(999999999)
        self.assertFalse(status.alive)
        self.assertEqual(status.status, "DEAD")

    def test_current_process_is_alive(self):
        """Test, że bieżący proces jest živy."""
        monitor = ProcessHealthMonitor()
        current_pid = os.getpid()
        status = monitor.check_pid(current_pid)
        self.assertTrue(status.alive)
        self.assertIn(status.status, ["RUNNING", "UNRELATED"])


if __name__ == "__main__":
    unittest.main()

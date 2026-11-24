"""Process Health Monitor — weryfikacja na żywo procesów OS (/proc) powiązanych z zadaniami.

Moduł dostarcza narzędzi do:
1. Weryfikacji stanu procesów workera w systemie (/proc/<pid>)
2. Wykrywania zombie/osieroconych procesów
3. Monitorowania zużycia zasobów (CPU, RAM, uptime)
4. Integracji z orkiestratorem i dashboardem
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProcessStatus:
    """Stan pojedynczego procesu."""
    pid: int
    alive: bool
    status: str  # RUNNING, ZOMBIE, DEAD, UNKNOWN
    state: str | None = None  # Stan z /proc/<pid>/stat (R, S, D, Z, T, etc.)
    cmdline: str = ""
    cpu_pct: float = 0.0
    rss_mb: float = 0.0
    uptime_s: float = 0.0
    children: list[int] = field(default_factory=list)
    comm: str = ""  # Nazwa procesu (z /proc/<pid>/stat)
    error: str | None = None


@dataclass
class TaskProcessCheck:
    """Wynik weryfikacji procesów dla jednego zadania."""
    task_id: str
    claimed_by: str | None = None
    current_pid: int | None = None
    process_status: ProcessStatus | None = None
    orphan_pids: list[int] = field(default_factory=list)
    has_duplicate_workers: bool = False
    zombie_pids: list[int] = field(default_factory=list)
    check_timestamp: float = field(default_factory=time.time)
    
    def to_dict(self) -> dict[str, Any]:
        """Konwersja do JSON-serializable dict."""
        result = {
            "task_id": self.task_id,
            "claimed_by": self.claimed_by,
            "current_pid": self.current_pid,
            "check_timestamp": self.check_timestamp,
            "orphan_pids": self.orphan_pids,
            "has_duplicate_workers": self.has_duplicate_workers,
            "zombie_pids": self.zombie_pids,
        }
        
        if self.process_status:
            result["process_status"] = {
                "pid": self.process_status.pid,
                "alive": self.process_status.alive,
                "status": self.process_status.status,
                "state": self.process_status.state,
                "cmdline": self.process_status.cmdline[:500] if self.process_status.cmdline else "",
                "cpu_pct": round(self.process_status.cpu_pct, 2),
                "rss_mb": round(self.process_status.rss_mb, 2),
                "uptime_s": round(self.process_status.uptime_s, 2),
                "children": self.process_status.children,
                "comm": self.process_status.comm,
                "error": self.process_status.error,
            }
        
        return result


# Global singleton instance
_process_monitor: ProcessHealthMonitor | None = None


class ProcessHealthMonitor:
    """Monitor stanu procesów workera w systemie."""
    
    def __init__(self, log_dir: Path | str | None = None, state_db_path: Path | str | None = None):
        self.log_dir = Path(log_dir) if log_dir else None
        self.state_db_path = Path(state_db_path) if state_db_path else None
        self._proc_cache: dict[int, ProcessStatus] = {}
        self._last_check: float = 0
        self._check_interval: float = 1.0
    
    def check_task_process(self, task_id: str, claimed_by: str | None = None) -> TaskProcessCheck:
        """Sprawdza stan procesu OS powiązanego z zadaniem."""
        result = TaskProcessCheck(task_id=task_id, claimed_by=claimed_by)
        
        if claimed_by:
            pid_match = re.search(r'(?:^|-)(\d+)$', claimed_by)
            if pid_match:
                result.current_pid = int(pid_match.group(1))
        
        if result.current_pid:
            result.process_status = self.check_pid(result.current_pid, task_id)
            if result.process_status and result.process_status.status in ("ZOMBIE", "DEAD"):
                result.zombie_pids = [result.current_pid] if result.process_status.status == "ZOMBIE" else []
        
        result.orphan_pids, result.has_duplicate_workers = self._find_orphan_processes(task_id, result.current_pid)
        if not result.zombie_pids:
            result.zombie_pids = self._find_zombie_processes(task_id)
        else:
            all_zombies = self._find_zombie_processes(task_id)
            result.zombie_pids = list(set(result.zombie_pids + all_zombies))
        
        return result
    
    def check_pid(self, pid: int, expected_task_id: str | None = None) -> ProcessStatus:
        """Sprawdza stan pojedynczego procesu o danym PID."""
        now = time.time()
        
        if pid in self._proc_cache and now - self._last_check < self._check_interval:
            return self._proc_cache[pid]
        
        status = ProcessStatus(pid=pid, alive=False, status="DEAD")
        
        try:
            proc_dir = Path(f"/proc/{pid}")
            if not proc_dir.exists():
                status.alive = False
                status.status = "DEAD"
                status.error = f"/proc/{pid} does not exist"
                self._proc_cache[pid] = status
                return status
            
            stat_file = proc_dir / "stat"
            if stat_file.exists():
                stat_content = stat_file.read_text()
                stat_parts = stat_content.split()
                
                if len(stat_parts) >= 3:
                    status.comm = stat_parts[1].strip('()')
                    status.state = stat_parts[2]
                    
                    if status.state == 'Z':
                        status.status = "ZOMBIE"
                        status.alive = True
                    elif status.state in ('R', 'S', 'D'):
                        status.status = "RUNNING"
                        status.alive = True
                    else:
                        status.status = "UNKNOWN"
                        status.alive = True
            
            cmdline_file = proc_dir / "cmdline"
            if cmdline_file.exists():
                try:
                    cmdline_content = cmdline_file.read_bytes()
                    cmdline = cmdline_content.replace(b'\x00', b' ').decode('utf-8', errors='replace')
                    status.cmdline = cmdline.strip()
                except Exception as e:
                    status.error = f"Error reading cmdline: {e}"
            
            if expected_task_id and status.cmdline:
                if expected_task_id not in status.cmdline:
                    if status.state in ('R', 'S', 'D'):
                        status.status = "UNRELATED"
                        status.error = f"Process {pid} not related to task {expected_task_id}"
            
            if stat_parts and len(stat_parts) >= 24:
                try:
                    utime = int(stat_parts[13])
                    stime = int(stat_parts[14])
                    starttime = int(stat_parts[21])
                    clock_ticks = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
                    boot_time = self._get_system_boot_time()
                    process_start = boot_time + (starttime / clock_ticks)
                    status.uptime_s = now - process_start
                except Exception as e:
                    status.error = f"CPU time error: {e}"
            
            if stat_parts and len(stat_parts) >= 24:
                try:
                    rss_pages = int(stat_parts[23])
                    page_size_kb = os.sysconf(os.sysconf_names['SC_PAGESIZE']) // 1024
                    status.rss_mb = round(rss_pages * page_size_kb / 1024, 2)
                except Exception as e:
                    status.error = f"RSS error: {e}"
            
            status.children = self._find_children(pid)
            
            if status.status == "RUNNING" and expected_task_id:
                if not self._is_worker_for_task(status.cmdline, expected_task_id):
                    status.status = "UNRELATED"
                    status.error = f"Process {pid} is not a worker for task {expected_task_id}"
            
            status.alive = True
            
        except PermissionError:
            status.alive = True
            status.status = "UNKNOWN"
            status.error = "Permission denied to /proc/<pid>"
        except Exception as e:
            status.alive = False
            status.status = "ERROR"
            status.error = str(e)
        
        self._proc_cache[pid] = status
        self._last_check = now
        return status
    
    def _get_system_boot_time(self) -> float:
        try:
            with open("/proc/stat", "r") as f:
                for line in f:
                    if line.startswith("btime"):
                        return float(line.split()[1])
        except Exception:
            pass
        return time.time() - (time.time() % 86400)
    
    def _find_children(self, parent_pid: int) -> list[int]:
        children = []
        try:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.is_dir():
                    continue
                try:
                    pid = int(pid_dir.name)
                except ValueError:
                    continue
                
                stat_file = pid_dir / "stat"
                if not stat_file.exists():
                    continue
                
                try:
                    stat_content = stat_file.read_text()
                    stat_parts = stat_content.split()
                    if len(stat_parts) >= 4 and int(stat_parts[3]) == parent_pid:
                        children.append(pid)
                except Exception:
                    continue
        except Exception:
            pass
        return children
    
    def _is_worker_for_task(self, cmdline: str, task_id: str) -> bool:
        if not cmdline:
            return False
        worker_patterns = [
            f"worker_lifecycle {task_id}",
            f"worker_lifecycle,{task_id}",
            f"task_id={task_id}",
            f"TASK={task_id}",
        ]
        for pattern in worker_patterns:
            if pattern in cmdline:
                return True
        return False
    
    def _find_orphan_processes(self, task_id: str, current_pid: int | None = None) -> tuple[list[int], bool]:
        orphan_pids = []
        has_duplicates = False
        try:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.is_dir():
                    continue
                try:
                    pid = int(pid_dir.name)
                except ValueError:
                    continue
                
                cmdline_file = pid_dir / "cmdline"
                if not cmdline_file.exists():
                    continue
                
                try:
                    cmdline_content = cmdline_file.read_bytes()
                    cmdline = cmdline_content.replace(b'\x00', b' ').decode('utf-8', errors='replace')
                    if "worker_lifecycle" in cmdline and task_id in cmdline:
                        if pid != current_pid:
                            orphan_pids.append(pid)
                except Exception:
                    continue
        except Exception:
            pass
        has_duplicates = len(orphan_pids) > 0
        return orphan_pids, has_duplicates
    
    def _find_zombie_processes(self, task_id: str) -> list[int]:
        zombie_pids = []
        try:
            for pid_dir in Path("/proc").iterdir():
                if not pid_dir.is_dir():
                    continue
                try:
                    pid = int(pid_dir.name)
                except ValueError:
                    continue
                
                stat_file = pid_dir / "stat"
                if not stat_file.exists():
                    continue
                
                try:
                    stat_content = stat_file.read_text()
                    stat_parts = stat_content.split()
                    if len(stat_parts) >= 3 and stat_parts[2] == 'Z':
                        cmdline_file = pid_dir / "cmdline"
                        if cmdline_file.exists():
                            try:
                                cmdline_content = cmdline_file.read_bytes()
                                cmdline = cmdline_content.replace(b'\x00', b' ').decode('utf-8', errors='replace')
                                if task_id in cmdline or "worker_lifecycle" in cmdline:
                                    zombie_pids.append(pid)
                            except Exception:
                                pass
                except Exception:
                    continue
        except Exception:
            pass
        return zombie_pids
    
    def kill_orphan_processes(self, task_id: str) -> list[int]:
        """Zabija osierocone procesy dla danego zadania. Zwraca listę zabitych PID-ów."""
        import signal
        killed = []
        
        # Znajdź osierocone procesy
        # Najpierw znajdź aktualny PID
        current_pid = None
        if self.state_db_path:
            try:
                from engine.state import State
                s = State(db_path=self.state_db_path)
                task = s.get_task(task_id)
                if task and task.get("claimed_by"):
                    m = re.search(r"(\d+)\s*$", task["claimed_by"])
                    if m:
                        current_pid = int(m.group(1))
            except Exception:
                pass
        
        orphan_pids, has_duplicates = self._find_orphan_processes(task_id, current_pid)
        if not orphan_pids:
            return killed
        
        # Znajdź aktualny PID workera z bazy
        current_pid = None
        if self.state_db_path:
            try:
                from engine.state import State
                s = State(db_path=self.state_db_path)
                task = s.get_task(task_id)
                if task and task.get("claimed_by"):
                    m = re.search(r"(\d+)\s*$", task["claimed_by"])
                    if m:
                        current_pid = int(m.group(1))
            except Exception:
                pass
        
        # Zabij osierocone procesy, pozostawiając najnowszy
        for orphan_pid in orphan_pids:
            if orphan_pid == current_pid:
                continue  # Nie zabijaj aktualnego workera
            try:
                # Spróbuj SIGTERM
                os.kill(orphan_pid, signal.SIGTERM)
                killed.append(orphan_pid)
                # Czekaj chwilę
                time.sleep(0.1)
                # Sprawdź, czy proces nadal żyje
                try:
                    os.kill(orphan_pid, 0)
                    # Jeszcze żyje, użyj SIGKILL
                    os.kill(orphan_pid, signal.SIGKILL)
                    time.sleep(0.1)
                except ProcessLookupError:
                    pass  # Proces został zabity
            except ProcessLookupError:
                pass  # Proces już nie istnieje
            except Exception as e:
                # Loguj błąd
                pass
        
        return killed


def get_process_monitor() -> ProcessHealthMonitor:
    """Zwraca singleton ProcessHealthMonitor."""
    global _process_monitor
    if _process_monitor is None:
        _process_monitor = ProcessHealthMonitor(
            log_dir=Path("/home/miko/Projekty/M+/mikowhy-teams/.agents/logs/console"),
            state_db_path=None
        )
    return _process_monitor

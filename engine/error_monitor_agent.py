"""Error Monitor Agent — Systemowy agent monitorujący błędy workerów i LLM w czasie rzeczywistym.

Funkcje:
1. Monitorowanie logów konsoli pod kątem błędów workerów
2. Monitorowanie kolejek LLM (błędy, timeouty, puste odpowiedzi)
3. Natychmiastowe powiadomienia (Telegram, Dashboard)
4. Automatyczne tworzenie zadań naprawczych (opcjonalnie)
5. REST API do pobierania listy błędów (dla dashboard)

Użycie:
  python -m engine.error_monitor_agent run [--port 8767]

Agent działa jako oddzielny proces, monitorujący:
- .agents/logs/console/*.jsonl (błędy workerów)
- .agents/logs/llm_queue/*.jsonl (błędy LLM)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
import subprocess

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.state import State
from engine import telegram_reporter

# Konfiguracja
ERROR_MONITOR_INTERVAL = 10  # Sprawdzaj co 10 sekund
LLM_LOG_DIR = ROOT / ".agents" / "logs" / "llm_queue"
CONSOLE_LOG_DIR = ROOT / ".agents" / "logs" / "console"
MAX_ERRORS_KEPT = 1000  # Maksymalna liczba błędów w pamięci


@dataclass
class MonitoredError:
    """Struktura błędu umieszczona w pamięci agenta."""
    error_id: str
    timestamp: float
    source: str  # 'worker' | 'llm_queue' | 'system'
    task_id: str | None
    step: str | None
    message: str
    raw_log: str
    severity: str = "ERROR"  # ERROR | WARNING | CRITICAL
    resolved: bool = False
    resolved_at: float | None = None
    resolved_by: str | None = None
    notify_sent: bool = False


@dataclass
class LLMQueueMessage:
    """Struktura wiadomości LLM z kolejki."""
    message_id: str
    timestamp: float
    task_id: str
    model: str
    prompt: str
    response: str | None
    tokens: int
    status: str  # sent | received | error | timeout
    error: str | None
    retries: int = 0


class ErrorMonitorAgent:
    """Główna klasa agenta monitorowania błędów."""
    
    def __init__(self):
        self.state = State()
        self.errors: list[MonitoredError] = []
        self.llm_messages: list[LLMQueueMessage] = []
        self.lock = threading.Lock()
        self.running = True
        self.port = 8767
        self.last_check = 0
        
    def _generate_error_id(self) -> str:
        """Generuj unikalne ID błędu."""
        return f"err-{int(time.time())}-{os.getpid()}-{len(self.errors)}"
    
    def _parse_console_line(self, line: str) -> tuple[str, str, str, str] | None:
        """Parsuj linię z logu konsoli workera.
        
        Zwraca: (timestamp, step, task_id, message) lub None
        """
        try:
            # Format: "14:11:08 ℹ [step] message"
            match = re.match(r'(\d{2}:\d{2}:\d{2})\s+([ℹ⚠️🔴💥\u2714\u274c]?)\s+\[([^\]]+)\]\s*(.*)', line)
            if match:
                timestamp_str, symbol, step, message = match.groups()
                # Zamień symbol na znaczny tekst
                if symbol == '💥':
                    severity = 'CRITICAL'
                elif symbol == '🔴':
                    severity = 'ERROR'
                elif symbol == '⚠️':
                    severity = 'WARNING'
                else:
                    severity = 'INFO'
                return (timestamp_str, step, severity, message)
        except Exception:
            pass
        return None
    
    def _is_error_line(self, line: str, severity: str) -> bool:
        """Sprawdź, czy linia zawiera błąd."""
        error_patterns = [
            'Exception:', 'Error:', 'RuntimeError', 'Traceback',
            'FAILED', 'Timeout', 'timeout', 'ConnectionError',
            'pustą implementację', 'bezwład LLM',
            'zwrócił pustą', 'Empty response'
        ]
        return any(pattern.lower() in line.lower() for pattern in error_patterns) and severity in ['ERROR', 'CRITICAL']
    
    def _extract_task_id(self, line: str) -> str | None:
        """Wydobądź task_id z linii logu."""
        # Wzory: "REC-AUDIT-FAILED-2-READY-20261123", "task-123", itp.
        match = re.search(r'([A-Z0-9-]{3,}-[A-Z0-9-]+|task-\d+)', line)
        return match.group(1) if match else None
    
    def _monitor_console_logs(self) -> None:
        """Monitoruj logi konsoli workerów pod kątem błędów."""
        if not CONSOLE_LOG_DIR.exists():
            CONSOLE_LOG_DIR.mkdir(parents=True, exist_ok=True)
            return
        
        for console_file in CONSOLE_LOG_DIR.glob("*.jsonl"):
            try:
                with open(console_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                    
                # Sprawdź ostatnie 20 linii (nowe błędy)
                for line in lines[-20:] if len(lines) > 20 else lines:
                    try:
                        parsed = self._parse_console_line(line.strip())
                        if parsed:
                            timestamp_str, step, severity, message = parsed
                            
                            if self._is_error_line(message, severity):
                                task_id = self._extract_task_id(message) or console_file.stem
                                
                                # Sprawdź, czy ten błąd już znamy
                                error_exists = any(
                                    e.task_id == task_id and 
                                    e.step == step and 
                                    e.message == message[:100]
                                    for e in self.errors
                                )
                                
                                if not error_exists:
                                    error = MonitoredError(
                                        error_id=self._generate_error_id(),
                                        timestamp=time.time(),
                                        source='worker',
                                        task_id=task_id,
                                        step=step,
                                        message=message[:500],
                                        raw_log=line.strip(),
                                        severity=severity
                                    )
                                    
                                    with self.lock:
                                        self.errors.append(error)
                                        # Ograniczenie rozmiaru listy
                                        if len(self.errors) > MAX_ERRORS_KEPT:
                                            self.errors = self.errors[-MAX_ERRORS_KEPT:]
                                    
                                    # Natychmiastowe powiadomienie
                                    self._send_notification(error)
                    except Exception:
                        continue
            except Exception:
                continue
    
    def _monitor_llm_queue_logs(self) -> None:
        """Monitoruj logi kolejki LLM."""
        if not LLM_LOG_DIR.exists():
            LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
            return
        
        for llm_file in LLM_LOG_DIR.glob("*.jsonl"):
            try:
                with open(llm_file, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                for line in lines[-20:] if len(lines) > 20 else lines:
                    try:
                        data = json.loads(line.strip())
                        
                        # Sprawdź, czy to wiadomość LLM z błędem
                        if data.get('status') == 'error' or data.get('error'):
                            message_id = data.get('message_id', data.get('id', str(time.time())))
                            task_id = data.get('task_id')
                            
                            error_exists = any(
                                e.message_id == message_id
                                for e in [err for err in self.errors if hasattr(err, 'message_id')]
                                if hasattr(e, 'message_id')
                            )
                            
                            if not error_exists:
                                error = MonitoredError(
                                    error_id=self._generate_error_id(),
                                    timestamp=data.get('timestamp', time.time()),
                                    source='llm_queue',
                                    task_id=task_id,
                                    step=data.get('step'),
                                    message=data.get('error', 'Unknown LLM error'),
                                    raw_log=line.strip(),
                                    severity='ERROR'
                                )
                                
                                with self.lock:
                                    self.errors.append(error)
                                    if len(self.errors) > MAX_ERRORS_KEPT:
                                        self.errors = self.errors[-MAX_ERRORS_KEPT:]
                                
                                self._send_notification(error)
                        
                        # Zapisz wiadomość LLM do pamięci (ostatnie 100)
                        msg = LLMQueueMessage(
                            message_id=data.get('message_id', data.get('id', str(time.time()))),
                            timestamp=data.get('timestamp', time.time()),
                            task_id=data.get('task_id', ''),
                            model=data.get('model', ''),
                            prompt=data.get('prompt', '')[:200],
                            response=data.get('response', '')[:200],
                            tokens=data.get('tokens', 0),
                            status=data.get('status', 'unknown'),
                            error=data.get('error'),
                            retries=data.get('retries', 0)
                        )
                        
                        with self.lock:
                            self.llm_messages.append(msg)
                            if len(self.llm_messages) > 100:
                                self.llm_messages = self.llm_messages[-100:]
                        
                    except json.JSONDecodeError:
                        continue
            except Exception:
                continue
    
    def _send_notification(self, error: MonitoredError) -> None:
        """Wyślij natychmiastowe powiadomienie o błędzie."""
        if error.notify_sent:
            return
        
        error.notify_sent = True
        
        # Powiadomienie przez print (konsole)
        msg = f"🔴 [{error.severity}] Worker Error in {error.task_id}: {error.step} - {error.message[:100]}"
        print(msg, flush=True)
        
        # Powiadomienie przez Telegram (jeśli skonfigurowany)
        if telegram_reporter.push_enabled():
            try:
                from engine.telegram_reporter import _push
                _push(f"🔴 *ERROR* w zadaniu `{error.task_id}`\nEtap: `{error.step}`\nBłąd: `{error.message[:200]}`")
            except Exception:
                pass
        
        # Zapisz do stanu (metryki)
        try:
            self.state.record_metric("worker_error_auto", {
                "task_id": error.task_id or "",
                "step": error.step or "",
                "source": error.source,
                "severity": error.severity,
                "message": error.message[:200]
            }, error.task_id)
        except Exception:
            pass
    
    def _check_new_errors(self) -> None:
        """Główna metoda sprawdzania nowych błędów."""
        if time.time() - self.last_check < ERROR_MONITOR_INTERVAL:
            return
        
        self.last_check = time.time()
        self._monitor_console_logs()
        self._monitor_llm_queue_logs()
    
    def get_errors(self, limit: int = 100) -> list[dict]:
        """Pobierz listę błędów (dla API)."""
        with self.lock:
            return [
                {
                    "error_id": e.error_id,
                    "timestamp": e.timestamp,
                    "datetime": datetime.fromtimestamp(e.timestamp).isoformat(),
                    "source": e.source,
                    "task_id": e.task_id,
                    "step": e.step,
                    "message": e.message,
                    "severity": e.severity,
                    "resolved": e.resolved,
                    "raw_log": e.raw_log[:500]
                }
                for e in sorted(self.errors, key=lambda x: x.timestamp, reverse=True)[:limit]
            ]
    
    def get_llm_messages(self, limit: int = 100) -> list[dict]:
        """Pobierz listę wiadomości LLM (dla API)."""
        with self.lock:
            return [
                {
                    "message_id": m.message_id,
                    "timestamp": m.timestamp,
                    "datetime": datetime.fromtimestamp(m.timestamp).isoformat(),
                    "task_id": m.task_id,
                    "model": m.model,
                    "prompt": m.prompt,
                    "response": m.response,
                    "tokens": m.tokens,
                    "status": m.status,
                    "error": m.error,
                    "retries": m.retries
                }
                for m in sorted(self.llm_messages, key=lambda x: x.timestamp, reverse=True)[:limit]
            ]
    
    def resolve_error(self, error_id: str) -> bool:
        """Oznacz błąd jako rozwiązany."""
        with self.lock:
            for error in self.errors:
                if error.error_id == error_id:
                    error.resolved = True
                    error.resolved_at = time.time()
                    return True
        return False
    
    def start(self, port: int = 8767) -> None:
        """Uruchom agenta."""
        self.port = port
        self.running = True
        
        # Uruchom HTTP server w tle
        server_thread = threading.Thread(
            target=self._run_http_server,
            daemon=True
        )
        server_thread.start()
        
        # Główna pętla monitorowania
        try:
            print(f"[error_monitor_agent] 🚀 Start (port {port})", flush=True)
            while self.running:
                self._check_new_errors()
                time.sleep(5)  # Sprawdzaj co 5 sekund
        except KeyboardInterrupt:
            print("[error_monitor_agent] 🛑 Stopping...", flush=True)
            self.running = False
        
    def stop(self) -> None:
        """Zatrzymaj agenta."""
        self.running = False
        
    def _run_http_server(self) -> None:
        """Uruchom HTTP server dla API."""
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/api/errors":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "errors": self.server.monitor_agent.get_errors()
                    }, ensure_ascii=False).encode())
                
                elif self.path == "/api/llm-messages":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "messages": self.server.monitor_agent.get_llm_messages()
                    }, ensure_ascii=False).encode())
                
                elif self.path.startswith("/api/errors/"):
                    # GET /api/errors/<error_id>/resolve
                    parts = self.path.split('/')
                    if len(parts) >= 4 and parts[3] == "resolve":
                        error_id = parts[2]
                        success = self.server.monitor_agent.resolve_error(error_id)
                        self.send_response(200 if success else 404)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"success": success}).encode())
                    else:
                        self.send_response(404)
                        self.end_headers()
                
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({
                        "status": "running",
                        "errors_count": len(self.server.monitor_agent.errors),
                        "llm_messages_count": len(self.server.monitor_agent.llm_messages)
                    }, ensure_ascii=False).encode())
        
        class AgentHTTPServer(ThreadingHTTPServer):
            def __init__(self, *args, monitor_agent, **kwargs):
                self.monitor_agent = monitor_agent
                super().__init__(*args, **kwargs)
        
        server = AgentHTTPServer(("127.0.0.1", self.port), Handler, monitor_agent=self)
        print(f"[error_monitor_agent] 🌐 HTTP API on port {self.port}", flush=True)
        server.serve_forever()


def run() -> None:
    """Uruchom agenta z linii poleceń."""
    parser = argparse.ArgumentParser(description="Error Monitor Agent")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "status"])
    parser.add_argument("--port", type=int, default=8767, help="Port for HTTP API")
    args = parser.parse_args()
    
    if args.command == "run":
        agent = ErrorMonitorAgent()
        agent.start(port=args.port)
    elif args.command == "status":
        # Try to connect to running agent
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/") as response:
                print(response.read().decode())
        except Exception as e:
            print(f"Agent not running: {e}")


if __name__ == "__main__":
    run()

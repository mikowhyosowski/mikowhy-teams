"""SshProcessRunner — ExecutionRunnerPort dla zdalnego wykonania przez SSH."""
from __future__ import annotations

import subprocess
from typing import Any

from engine.ports import ExecutionContext, ExecutionResult


class SshProcessRunner:
    """
    Adapter ExecutionRunnerPort uruchamiający komendy zdalnie przez SSH.
    
    Wymaga: klucz SSH lub ssh-agent, dostęp do hosta
    Konfiguracja przez env vars:
    - SSH_HOST: host docelowy (wymagany)
    - SSH_USER: użytkownik (domyślnie: bieżący)
    - SSH_KEY: ścieżka do klucza prywatnego (opcjonalnie)
    - SSH_PORT: port SSH (domyślnie: 22)
    - SSH_OPTS: dodatkowe opcje ssh (opcjonalnie)
    """

    def __init__(
        self,
        host: str | None = None,
        user: str | None = None,
        key_path: str | None = None,
        port: int = 22,
        ssh_opts: str | None = None,
    ):
        import os
        self.host = host or os.environ.get("SSH_HOST")
        self.user = user or os.environ.get("SSH_USER")
        self.key_path = key_path or os.environ.get("SSH_KEY")
        self.port = int(os.environ.get("SSH_PORT", str(port)))
        self.ssh_opts = ssh_opts or os.environ.get("SSH_OPTS", "")

    def _build_ssh_cmd(self, cmd: str) -> list[str]:
        ssh_cmd = ["ssh"]
        if self.port != 22:
            ssh_cmd.extend(["-p", str(self.port)])
        if self.key_path:
            ssh_cmd.extend(["-i", self.key_path])
        if self.ssh_opts:
            ssh_cmd.extend(self.ssh_opts.split())
        target = f"{self.user}@{self.host}" if self.user else self.host
        ssh_cmd.append(target)
        ssh_cmd.append(cmd)
        return ssh_cmd

    def run_command(self, cmd: list[str] | str, *, cwd: str | None = None, timeout_s: float | None = None,
                    env: dict[str, str] | None = None, context: ExecutionContext | None = None) -> ExecutionResult:
        import uuid
        run_id = f"ssh-{uuid.uuid4().hex[:8]}"
        
        if not self.host:
            return ExecutionResult(
                run_id=run_id,
                exit_code=None,
                duration_ms=0,
                log_path=None,
                stdout_tail="",
                stderr_tail="SSH_HOST not configured",
                status="FAILED",
            )
        
        if isinstance(cmd, str):
            cmd_str = cmd
        else:
            cmd_str = " ".join(cmd)
        
        # Build remote command with cwd and env
        remote_parts = []
        if cwd:
            remote_parts.append(f"cd {cwd}")
        if env:
            for k, v in env.items():
                remote_parts.append(f"export {k}='{v}'")
        if context and context.task_id:
            remote_parts.append(f"export SPONSOR_TASK_ID='{context.task_id}'")
        if context and context.agent_role:
            remote_parts.append(f"export SPONSOR_AGENT_ROLE='{context.agent_role}'")
        if context and context.substep:
            remote_parts.append(f"export SPONSOR_STEP='{context.substep}'")
        remote_parts.append(cmd_str)
        remote_cmd = " && ".join(remote_parts)
        
        ssh_cmd = self._build_ssh_cmd(remote_cmd)
        
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            status = "SUCCEEDED" if result.returncode == 0 else "FAILED"
            return ExecutionResult(
                run_id=run_id,
                exit_code=result.returncode,
                duration_ms=0,
                log_path=None,
                stdout_tail=result.stdout[-4000:] if result.stdout else "",
                stderr_tail=result.stderr[-4000:] if result.stderr else "",
                status=status,
            )
        except subprocess.TimeoutExpired:
            return ExecutionResult(
                run_id=run_id,
                exit_code=None,
                duration_ms=0,
                log_path=None,
                stdout_tail="",
                stderr_tail=f"SSH command timed out after {timeout_s}s",
                status="TIMEOUT",
            )
        except Exception as e:
            return ExecutionResult(
                run_id=run_id,
                exit_code=None,
                duration_ms=0,
                log_path=None,
                stdout_tail="",
                stderr_tail=str(e),
                status="FAILED",
            )

    def kill_execution(self, run_id: str) -> dict[str, Any]:
        # SSH processes are hard to kill remotely without tracking PIDs
        # In a real implementation, you'd track SSH connection PIDs
        return {"run_id": run_id, "ok": False, "error": "SSH kill not implemented - track PIDs for real impl"}
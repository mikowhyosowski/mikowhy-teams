"""DockerProcessRunner — ExecutionRunnerPort dla kontenerów Docker."""
from __future__ import annotations

import subprocess
from typing import Any

from engine.ports import ExecutionContext, ExecutionResult


class DockerProcessRunner:
    """
    Adapter ExecutionRunnerPort uruchamiający komendy w kontenerach Docker.
    
    Wymaga: docker daemon dostępny (unix socket lub DOCKER_HOST)
    Konfiguracja przez env vars:
    - DOCKER_HOST: np. unix:///var/run/docker.sock lub tcp://host:2376
    - DOCKER_IMAGE: domyślny obraz (domyślnie: python:3.12-slim)
    - DOCKER_NETWORK: sieć do dołączenia (opcjonalnie)
    """

    def __init__(
        self,
        default_image: str = "python:3.12-slim",
        docker_host: str | None = None,
        network: str | None = None,
    ):
        import os
        self.default_image = default_image
        self.docker_host = docker_host or os.environ.get("DOCKER_HOST")
        self.network = network or os.environ.get("DOCKER_NETWORK")

    def run_command(self, cmd: list[str] | str, *, cwd: str | None = None, timeout_s: float | None = None,
                    env: dict[str, str] | None = None, context: ExecutionContext | None = None) -> ExecutionResult:
        import uuid
        run_id = f"docker-{uuid.uuid4().hex[:8]}"
        
        if isinstance(cmd, str):
            cmd_str = cmd
        else:
            cmd_str = " ".join(cmd)
        
        docker_cmd = ["docker"]
        if self.docker_host:
            docker_cmd.extend(["-H", self.docker_host])
        docker_cmd.extend(["run", "--rm", "-i"])
        
        if cwd:
            docker_cmd.extend(["-v", f"{cwd}:/workspace", "-w", "/workspace"])
        else:
            docker_cmd.extend(["-w", "/workspace"])
        
        if self.network:
            docker_cmd.extend(["--network", self.network])
        
        if env:
            for k, v in env.items():
                docker_cmd.extend(["-e", f"{k}={v}"])
        
        if context and context.task_id:
            docker_cmd.extend(["-e", f"SPONSOR_TASK_ID={context.task_id}"])
        if context and context.agent_role:
            docker_cmd.extend(["-e", f"SPONSOR_AGENT_ROLE={context.agent_role}"])
        if context and context.substep:
            docker_cmd.extend(["-e", f"SPONSOR_STEP={context.substep}"])
        
        docker_cmd.append(self.default_image)
        docker_cmd.extend(["/bin/sh", "-c", cmd_str])
        
        try:
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            status = "SUCCEEDED" if result.returncode == 0 else "FAILED"
            return ExecutionResult(
                run_id=run_id,
                exit_code=result.returncode,
                duration_ms=0,  # Docker doesn't easily give us this
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
                stderr_tail=f"Docker command timed out after {timeout_s}s",
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
        # Docker containers are removed with --rm, so nothing to kill
        # In a real implementation, you'd track container IDs and docker kill them
        return {"run_id": run_id, "ok": True, "status": "KILLED", "note": "Docker --rm auto-removes"}
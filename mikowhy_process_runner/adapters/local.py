"""LocalProcessRunner — ExecutionRunnerPort na engine/sponsor_shell (lokalny proces, PGID kill)."""
from __future__ import annotations

from typing import Any

from engine.ports import ExecutionContext, ExecutionResult
from engine import sponsor_shell


class LocalProcessRunner:
    def __init__(self):
        self._shell = sponsor_shell

    def run_command(self, cmd: list[str] | str, *, cwd: str | None = None, timeout_s: float | None = None,
                    env: dict[str, str] | None = None, context: ExecutionContext | None = None) -> ExecutionResult:
        # sponsor_shell expects float timeout, use default if None
        timeout = timeout_s if timeout_s is not None else sponsor_shell.DEFAULT_TIMEOUT_S
        res = self._shell.run_command(
            cmd, task_id=context.task_id if context else None,
            agent_role=context.agent_role if context else None,
            substep=context.substep if context else None,
            timeout_s=timeout,
            cwd=cwd, env=env)
        return ExecutionResult(
            run_id=res.get("run_id", ""), exit_code=res.get("exit_code"),
            duration_ms=res.get("duration_ms"), log_path=res.get("log_path"),
            stdout_tail=res.get("stdout_tail", ""), stderr_tail=res.get("stderr_tail", ""),
            status=res.get("status", "SUCCEEDED"))

    def kill_execution(self, run_id: str) -> dict[str, Any]:
        return self._shell.kill_run(run_id)
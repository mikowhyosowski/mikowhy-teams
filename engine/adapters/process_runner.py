"""LocalProcessRunner — ExecutionRunnerPort na engine/sponsor_shell (lokalny proces, PGID kill)."""
from __future__ import annotations

from typing import Any

from engine.ports import ExecutionContext, ExecutionResult


class LocalProcessRunner:
    def __init__(self):
        from engine import sponsor_shell
        self._shell = sponsor_shell

    def run_command(self, cmd: list[str] | str, *, cwd: str | None = None, timeout_s: float | None = None,
                    env: dict[str, str] | None = None, context: ExecutionContext | None = None) -> ExecutionResult:
        res = self._shell.run_command(
            cmd, task_id=context.task_id if context else None,
            agent_role=context.agent_role if context else None,
            substep=context.substep if context else None,
            timeout_s=int(timeout_s) if timeout_s else None,
            cwd=cwd, env=env)
        return ExecutionResult(
            run_id=res.get("run_id", ""), exit_code=res.get("exit_code"),
            duration_ms=res.get("duration_ms"), log_path=res.get("log_path"),
            stdout_tail=res.get("stdout_tail", ""), stderr_tail=res.get("stderr_tail", ""),
            status=res.get("status", "SUCCEEDED"))

    def kill_execution(self, run_id: str) -> dict[str, Any]:
        return self._shell.kill_run(run_id)

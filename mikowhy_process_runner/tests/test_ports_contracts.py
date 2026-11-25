"""Testy kontraktowe ExecutionRunnerPort (local, docker, ssh)."""
from __future__ import annotations

import pytest

from engine.ports import ExecutionContext, ExecutionResult, ExecutionRunnerPort


def make_local_runner():
    from mikowhy_process_runner.adapters.local import LocalProcessRunner
    return LocalProcessRunner()


def make_docker_runner():
    from mikowhy_process_runner.adapters.docker import DockerProcessRunner
    return DockerProcessRunner()


def make_ssh_runner():
    from mikowhy_process_runner.adapters.ssh import SshProcessRunner
    return SshProcessRunner(host="invalid-host")  # Will fail gracefully


# Pełne testy tylko dla local (działa bez zewnętrznych zależności)
class TestLocalRunner:
    """Pełne testy kontraktowe dla LocalProcessRunner."""
    
    @pytest.fixture
    def runner(self):
        return make_local_runner()
    
    def test_implements_port(self, runner):
        assert isinstance(runner, ExecutionRunnerPort)
    
    def test_run_command_simple(self, runner):
        result = runner.run_command(["echo", "hello"])
        assert isinstance(result, ExecutionResult)
        assert result.exit_code == 0
        assert "hello" in result.stdout_tail
        assert result.status == "SUCCEEDED"
        assert result.run_id.startswith("run-") or result.run_id  # any non-empty
    
    def test_run_command_with_cwd(self, runner):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            result = runner.run_command(["pwd"], cwd=tmp)
            assert result.exit_code == 0
            assert tmp in result.stdout_tail
    
    def test_run_command_with_env(self, runner):
        result = runner.run_command('echo "$TEST_VAR"', env={"TEST_VAR": "hello_env"})
        assert result.exit_code == 0
        assert "hello_env" in result.stdout_tail
    
    def test_run_command_timeout(self, runner):
        result = runner.run_command(["sleep", "10"], timeout_s=0.1)
        assert result.status in ("TIMEOUT", "KILLED")
    
    def test_run_command_failure(self, runner):
        result = runner.run_command(["false"])
        assert result.exit_code != 0
        assert result.status == "FAILED"
    
    def test_kill_execution(self, runner):
        # Run a long command, then try to kill (will be already done)
        result = runner.run_command(["echo", "test"])
        kill_result = runner.kill_execution(result.run_id)
        assert isinstance(kill_result, dict)
        assert kill_result.get("run_id") == result.run_id


class TestDockerRunner:
    """Testy importu i podstawowe dla DockerProcessRunner."""
    
    def test_implements_port(self):
        runner = make_docker_runner()
        assert isinstance(runner, ExecutionRunnerPort)
    
    def test_health_check_no_crash(self):
        runner = make_docker_runner()
        # Should not crash even if Docker not available
        result = runner.run_command(["echo", "test"], timeout_s=1)
        assert isinstance(result, ExecutionResult)


class TestSshRunner:
    """Testy importu i podstawowe dla SshProcessRunner."""
    
    def test_implements_port(self):
        runner = make_ssh_runner()
        assert isinstance(runner, ExecutionRunnerPort)
    
    def test_health_check_no_crash(self):
        runner = make_ssh_runner()
        # Should not crash even if SSH not available
        result = runner.run_command(["echo", "test"], timeout_s=1)
        assert isinstance(result, ExecutionResult)
        # Will fail gracefully
        assert result.status == "FAILED"
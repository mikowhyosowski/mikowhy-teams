"""GitWorktreeAdapter — VcsWorkspacePort na engine/git_guard + engine/projects + github_bridge."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from engine.ports import WorkspaceHandle


class GitWorktreeAdapter:
    def __init__(self, root: Path | str | None = None):
        from engine import git_guard, projects
        self._git = git_guard
        self._projects = projects
        self._root = Path(root) if root else projects.ROOT

    def _repo(self, task: dict[str, Any]) -> Path:
        from engine.state import State
        return self._projects.repo_for_task(task, State())

    def prepare_workspace(self, task: dict[str, Any]) -> WorkspaceHandle:
        repo = self._repo(task)
        branch = task.get("branch") or f"task/{task['id']}"
        base = self._projects.integration_branch(task.get("project_id") or "mikowhy-teams") or "master"
        wt = self._git.create_worktree(task["id"], base, branch, repo=repo)
        return WorkspaceHandle(task_id=task["id"], path=str(wt), branch=branch, base=base)

    def verify_scope(self, workspace: WorkspaceHandle, scope_paths: list[str]) -> list[str]:
        reverted = self._git.enforce_scope(Path(workspace.path), scope_paths)
        return reverted or []

    def commit_and_push(self, workspace: WorkspaceHandle, task: dict[str, Any]) -> None:
        wt = Path(workspace.path)
        self._git.commit_task(wt, workspace.task_id, f"feat({workspace.task_id.lower()}): {task.get('title', '')[:60]}")
        self._git.push(workspace.branch, wt)

    def integrate_to_target(self, task: dict[str, Any]) -> dict[str, Any]:
        # merge do develop/master robi orchestrator._handle_merges — tu cienka delegacja
        return {"task_id": task["id"], "status": task.get("status"), "note": "integrate via orchestrator._handle_merges"}

    def cleanup_workspace(self, workspace: WorkspaceHandle) -> None:
        self._git.remove_worktree(workspace.task_id, repo=self._root)

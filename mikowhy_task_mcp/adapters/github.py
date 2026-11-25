"""GitHubTaskAdapter — stub adapter TaskSourcePort dla GitHub Issues/Projects."""
from __future__ import annotations

from typing import Any

from engine.ports import TaskSourcePort


class GitHubTaskAdapter:
    """
    Adapter TaskSourcePort integrujący się z GitHub Issues/Projects (v2).
    
    Wymaga: GITHUB_TOKEN z uprawnieniami repo:read, project:read, issues:write
    Konfiguracja przez env vars:
    - GITHUB_OWNER: organizacja/użytkownik
    - GITHUB_REPO: nazwa repozytorium
    - GITHUB_PROJECT_NUMBER: numer projektu (opcjonalnie)
    """

    def __init__(self, owner: str | None = None, repo: str | None = None, project_number: int | None = None):
        self.owner = owner
        self.repo = repo
        self.project_number = project_number
        self._client = None  # httpx.AsyncClient z GitHub GraphQL

    def _get_client(self):
        if self._client is None:
            import httpx
            import os
            token = os.environ.get("GITHUB_TOKEN")
            if not token:
                raise RuntimeError("GITHUB_TOKEN not set")
            self._client = httpx.AsyncClient(
                base_url="https://api.github.com/graphql",
                headers={"Authorization": f"Bearer {token}", "User-Agent": "mikowhy-teams"}
            )
        return self._client

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        client = self._get_client()
        resp = await client.post("", json={"query": query, "variables": variables or {}})
        resp.raise_for_status()
        return resp.json()

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        # STUB: In a real implementation, query GitHub Projects v2 for items
        # with filtering by status field
        return []

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        # STUB: Fetch single issue/project item by ID
        return None

    def claim_next_task(self, role: str, worker_id: str) -> dict[str, Any] | None:
        # STUB: Find unassigned issue matching role label, assign to worker
        return None

    def transition_status(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
        # STUB: Update issue status field in GitHub Project
        return {"id": task_id, "status": new_status}

    def append_comment(self, task_id: str, author: str, text: str) -> None:
        # STUB: Add comment to GitHub Issue
        pass

    def attach_artifact(self, task_id: str, path: str, label: str = "", kind: str = "") -> dict | None:
        # STUB: Upload artifact as comment attachment or release asset
        return None

    def create_subtasks(self, task_id: str, subtask_specs: list[dict[str, Any]]) -> list[str]:
        # STUB: Create sub-issues linked to parent
        return []
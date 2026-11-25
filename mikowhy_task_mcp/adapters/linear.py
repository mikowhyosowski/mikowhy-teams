"""LinearTaskAdapter — stub adapter TaskSourcePort dla Linear."""
from __future__ import annotations

from typing import Any

from engine.ports import TaskSourcePort


class LinearTaskAdapter:
    """
    Adapter TaskSourcePort integrujący się z Linear (linear.app).
    
    Wymaga: LINEAR_API_KEY
    Konfiguracja przez env vars:
    - LINEAR_TEAM_ID: ID zespołu
    - LINEAR_PROJECT_ID: ID projektu (opcjonalnie)
    """

    def __init__(self, team_id: str | None = None, project_id: str | None = None):
        self.team_id = team_id
        self.project_id = project_id
        self._client = None  # httpx.AsyncClient z Linear GraphQL

    def _get_client(self):
        if self._client is None:
            import httpx
            import os
            api_key = os.environ.get("LINEAR_API_KEY")
            if not api_key:
                raise RuntimeError("LINEAR_API_KEY not set")
            self._client = httpx.AsyncClient(
                base_url="https://api.linear.app/graphql",
                headers={"Authorization": api_key, "User-Agent": "mikowhy-teams"}
            )
        return self._client

    async def _graphql(self, query: str, variables: dict | None = None) -> dict:
        client = self._get_client()
        resp = await client.post("", json={"query": query, "variables": variables or {}})
        resp.raise_for_status()
        return resp.json()

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        # STUB: Query Linear issues with filter by state
        return []

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        # STUB: Fetch single issue by ID
        return None

    def claim_next_task(self, role: str, worker_id: str) -> dict[str, Any] | None:
        # STUB: Find unassigned issue matching role label, assign to worker
        return None

    def transition_status(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
        # STUB: Update issue state in Linear
        return {"id": task_id, "status": new_status}

    def append_comment(self, task_id: str, author: str, text: str) -> None:
        # STUB: Add comment to Linear issue
        pass

    def attach_artifact(self, task_id: str, path: str, label: str = "", kind: str = "") -> dict | None:
        # STUB: Upload attachment to Linear issue
        return None

    def create_subtasks(self, task_id: str, subtask_specs: list[dict[str, Any]]) -> list[str]:
        # STUB: Create sub-issues linked to parent
        return []
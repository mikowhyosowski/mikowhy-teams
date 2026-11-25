"""McpTaskAdapter — adapter TaskSourcePort komunikujący się przez MCP (Model Context Protocol)."""
from __future__ import annotations

from typing import Any

from engine.ports import TaskSourcePort


class McpTaskAdapter:
    """
    Adapter TaskSourcePort używający MCP (Model Context Protocol) do komunikacji
    z serwerem zadań. Serwer MCP udostępnia narzędzia:
    - list_tasks(status?) -> list[Task]
    - claim_task(role, worker_id) -> Task
    - update_task_status(task_id, new_status, **fields) -> Task
    - add_comment(task_id, author, text) -> void
    - attach_artifact(task_id, path, label, kind) -> Artifact
    - create_subtasks(task_id, specs) -> list[Task]
    """

    def __init__(self, mcp_server_url: str = "http://localhost:8766/mcp"):
        self.mcp_server_url = mcp_server_url
        self._client = None  # Lazy init: fastmcp.Client

    def _get_client(self):
        if self._client is None:
            from fastmcp import Client
            self._client = Client(self.mcp_server_url)
        return self._client

    def list_tasks(self, status: str | None = None) -> list[dict[str, Any]]:
        client = self._get_client()
        result = client.call_tool("list_tasks", {"status": status} if status else {})
        return result.data if hasattr(result, "data") else result

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        client = self._get_client()
        result = client.call_tool("get_task", {"task_id": task_id})
        data = result.data if hasattr(result, "data") else result
        return data if data else None

    def claim_next_task(self, role: str, worker_id: str) -> dict[str, Any] | None:
        client = self._get_client()
        result = client.call_tool("claim_task", {"role": role, "worker_id": worker_id})
        data = result.data if hasattr(result, "data") else result
        return data if data else None

    def transition_status(self, task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
        client = self._get_client()
        payload = {"task_id": task_id, "new_status": new_status, **fields}
        result = client.call_tool("update_task_status", payload)
        return result.data if hasattr(result, "data") else result

    def append_comment(self, task_id: str, author: str, text: str) -> None:
        client = self._get_client()
        client.call_tool("add_comment", {"task_id": task_id, "author": author, "text": text})

    def attach_artifact(self, task_id: str, path: str, label: str = "", kind: str = "") -> dict | None:
        client = self._get_client()
        result = client.call_tool("attach_artifact", {
            "task_id": task_id, "path": path, "label": label, "kind": kind
        })
        data = result.data if hasattr(result, "data") else result
        return data

    def create_subtasks(self, task_id: str, subtask_specs: list[dict[str, Any]]) -> list[str]:
        client = self._get_client()
        result = client.call_tool("create_subtasks", {
            "task_id": task_id, "subtask_specs": subtask_specs
        })
        data = result.data if hasattr(result, "data") else result
        return data if isinstance(data, list) else []
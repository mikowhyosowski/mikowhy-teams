"""MCP Server for Task Management (FastMCP)."""
from __future__ import annotations

import os
from typing import Any

from fastmcp import FastMCP

# Adapter selection via env
TASK_PROVIDER = os.environ.get("TASK_PROVIDER", "sqlite")

if TASK_PROVIDER == "sqlite":
    from mikowhy_task_mcp.adapters.sqlite import SqliteTaskAdapter
    adapter = SqliteTaskAdapter()
elif TASK_PROVIDER == "memory":
    from mikowhy_task_mcp.adapters.memory import InMemoryTaskAdapter
    adapter = InMemoryTaskAdapter()
else:
    raise RuntimeError(f"MCP server: unsupported TASK_PROVIDER={TASK_PROVIDER}")

mcp = FastMCP("mikowhy-task-mcp")


@mcp.tool
def list_tasks(status: str | None = None) -> list[dict[str, Any]]:
    """List tasks, optionally filtered by status."""
    return adapter.list_tasks(status=status)


@mcp.tool
def get_task(task_id: str) -> dict[str, Any] | None:
    """Get a single task by ID."""
    return adapter.get_task(task_id)


@mcp.tool
def claim_task(role: str, worker_id: str) -> dict[str, Any] | None:
    """Claim next available task for a role."""
    return adapter.claim_next_task(role=role, worker_id=worker_id)


@mcp.tool
def update_task_status(task_id: str, new_status: str, **fields: Any) -> dict[str, Any]:
    """Transition task to new status."""
    return adapter.transition_status(task_id, new_status, **fields)


@mcp.tool
def add_comment(task_id: str, author: str, text: str) -> None:
    """Add a comment to a task."""
    adapter.append_comment(task_id, author, text)


@mcp.tool
def attach_artifact(task_id: str, path: str, label: str = "", kind: str = "") -> dict | None:
    """Attach an artifact to a task."""
    return adapter.attach_artifact(task_id, path, label=label, kind=kind)


@mcp.tool
def create_subtasks(task_id: str, subtask_specs: list[dict[str, Any]]) -> list[str]:
    """Create subtasks for a task."""
    return adapter.create_subtasks(task_id, subtask_specs)


if __name__ == "__main__":
    import uvicorn
    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8766"))
    uvicorn.run(mcp.http_app(), host=host, port=port)